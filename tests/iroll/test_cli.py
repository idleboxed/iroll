"""CLI defaults, selection, output and portable entry point. SPDX-License-Identifier: BSD-3-Clause."""

import io
import json
import logging
import signal
import subprocess
import sys
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any, NoReturn

import pytest

from iroll import cli
from iroll.host import dependencies
from iroll.components import registry
from iroll.rollout.build import BuildOptions, Builder
from iroll.card.service import CardInitializer, read_instructions
from iroll.card.layout import CardPlan, CardTarget
from iroll.card.linux import LinuxCardBackend
from iroll.common.errors import BuildError
from iroll.host.platforms import DEFAULT_PLATFORM, Toolchain
from iroll.common.process import Runner
from iroll.sdk.prepare import SdkOptions, SdkPreparer
from iroll.components.registry import COMPONENTS, Component


def test_build_defaults_are_x86_with_in_out_and_tmp_in_current_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    received = []

    class FakeBuilder:
        def __init__(self, options: BuildOptions, **kwargs: object) -> None:
            received.append(options)

        def run(self) -> Path:
            return tmp_path / "logs"

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("XDG_CACHE_HOME", f"{tmp_path / "ignored-xdg-cache"}")
    monkeypatch.setenv("HOME", f"{tmp_path / "ignored-home"}")
    monkeypatch.setattr(cli, "Builder", FakeBuilder)

    assert cli.main(["rollout", "--json"]) == 0

    options = received[0]
    assert options.toolchain.profile.name == "linux-x86_64"
    assert options.source_root == tmp_path / "in/components"
    assert options.bios == tmp_path / "in/bios"
    assert options.dest == tmp_path / "out"
    assert options.archives == options.downloads
    assert options.downloads == tmp_path / "in/downloads"
    assert options.build_dir == tmp_path / "tmp"
    assert options.logs == tmp_path / "logs/rollout"
    assert options.components == tuple(
        component for component in COMPONENTS if DEFAULT_PLATFORM in component.platforms
    )
    assert options.no_diag is False
    assert json.loads(capsys.readouterr().out)["system"] == f"{tmp_path / "out/system"}"
    assert not list(tmp_path.iterdir())


def test_custom_downloads_directory_is_resolved_from_current_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    received = []

    def run(self: Builder) -> Path:
        received.append(self.options)

        return tmp_path / "logs"

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli.Builder, "run", run)

    assert cli.main(["rollout", "--downloads", "source-downloads"]) == 0

    assert received[0].archives == received[0].downloads == tmp_path / "source-downloads"
    assert received[0].build_dir == tmp_path / "tmp"
    assert received[0].logs == tmp_path / "logs/rollout"
    assert not list(tmp_path.iterdir())


def test_arm_list_includes_igui(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["list", "--platform", "rk3032", "--logs_dir", f"{tmp_path / "logs"}", "--json"]) == 0

    document = json.loads(capsys.readouterr().out)
    igui = next(row for row in document["components"] if row["name"] == "igui")
    assert igui["supported"]
    assert igui["reason"] == ""


@pytest.mark.parametrize("no_diag", [False, True])
def test_rollout_accepts_arbitrary_destination(
    tmp_path: Path, sdk: Path, runner: Any, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture, no_diag: bool,
) -> None:
    received = []

    def run(self: Builder) -> Path:
        received.append(self.options)

        return tmp_path / "logs"

    monkeypatch.setattr(cli, "Runner", lambda: runner)
    monkeypatch.setattr(cli.Builder, "run", run)

    assert cli.main(
        [
            "rollout",
            "--platform",
            "rk3032",
            "--sdk",
            f"{sdk}",
            "--dest",
            f"{tmp_path / "ordinary-directory"}",
            "--json",
            *(["--no-diag"] if no_diag else []),
        ]
    ) == 0

    assert received[0].dest == tmp_path / "ordinary-directory"
    assert received[0].components == COMPONENTS
    assert received[0].no_diag is no_diag
    assert json.loads(capsys.readouterr().out)["status"] == "BUILT_AND_PUBLISHED"
    assert not any(record.levelno >= logging.WARNING for record in caplog.records)
    assert "rk3032" not in caplog.text


def test_card_help_lists_only_supported_commands(capsys: pytest.CaptureFixture[str]) -> None:

    with pytest.raises(SystemExit, match="^0$") as error:
        cli.main(["card", "--help"])

    assert error.value.code == 0
    output = capsys.readouterr()
    assert "{init,deps}" in output.out
    assert "migrate-saves" not in output.out
    assert not output.err


@pytest.mark.parametrize("arguments", [[], ["--dry-run", "--json"]])
def test_removed_save_migration_is_rejected_without_changing_files(tmp_path: Path, arguments: list[str]) -> None:
    legacy = tmp_path / "states/unchanged.state4"
    legacy.parent.mkdir()
    legacy.write_bytes(b"synthetic old state")
    before = {path: (path.stat().st_ino, path.stat().st_mtime_ns) for path in tmp_path.rglob("*")}
    command = Path(sys.executable).parent / "iroll"

    result = subprocess.run(
        [f"{command}", "card", "migrate-saves", f"{tmp_path}", *arguments],
        cwd=tmp_path, capture_output=True, text=True, check=False,
    )

    assert result.returncode == 2, result.stderr
    assert "invalid choice: 'migrate-saves'" in result.stderr
    assert not result.stdout
    assert {path: (path.stat().st_ino, path.stat().st_mtime_ns) for path in tmp_path.rglob("*")} == before
    assert legacy.read_bytes() == b"synthetic old state"


def test_unknown_flags_use_argparse_exit_code() -> None:

    with pytest.raises(SystemExit, match="^2$") as error:
        cli.main(["rollout", "--with-missing"])

    assert error.value.code == 2


def test_card_init_reexecs_through_sudo_with_the_same_arguments(monkeypatch: pytest.MonkeyPatch) -> None:
    stream = io.StringIO()
    stream.isatty = lambda: True
    calls = []

    def which(name: str) -> str | None:
        return {"sudo": "/usr/bin/sudo", "iroll": "/home/user/.local/bin/iroll"}.get(name)

    def execv(path: str, argv: list[str]) -> None:
        calls.append((path, argv))
        raise RuntimeError("synthetic exec")

    monkeypatch.setattr(cli.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(cli.os, "execv", execv)
    monkeypatch.setattr(cli.shutil, "which", which)
    monkeypatch.setattr(cli.sys, "stdin", stream)
    monkeypatch.setattr(cli.sys, "argv", ["iroll"])
    arguments = ["card", "init", "/dev/sdc", "--json"]
    args = cli.create_parser().parse_args(arguments)

    with pytest.raises(RuntimeError, match="synthetic exec"):
        cli.elevate_card_init(args, arguments)

    assert calls == [(
        "/usr/bin/sudo",
        ["/usr/bin/sudo", "--", "/home/user/.local/bin/iroll", *arguments],
    )]


def test_card_dry_run_does_not_request_sudo(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(cli.os, "execv", lambda *args: pytest.fail("dry-run must not execute sudo"))
    arguments = ["card", "init", "/dev/sdc", "--dry-run"]

    cli.elevate_card_init(cli.create_parser().parse_args(arguments), arguments)


def test_sudo_owner_is_read_without_changing_files(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli.os, "geteuid", lambda: 0)
    monkeypatch.setenv("SUDO_UID", "1234")
    monkeypatch.setenv("SUDO_GID", "5678")
    owner = cli.read_sudo_owner()

    assert owner == (1234, 5678)


@pytest.mark.parametrize("error, expected", [(BuildError("broken"), 1), (KeyboardInterrupt(), 130)])
def test_cli_reports_build_failure_or_cancellation(
    monkeypatch: pytest.MonkeyPatch, error: BaseException, expected: int, caplog: pytest.LogCaptureFixture,
) -> None:
    def fail(self: Builder) -> Path:
        raise error

    monkeypatch.setattr(cli.Builder, "run", fail)

    assert cli.main(["rollout"]) == expected
    assert caplog.records[-1].levelno == logging.ERROR


def test_logging_keeps_callers_handlers_without_duplicates(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    package = logging.getLogger("iroll")
    root = logging.getLogger()
    handlers, root_handlers, level = list(package.handlers), list(root.handlers), package.level

    with caplog.at_level(logging.INFO):
        assert cli.main(["list", "--logs_dir", f"{tmp_path / "logs"}"]) == 0
        assert cli.main(["list", "--logs_dir", f"{tmp_path / "logs"}"]) == 0

    assert len(caplog.records) == 2 * len(COMPONENTS)
    assert package.handlers == handlers
    assert root.handlers == root_handlers
    assert package.level == level


def test_installed_entrypoint_runs_from_foreign_directory_without_readelf(tmp_path: Path) -> None:
    command = Path(sys.executable).parent / "iroll"
    result = subprocess.run(
        [f"{command}", "list", "--json"], cwd=tmp_path, capture_output=True, text=True, check=False
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["platform"] == DEFAULT_PLATFORM
    assert not result.stderr
    reports = list((tmp_path / "logs/list").glob("*/result.json"))
    assert len(reports) == 1


def test_import_does_not_create_files_or_run_processes(tmp_path: Path) -> None:
    code = """
import subprocess
def forbidden(*args: object, **kwargs: object) -> None:
    raise AssertionError("Import must not start a process")
subprocess.Popen = forbidden
import iroll.cli
"""
    result = subprocess.run([sys.executable, "-c", code], cwd=tmp_path, capture_output=True, text=True)

    assert result.returncode == 0, result.stderr
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("flags,names,answers", [
    pytest.param([], [], [], id="unattended"),
    pytest.param(["--ask"], ["iman.json", "emulation.json"], [True, False], id="ask"),
])
def test_rollout_passes_optional_confirmation_without_changing_json_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    flags: list[str], names: list[str], answers: list[bool],
) -> None:
    stream = io.StringIO("y\nn\n")
    stream.isatty = lambda: True
    monkeypatch.setattr(cli.sys, "stdin", stream)
    paths = [tmp_path / "system/iman" / name for name in names]
    received = []

    def run(self: Builder) -> Path:
        received.append(self.confirm_replace)

        return tmp_path / "logs"

    monkeypatch.setattr(cli.Builder, "run", run)

    assert cli.main(["rollout", "--json", *flags]) == 0

    callback, = received
    assert (callback is not None) == bool(paths)
    assert [callback(path) for path in paths] == answers
    output = capsys.readouterr()
    assert json.loads(output.out)["status"] == "BUILT_AND_PUBLISHED"
    assert output.err.count("[y/N]") == len(paths)
    assert all(f"{path}" in output.err for path in paths)


@pytest.mark.parametrize(
    "reply,accepted", [("y\n", True), ("Y\r\n", True), ("n\n", False), ("\n", False), ("", False)],
)
def test_file_confirmation_accepts_y_and_defaults_to_no(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], reply: str, accepted: bool,
) -> None:
    stream = io.StringIO(reply)
    stream.isatty = lambda: True
    monkeypatch.setattr(cli.sys, "stdin", stream)
    path = tmp_path / "emulation.json"

    assert cli.confirm_replace(path) is accepted

    output = capsys.readouterr()
    assert not output.out
    assert f"{path}" in output.err
    assert "[y/N]" in output.err


def test_ask_rejects_noninteractive_input_before_any_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO("y\n"))
    monkeypatch.chdir(tmp_path)

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("Noninteractive --ask must fail before any build work")

    monkeypatch.setattr(cli, "discover_sdk", forbidden)
    monkeypatch.setattr(cli, "Builder", forbidden)

    assert cli.main(["rollout", "--ask"]) == 1

    assert "--ask requires an interactive terminal" in caplog.text
    assert not list(tmp_path.iterdir())


def test_rollout_resolves_build_and_logs_independently_from_downloads_and_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    received = []

    def run(self: Builder) -> Path:
        received.append(self.options)

        return self.options.logs / "2026-09-23T12:00:00Z"

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli.Builder, "run", run)

    assert cli.main([
        "rollout", "--source", "sources", "--downloads", "archives",
        "--build-dir", "scratch/build", "--logs_dir", "reports", "--json",
    ]) == 0

    options = received[0]
    assert options.source_root == tmp_path / "sources"
    assert options.downloads == tmp_path / "archives"
    assert options.build_dir == tmp_path / "scratch/build"
    assert options.logs == tmp_path / "reports/rollout"
    assert options.dest == tmp_path / "out"
    assert json.loads(capsys.readouterr().out)["reports"] == f"{options.logs / "2026-09-23T12:00:00Z"}"
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("source_kind", ["directories", "absolute-symlinks", "relative-symlinks"])
def test_rollout_builds_default_layout_with_local_components(
    options: BuildOptions, runner: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str], source_kind: str,
) -> None:
    working = tmp_path / "working"
    components = working / "in/components"
    downloads = working / "in/downloads"
    downloads.mkdir(parents=True)

    if source_kind == "directories":
        options.source_root.rename(components)

    else:
        components.mkdir()

        for name in ("igui", "iman"):
            target = options.source_root / name

            if source_kind == "relative-symlinks":
                target = Path("../../../project") / name

            (components / name).symlink_to(target, target_is_directory=True)

    for component in (item for item in options.components if item.source):
        (options.archives / component.source.archive).rename(downloads / component.source.archive)

        for module in component.submodules:
            (options.archives / module.source.archive).rename(downloads / module.source.archive)

    archive_state = {path.name: (path.stat().st_ino, path.stat().st_mtime_ns) for path in downloads.iterdir()}
    monkeypatch.chdir(working)
    monkeypatch.setattr(cli, "Runner", lambda: runner)
    monkeypatch.setattr(cli, "COMPONENTS", options.components)
    monkeypatch.setattr(registry, "COMPONENTS", options.components)

    assert cli.main(["rollout", "--json"]) == 0

    document = json.loads(capsys.readouterr().out)
    assert document["system"] == f"{working / "out/system"}"
    assert {path.name for path in working.iterdir()} == {"in", "logs", "out", "tmp"}
    assert {path.name for path in (working / "in").iterdir()} == {"components", "downloads"}
    assert {path.name for path in (working / "tmp").iterdir()} == {"build"}
    assert Path(document["reports"]).parent == working / "logs/rollout"
    assert (Path(document["reports"]) / "result.json").is_file()
    assert not (working / "tmp/out_stage").exists()
    assert {path.name for path in (working / "tmp/build").iterdir()} == {"our", "their"}
    assert {path.name for path in (working / "out").iterdir()} == {"system", "games", "INSTRUCTIONS.md"}
    assert {path.name for path in (working / "out/games").iterdir()} == {"states", "saves"}

    for component in options.components:
        assert (working / "out/system" / component.destination).is_file()

    assert archive_state == {path.name: (path.stat().st_ino, path.stat().st_mtime_ns) for path in downloads.iterdir()}
    assert (working / "out/system/igui/controls/2563-0555-hidraw.json").is_file()


def test_default_components_do_not_fall_back_to_projects_in_working_directory(
    options: BuildOptions, runner: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.chdir(options.source_root)
    monkeypatch.setattr(cli, "Runner", lambda: runner)

    assert cli.main(["rollout", "--with-igui", "--with-iman"]) == 1

    assert "No such file or directory" in caplog.text
    assert f"{options.source_root / "in"}" in caplog.text
    assert not (options.source_root / "in").exists()
    assert not (options.source_root / "out").exists()
    assert not runner.calls


@pytest.mark.parametrize("relative", ["in", "in/components", "out", "tmp", "logs"])
def test_default_layout_rejects_symlink_roots_before_commands(
    tmp_path: Path, runner: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, relative: str,
) -> None:
    working = tmp_path / "working"
    outside = tmp_path / "outside"
    outside.mkdir()
    path = working / relative
    path.parent.mkdir(parents=True)
    path.symlink_to(outside, target_is_directory=True)
    monkeypatch.chdir(working)
    monkeypatch.setattr(cli, "Runner", lambda: runner)

    assert cli.main(["rollout", "--with-igui"]) == 1

    assert "Symlink path is not allowed" in caplog.text
    assert not list(outside.iterdir())
    assert not runner.calls


def test_bios_option_is_relative_to_working_directory_not_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    received = []

    def run(self: Builder) -> Path:
        received.append(self.options)

        return tmp_path / "logs"

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli.Builder, "run", run)

    assert cli.main(["rollout", "--source", "projects", "--bios", "firmware"]) == 0

    assert received[0].bios == tmp_path / "firmware"
    assert received[0].source_root == tmp_path / "projects"
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("platform", ["linux-x86_64", "rk3032"])
def test_list_has_one_sega_core_and_the_requested_new_systems(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], platform: str,
) -> None:
    assert cli.main(["list", "--platform", platform, "--logs_dir", f"{tmp_path / "logs"}", "--json"]) == 0

    rows = {row["name"]: row for row in json.loads(capsys.readouterr().out)["components"]}
    expected = {
        "core-nestopia": ["NES", "FDS"],
        "core-gambatte": ["GB", "GBC"],
        "core-snes9x2010": ["SNES"],
        "core-gpsp": ["GBA"],
        "core-picodrive": ["MD", "SMS", "GG", "SG1000", "SC3000", "SCD", "32X", "PICO"],
        "core-pcsx-rearmed": ["PSX"],
    }
    assert {name for name in rows if name.startswith("core-")} == {*expected, "core-mupen64plus-next"}

    for name, systems in expected.items():
        assert rows[name]["game_platforms"] == systems
        assert rows[name]["supported"] is True

    assert len({system for systems in expected.values() for system in systems}) == sum(map(len, expected.values()))
    n64 = rows["core-mupen64plus-next"]
    assert n64["game_platforms"] == ["N64"]
    assert n64["supported"] is (platform == "rk3032")
    assert n64["flag"] is None


@pytest.mark.parametrize("name", ["gambatte", "snes9x2010", "gpsp", "picodrive", "pcsx-rearmed"])
def test_new_core_cli_switch_selects_only_that_component(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str,
) -> None:
    received = []

    def run(self: Builder) -> Path:
        received.append(self.options)

        return tmp_path / "logs"

    monkeypatch.setattr(cli.Builder, "run", run)
    monkeypatch.chdir(tmp_path)

    assert cli.main(["rollout", f"--with-core-{name}"]) == 0

    assert [component.name for component in received[0].components] == [f"core-{name}"]


def test_removed_genesis_plus_gx_switch_is_not_silently_accepted() -> None:

    with pytest.raises(SystemExit, match="^2$") as exc:
        cli.main(["rollout", "--with-core-genesis-plus-gx"])

    assert exc.value.code == 2


@pytest.mark.parametrize("command", ["rollout", "deps"])
def test_n64_is_selected_by_existing_core_group_without_expanding_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], command: str,
) -> None:
    received = []

    def build(self: Builder) -> Path:
        received.append(self.options.components)

        return tmp_path / "logs"

    def check(
        components: tuple[Component, ...], toolchain: Toolchain, runner: Runner, *, cwd: Path,
    ) -> cli.dependencies.DependencyReport:
        received.append(components)

        return cli.dependencies.DependencyReport((), (), ())

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli.Builder, "run", build)
    monkeypatch.setattr(cli.dependencies, "check", check)
    monkeypatch.setattr(cli, "discover_sdk", lambda profile, sdk, runner, *, cwd: Toolchain(profile))

    assert cli.main([command, "--platform", "rk3032", "--with-cores"]) == 0

    components = received[0]
    assert "core-mupen64plus-next" in {component.name for component in components}

    with pytest.raises(SystemExit, match="^2$") as exc:
        cli.main([command, "--with-core-mupen64plus-next"])

    assert exc.value.code == 2

    with pytest.raises(SystemExit, match="^0$") as help_exit:
        cli.main([command, "--help"])

    assert help_exit.value.code == 0
    assert "--with-core-mupen64plus-next" not in capsys.readouterr().out


@pytest.mark.parametrize("dry_run,flags,status,warnings", [
    pytest.param(False, [], "CARD_INITIALIZED", [], id="initialize"),
    pytest.param(
        True, ["--dry-run"], "DRY_RUN",
        ["ALL EXISTING DATA WILL BE LOST on initialization; dry-run made no changes"], id="dry-run",
    ),
])
def test_card_cli_delegates_scenario_and_owner_without_creating_report_files(
    tmp_path: Path, target: CardTarget, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture, dry_run: bool,
    flags: list[str], status: str, warnings: list[str],
) -> None:
    received = []

    class Initializer:
        def run(
            self, device: str, *, logs_root: Path, dry_run: bool,
            confirm: Callable[[CardPlan], bool], owner: tuple[int, int] | None = None,
        ) -> dict[str, Any]:
            received.append((device, logs_root, dry_run, confirm, owner))

            return {"status": status}

    monkeypatch.setattr(cli, "CardInitializer", Initializer)
    monkeypatch.setattr(cli.os, "geteuid", lambda: 0)
    monkeypatch.setattr(cli, "read_sudo_owner", lambda: (1234, 5678))
    arguments = ["card", "init", target.device, "--logs_dir", f"{tmp_path / "logs"}", "--json", *flags]

    assert cli.main(arguments) == 0

    output = capsys.readouterr()
    assert json.loads(output.out)["status"] == status
    assert "status" not in caplog.text
    assert [record.getMessage() for record in caplog.records if record.levelno == logging.WARNING] == warnings
    assert received == [(target.device, tmp_path / "logs/card-init", dry_run, cli.confirm_card, (1234, 5678))]
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("output,message", [
    pytest.param("{", "Invalid lsblk JSON output", id="malformed-json"),
    pytest.param("[]", "object in lsblk JSON output", id="document-list"),
])
def test_card_cli_reports_listing_errors_without_traceback_or_writes(
    inspection: tuple[LinuxCardBackend, dict[str, str], list[Path]], tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture,
    output: str, message: str,
) -> None:
    backend, texts, holders = inspection
    monkeypatch.setattr(backend, "run_command", lambda *args, **kwargs: output)
    monkeypatch.setattr(cli, "CardInitializer", lambda: CardInitializer(backend))
    monkeypatch.setattr(cli.os, "geteuid", lambda: 0)
    monkeypatch.setattr(cli, "read_sudo_owner", lambda: None)
    monkeypatch.chdir(tmp_path)

    assert cli.main(["card", "init", "/dev/sdz", "--logs_dir", f"{tmp_path / "logs"}", "--json"]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""
    assert message in caplog.text
    assert len(caplog.records) == 1 and caplog.records[0].levelno == logging.ERROR
    assert caplog.records[0].exc_info is None
    assert "Traceback" not in caplog.text
    assert not list(tmp_path.iterdir())


def test_card_dry_run_json_is_clean(
    backend: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(cli, "CardInitializer", lambda: CardInitializer(backend))

    assert cli.main(["card", "init", "/dev/sdz", "--dry-run", "--logs_dir", f"{tmp_path / "logs"}", "--json"]) == 0

    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "DRY_RUN" and result["target"]["diskseq"] == 42
    assert result["confirmation"] == "y/Y" and result["confirmation_default"] == "n"
    assert "ALL EXISTING DATA WILL BE LOST" in caplog.text
    assert backend.calls == ["inspect"]

def test_card_cli_uses_default_logs_root() -> None:
    args = cli.create_parser().parse_args(["card", "init", "/dev/sdz"])

    assert args.logs_dir == Path("logs")
    assert not hasattr(args, "journal")

@pytest.mark.parametrize(
    "reply,accepted",
    [
        ("y\n", True), ("Y\n", True), ("Y\r\n", True),
        ("n\n", False), ("N\n", False), ("\n", False), ("", False),
        ("yes\n", False), (" y\n", False), ("y \n", False),
        ("ERASE /dev/sdz 42\n", False), ("invalid\n", False),
    ],
)
def test_confirmation_accepts_only_y_and_defaults_to_no(
    target: CardTarget, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    reply: str, accepted: bool,
) -> None:
    stream = io.StringIO(reply)
    stream.isatty = lambda: True
    monkeypatch.setattr(cli.sys, "stdin", stream)

    assert cli.confirm_card(CardPlan(target)) == accepted

    result = capsys.readouterr()
    assert not result.out
    assert "ALL DATA ON THE SELECTED CARD WILL BE LOST" in result.err
    assert "2147483648" in result.err and "Synthetic SD" in result.err
    assert "Device: /dev/sdz" in result.err
    assert "diskseq" not in result.err
    assert "[y/N]" in result.err

def test_confirmation_lists_automatic_unmounts(
    target: CardTarget, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    stream = io.StringIO("n\n")
    stream.isatty = lambda: True
    monkeypatch.setattr(cli.sys, "stdin", stream)
    plan = CardPlan(replace(target, mountpoints=("/media/card",)))

    assert cli.confirm_card(plan) is False

    error = capsys.readouterr().err
    assert "Mounted path to unmount: /media/card" in error
    assert "unmounted automatically after confirmation" in error

def test_noninteractive_confirmation_is_rejected(target: CardTarget, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO("y\n"))

    with pytest.raises(BuildError, match="interactive terminal"):
        cli.confirm_card(CardPlan(target))

@pytest.mark.parametrize("reply", ["\n", "n\n", "N\n", "", "invalid\n"])
def test_card_cli_decline_never_opens_or_changes_card(
    backend: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, reply: str,
) -> None:
    stream = io.StringIO(reply)
    stream.isatty = lambda: True
    monkeypatch.setattr(cli.sys, "stdin", stream)
    monkeypatch.setattr(cli.os, "geteuid", lambda: 0)
    monkeypatch.setattr(cli, "CardInitializer", lambda: CardInitializer(backend))

    result = cli.main([
        "card", "init", "/dev/sdz", "--logs_dir", f"{tmp_path / "logs"}",
    ])

    assert result == 1
    assert "Confirmation declined" in caplog.text
    assert backend.calls == ["inspect", "preflight"]
    runs = list((tmp_path / "logs/card-init").iterdir())
    assert len(runs) == 1
    failure = json.loads((runs[0] / "failure.json").read_text())
    assert failure["card_data_changed"] is False

@pytest.mark.parametrize("flag", ["--force", "--yes"])
def test_card_cli_has_no_confirmation_bypass(flag: str) -> None:

    with pytest.raises(SystemExit, match="^2$") as error:
        cli.main(["card", "init", "/dev/sdz", flag])

    assert error.value.code == 2

def test_sigterm_uses_cancellation_and_restores_handler() -> None:
    previous = signal.getsignal(signal.SIGTERM)

    with pytest.raises(KeyboardInterrupt, match="^$"), cli.handle_cancellation_signals():
        handler = signal.getsignal(signal.SIGTERM)
        handler(signal.SIGTERM, None)

    assert signal.getsignal(signal.SIGTERM) == previous

def test_card_cli_initializes_with_automatic_identity_and_no_sequence_argument(
    backend: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    backend.target = replace(backend.target, diskseq=123)
    stream = io.StringIO("Y\n")
    stream.isatty = lambda: True
    monkeypatch.setattr(cli.sys, "stdin", stream)
    monkeypatch.setattr(cli.os, "geteuid", lambda: 0)
    monkeypatch.setattr(cli, "CardInitializer", lambda: CardInitializer(backend))

    result = cli.main([
        "card", "init", "/dev/sdz", "--logs_dir", f"{tmp_path / "logs"}", "--json",
    ])

    assert result == 0
    document = json.loads(capsys.readouterr().out)
    assert document["status"] == "CARD_INITIALIZED" and document["diskseq"] == 123
    assert backend.calls[:3] == ["inspect", "preflight", "inspect"]
    assert backend.content == read_instructions()
    assert Path(document["journal"]).parent.parent == tmp_path / "logs/card-init"
    assert Path(document["journal"]).name == "journal.jsonl"

def test_card_cli_rejects_removed_sequence_parameter_before_inspection(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    def forbidden() -> None:
        pytest.fail("Removed option must fail before device access")

    monkeypatch.setattr(cli, "CardInitializer", forbidden)

    with pytest.raises(SystemExit, match="^2$") as error:
        cli.main(["card", "init", "/dev/sdz", "--diskseq", "42"])

    assert error.value.code == 2
    assert "unrecognized arguments" in capsys.readouterr().err


@pytest.mark.parametrize("command", ["rollout"])
def test_cli_resolves_explicit_sdk_but_downloads_stay_relative_to_cwd_not_source(
    sdk: Path, runner: Any, monkeypatch: pytest.MonkeyPatch, command: str,
) -> None:
    received = []

    def run(self: Builder) -> Path:
        received.append(self.options)

        return sdk.parent / "logs"

    monkeypatch.chdir(sdk.parent)
    monkeypatch.setattr(cli, "Runner", lambda: runner)
    monkeypatch.setattr(cli.Builder, "run", run)

    assert cli.main([command, "--platform", "rk3032", "--sdk", "sdk", "--source", "sources"]) == 0

    options = received[0]
    assert f"{options.toolchain.sysroot}" == runner.sdk_sysroot
    assert options.source_root == sdk.parent / "sources"
    assert options.downloads == options.archives == sdk.parent / "in/downloads"
    assert options.build_dir == sdk.parent / "tmp"
    assert options.logs == sdk.parent / "logs/rollout"
    assert not options.downloads.exists()

def test_cli_discovers_default_rk3032_sdk(
    sdk: Path, runner: Any, dependency_tools: None, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture,
) -> None:
    cwd = sdk.parent
    original_sdk = cwd / "original-sdk"
    sdk.rename(original_sdk)
    default_sdk = cwd / "in/sdk/rk3032"
    default_sdk.parent.mkdir(parents=True)
    original_sdk.rename(default_sdk)
    runner.sdk_sysroot = f"{default_sdk / "arm-buildroot-linux-gnueabihf/sysroot"}"
    monkeypatch.chdir(cwd)
    monkeypatch.setattr(cli, "Runner", lambda: runner)

    assert cli.main(["deps", "--platform", "rk3032", "--logs_dir", f"{cwd / "logs"}", "--json"]) == 0

    result = json.loads(capsys.readouterr().out)
    assert result["ready"] is True
    assert f"SDK sysroot: {runner.sdk_sysroot}" in result["checks"]
    assert not any(record.levelno >= logging.WARNING for record in caplog.records)

def test_deps_uses_the_same_sdk_discovery(
    sdk: Path, runner: Any, dependency_tools: None,
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(cli, "Runner", lambda: runner)

    assert cli.main([
        "deps", "--platform", "rk3032", "--sdk", f"{sdk}", "--logs_dir", f"{sdk.parent / "logs"}", "--json",
    ]) == 0

    result = json.loads(capsys.readouterr().out)
    assert result["ready"] is True
    assert f"SDK sysroot: {runner.sdk_sysroot}" in result["checks"]

@pytest.mark.parametrize("flag", ["--sysroot", "--cross-prefix", "--offline", "--cache", "--archives"])
def test_removed_cli_options_are_not_kept_as_hidden_aliases(flag: str) -> None:

    with pytest.raises(SystemExit, match="^2$") as error:
        cli.main(["rollout", flag, "/unused"])

    assert error.value.code == 2


def test_cli_dependency_report_is_json_and_failure_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    report = dependencies.DependencyReport((), ("make : NOT FOUND",), ("sudo apt-get install make",))
    monkeypatch.setattr(dependencies, "check", lambda *args, **kwargs: report)

    assert cli.main(["deps", "--logs_dir", f"{tmp_path / "logs"}", "--json"]) == 1

    assert json.loads(capsys.readouterr().out)["ready"] is False


@pytest.mark.parametrize("missing", [(), ("mkfs.exfat : NOT FOUND",)])
def test_card_deps_cli_reports_tools_without_initializing_a_card(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], missing: tuple[str, ...],
) -> None:
    report = dependencies.DependencyReport((), missing, ())
    monkeypatch.setattr(dependencies, "check_card", lambda: report)

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("card deps must not initialize or inspect a card")

    monkeypatch.setattr(cli, "CardInitializer", forbidden)

    assert cli.main(["card", "deps", "--logs_dir", f"{tmp_path / "logs"}", "--json"]) == int(bool(missing))

    document = json.loads(capsys.readouterr().out)
    assert document["ready"] == (not missing)
    assert document["missing"] == list(missing)


def test_sdk_cli_defaults_and_json_are_cwd_relative(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    received = []

    def run(self: SdkPreparer) -> dict[str, str]:
        received.append(self.options)

        return {"status": "SDK_PREPARED", "sdk": f"{self.options.sdk}"}

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(SdkPreparer, "run", run)

    assert cli.main(["sdk", "prepare", "--nor", "reference.bin", "--json"]) == 0

    assert received == [SdkOptions(
        tmp_path / "reference.bin", tmp_path / "in/sdk/rk3032",
        tmp_path / "in/downloads", tmp_path / "logs/sdk-prepare",
    )]
    assert json.loads(capsys.readouterr().out)["status"] == "SDK_PREPARED"
    assert not list(tmp_path.iterdir())


def test_sdk_cli_explicit_paths_do_not_trigger_rollout_discovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    received = []

    def run(self: SdkPreparer) -> dict[str, str]:
        received.append(self.options)

        return {"status": "SDK_REUSED"}

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(SdkPreparer, "run", run)

    assert cli.main([
        "sdk", "prepare", "--platform", "rk3032", "--nor", "nor", "--sdk", "custom-sdk",
        "--downloads", "archives", "--logs_dir", "reports", "--json",
    ]) == 0

    assert received == [SdkOptions(
        tmp_path / "nor", tmp_path / "custom-sdk", tmp_path / "archives", tmp_path / "reports/sdk-prepare",
    )]
    assert json.loads(capsys.readouterr().out)["status"] == "SDK_REUSED"


@pytest.mark.parametrize("arguments", [[], ["--nor", "nor", "--platform", "linux-x86_64"]])
def test_sdk_cli_requires_nor_and_only_supports_rk3032(arguments: list[str]) -> None:

    with pytest.raises(SystemExit, match="^2$") as error:
        cli.main(["sdk", "prepare", *arguments])

    assert error.value.code == 2


def test_sdk_cli_cancellation_reports_failure_without_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    def cancelled(self: SdkPreparer) -> NoReturn:
        raise KeyboardInterrupt

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(SdkPreparer, "run", cancelled)

    assert cli.main(["sdk", "prepare", "--nor", "nor", "--json"]) == 130

    assert capsys.readouterr().out == ""
