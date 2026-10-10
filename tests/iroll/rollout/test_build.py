"""Isolated build-service integration with fake compilers and real file validation/publication.

SPDX-License-Identifier: BSD-3-Clause
"""

import json
import logging
import re
from collections.abc import Callable
from dataclasses import replace
from functools import partial
from itertools import combinations
from pathlib import Path
from typing import Any

import pytest

from iroll.common import paths
from iroll.components import registry
from iroll.rollout.build import BuildOptions, Builder, build_commands
from iroll.common.errors import BuildError
from iroll.host.platforms import PROFILES, Toolchain
from iroll.components.registry import COMPONENTS, Source, select

COMPILER_FAILURES = [
    pytest.param("fail_command", BuildError, "Synthetic compiler failure", id="compiler-failure"),
    pytest.param("cancel_command", KeyboardInterrupt, "^$", id="cancellation"),
]


@pytest.mark.parametrize("content,message", [
    pytest.param("[package\n", "Invalid Cargo manifest", id="malformed-toml"),
    pytest.param("package = \"igui\"\n", "Wrong Cargo package", id="scalar-package"),
    pytest.param("package = [\"igui\"]\n", "Wrong Cargo package", id="package-list"),
    pytest.param("[package]\nname = \"another\"\n", "Wrong Cargo package", id="different-name"),
])
def test_invalid_cargo_metadata_refuses_before_compilers_or_publication(
    options: BuildOptions, runner: Any, content: str, message: str,
) -> None:
    options = replace(options, components=tuple(item for item in options.components if item.name == "igui"))
    (options.source_root / "igui/Cargo.toml").write_text(content)

    with pytest.raises(BuildError, match=message):
        Builder(options, runner=runner).run()

    assert not runner.calls
    assert not options.dest.exists()


def change_session_environment(
    options: BuildOptions, monkeypatch: pytest.MonkeyPatch, *, name: str,
) -> BuildOptions:
    monkeypatch.setenv(name, "unrelated-caller-value")
    return options


def remove_published_binaries(options: BuildOptions, monkeypatch: pytest.MonkeyPatch) -> BuildOptions:

    for component in options.components:
        (options.dest / "system" / component.destination).unlink()

    return options


@pytest.mark.parametrize("platform", ["linux-x86_64", "rk3032"])
def test_build_logs_platform_once_before_component_progress(
    options: BuildOptions, runner: Any, toolchains: dict[str, Toolchain], caplog: pytest.LogCaptureFixture,
    platform: str,
) -> None:

    options = replace(options, toolchain=toolchains[platform])

    with caplog.at_level(logging.INFO):
        Builder(options, runner=runner).run()

    messages = [record.getMessage() for record in caplog.records if record.name == "iroll.rollout.build"]
    assert messages[0] == f"Platform: {platform}"
    assert [message for message in messages if message.startswith("Platform:")] == [f"Platform: {platform}"]
    assert [message for message in messages if message.startswith("Building ")] == [
        f"Building {component.name} ..." for component in options.components
    ]
    assert messages[-1] == f"Built {len(options.components)} components into {options.dest / paths.SYSTEM_DIR}"


def test_local_cargo_component_directory_may_be_a_symlink(options: BuildOptions, runner: Any) -> None:
    source = options.source_root / "igui"
    linked_source = options.source_root.parent / "linked-igui"
    source.rename(linked_source)
    source.symlink_to(linked_source, target_is_directory=True)
    options = replace(options, components=tuple(item for item in options.components if item.name == "igui"))

    Builder(options, runner=runner).run()

    assert (options.dest / "system/igui/igui").is_file()


def test_all_components_are_staged_before_publication_without_launching(
    options: BuildOptions, runner: Any,
) -> None:
    options.dest.mkdir()
    (options.dest / "prefs.json").write_bytes(b"untouched preferences")
    games = options.dest / "games"
    games.mkdir()
    (games / "rom.nes").write_bytes(b"untouched ROM")

    report_dir = Builder(options, runner=runner).run()

    system = options.dest / "system"
    manifest = json.loads((system / "iman/emulation.json").read_text())
    state = json.loads((report_dir / "result.json").read_text())
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z(?:_\d{2})?", report_dir.name)
    assert manifest["architecture"] == "x86_64"
    assert set(manifest["cores"]) == {
        platform for component in options.components for platform in component.game_platforms
    }
    assert set(state["components"]) == {component.name for component in options.components}
    assert state["hardware_verified"] is False
    assert (system / "igui/locales/ru.json").read_text() == "{}\n"

    for name in ("2563-0555-hidraw.json", "20bc-5500-evdev.json", "2563-0555-evdev.json"):
        assert (system / "igui/controls" / name).read_text() == "{}\n"
        assert f"igui/controls/{name}" in state["components"]["igui"]["files"]

    assert not (system / "igui/profiles").exists()
    assert not (system / "licenses").exists()
    assert not (system / "iroll.json").exists()
    assert not (options.dest / ".iroll.lock").exists()
    assert not (options.downloads / ".iroll.lock").exists()
    assert all((system / component.destination).is_file() for component in options.components)
    assert (system / "igui/igui").stat().st_mode & 0o777 == 0o755
    assert (system / "retroarch/cores/nestopia_libretro.so").stat().st_mode & 0o777 == 0o644
    assert (options.dest / "prefs.json").read_bytes() == b"untouched preferences"
    assert (games / "rom.nes").read_bytes() == b"untouched ROM"
    assert not (options.build_dir / "out_stage").exists()
    assert not list(options.dest.glob(".iroll-stage-*"))
    assert not list(options.dest.glob(".iroll-backup-*"))
    assert json.loads((report_dir / "result.json").read_text())["applications_launched"] is False
    assert {argv[0] for argv, _, _ in runner.calls} <= {"rustc", "pkg-config", "cargo", "sh", "make", "cc", "c++"}
    cargo = [argv for argv, _, _ in runner.calls if argv[0] == "cargo"]
    assert all("--offline" in argv and "--locked" in argv and "--release" in argv for argv in cargo)
    assert all("--features" not in argv for argv in cargo)
    assert report_dir.parent == options.logs
    cargo_target = options.build_dir / "build/our" / options.toolchain.profile.name
    assert all(argv[argv.index("--target-dir") + 1] == f"{cargo_target}" for argv in cargo)
    assert not list(options.downloads.iterdir())
    assert not (options.build_dir / ".iroll.lock").exists()
    assert not (options.logs / ".iroll.lock").exists()


def test_retroarch_is_built_and_published_without_dependency_patches(options: BuildOptions, runner: Any) -> None:
    options = replace(options, components=tuple(item for item in options.components if item.name == "retro"))

    Builder(options, runner=runner).run()

    system = options.dest / "system"
    calls = [argv for argv, _, _ in runner.calls if argv[0] == "patch"]
    assert calls == []
    report = next(options.logs.glob("*/result.json"))
    record = json.loads(report.read_text())["components"]["retro"]
    assert record["source"]["sha256"] == options.components[0].source.sha256
    assert not (system / "licenses").exists()
    assert not any(name.endswith(".patch") for name in record["files"])


def test_offline_build_finds_archives_in_downloads(options: BuildOptions, runner: Any) -> None:
    options.downloads.mkdir()

    for component in (item for item in options.components if item.source is not None):
        (options.archives / component.source.archive).rename(options.downloads / component.source.archive)

        for module in component.submodules:
            (options.archives / module.source.archive).rename(options.downloads / module.source.archive)

    options = replace(options, archives=None)
    before = {
        path.name: (path.read_bytes(), path.stat().st_ino, path.stat().st_mtime_ns)
        for path in options.downloads.iterdir()
    }

    Builder(options, runner=runner).run()

    assert options.archives == options.downloads
    assert {
        path.name: (path.read_bytes(), path.stat().st_ino, path.stat().st_mtime_ns)
        for path in options.downloads.iterdir()
    } == before
    assert all(
        (options.downloads / component.source.archive).is_file()
        for component in options.components if component.source is not None
    )
    assert (options.dest / "system/retroarch/retroarch").is_file()


def test_normal_build_reuses_archives_without_network_and_allows_cargo_registry_fallback(
    options: BuildOptions, runner: Any,
) -> None:
    options = replace(options, offline=False)

    Builder(options, runner=runner).run()

    cargo = [argv for argv, _, _ in runner.calls if argv[0] == "cargo"]
    assert all("--locked" in argv and "--offline" not in argv for argv in cargo)
    assert not any(
        (options.downloads / component.source.archive).exists() for component in options.components if component.source
    )


@pytest.mark.parametrize("command", ["cargo", "sh", "make"])
@pytest.mark.parametrize("failure_field,error_type,message", COMPILER_FAILURES)
def test_build_failure_or_cancel_preserves_destination_and_cleans_work(
    options: BuildOptions, runner: Any, command: str, failure_field: str,
    error_type: type[BaseException], message: str,
) -> None:
    system = options.dest / "system"
    system.mkdir(parents=True)
    (system / "keep.txt").write_bytes(b"original")

    setattr(runner, failure_field, command)

    with pytest.raises(error_type, match=message):
        Builder(options, runner=runner).run()

    assert {path.name for path in system.iterdir()} == {"keep.txt"}
    assert (system / "keep.txt").read_bytes() == b"original"
    assert not (options.build_dir / "out_stage").exists()
    reports = list(options.logs.glob("*/failure.json"))
    assert len(reports) == 1
    assert json.loads(reports[0].read_text())["automatic_retry"] is False


def test_missing_dependency_stops_before_writes(options: BuildOptions, runner: Any) -> None:
    runner.missing_libraries.add("x11")

    with pytest.raises(BuildError, match="x11 : NOT FOUND"):
        Builder(options, runner=runner).run()

    assert not options.downloads.exists()
    assert not options.build_dir.exists()
    assert not options.logs.exists()
    assert not options.dest.exists()
    assert not any(argv[0] in ("cargo", "make", "sh") for argv, _, _ in runner.calls)


@pytest.mark.parametrize("change_options,message", [
    pytest.param(
        lambda options: replace(options, source_root=Path("relative")), "requires absolute", id="relative-source",
    ),
    pytest.param(
        lambda options: replace(options, downloads=options.dest / "downloads"), "overlap unsafely", id="downloads",
    ),
    pytest.param(
        lambda options: replace(options, build_dir=Path("relative")), "requires absolute", id="relative-build",
    ),
    pytest.param(lambda options: replace(options, logs=Path("relative")), "requires absolute", id="relative-logs"),
    pytest.param(lambda options: replace(options, dest=options.source_root), "overlap unsafely", id="destination"),
    pytest.param(lambda options: replace(options, jobs=0), "--jobs must be between", id="invalid-jobs"),
    pytest.param(
        lambda options: replace(options, toolchain=Toolchain(PROFILES["rk3032"])), "requires --sdk", id="missing-sdk",
    ),
])
def test_invalid_options_stop_before_commands(
    options: BuildOptions, runner: Any, change_options: Callable[[BuildOptions], BuildOptions], message: str,
) -> None:
    options = change_options(options)

    with pytest.raises(BuildError, match=message):
        Builder(options, runner=runner).run()

    assert not any(argv[0] in ("cargo", "make", "sh") for argv, _, _ in runner.calls)


def test_core_only_build_then_retro_build_creates_complete_runtime_manifest(
    options: BuildOptions, runner: Any,
) -> None:
    cores = tuple(component for component in options.components if component.core)
    retro = tuple(component for component in options.components if component.name == "retro")

    Builder(replace(options, components=cores), runner=runner).run()

    system = options.dest / "system"
    assert not (system / "iman/emulation.json").exists()
    assert (system / "retroarch/cores/nestopia_libretro.so").is_file()

    Builder(replace(options, components=retro), runner=runner).run()

    document = json.loads((system / "iman/emulation.json").read_text())
    assert "version" not in document
    assert set(document["cores"]) == {
        platform for component in options.components for platform in component.game_platforms
    }
    assert document["retroarch"]["path"] == "retroarch/retroarch"


def test_changed_selected_files_and_broken_manifest_are_replaced(options: BuildOptions, runner: Any) -> None:
    Builder(options, runner=runner).run()
    system = options.dest / "system"
    (system / "retroarch/cores/nestopia_libretro.so").write_bytes(b"modified")
    (system / "iman/emulation.json").write_bytes(b"user-edited invalid manifest")
    (system / "igui/igui").write_bytes(b"not an ELF")

    Builder(options, runner=runner).run()

    assert (system / "igui/igui").read_bytes().startswith(b"\x7fELF")
    document = json.loads((system / "iman/emulation.json").read_text())
    assert set(document["cores"]) == {
        platform for component in options.components for platform in component.game_platforms
    }


@pytest.mark.parametrize("no_diag", [False, True])
def test_all_arm_recipes_include_board_gui_and_manager(
    arm_options: BuildOptions, runner: Any, no_diag: bool,
) -> None:
    options = replace(arm_options, no_diag=no_diag)
    sdk = options.toolchain.sysroot

    Builder(options, runner=runner).run()

    system = options.dest / paths.SYSTEM_DIR
    assert (system / "igui/igui").is_file()
    assert json.loads((system / paths.RUNTIME_MANIFEST).read_text())["architecture"] == "arm"
    profile = json.loads((system / "iman/iman.json").read_text())
    assert profile["gui"]["display"]["backend"] == "drm"
    assert "read_only" not in profile["gui"]
    assert "retroarch" not in profile
    settings = json.loads((system / paths.RUNTIME_MANIFEST).read_text())["settings"]
    assert settings["audio_driver"] == "alsa"
    assert settings["audio_latency"] == 232
    assert settings["video_context_driver"] == "kms"
    assert settings["video_autoswitch_refresh_rate"] == 3

    for player in (1, 2):
        assert settings[f"input_player{player}_joypad_index"] == player - 1
        assert settings[f"input_libretro_device_p{player}"] == 1

        for name in ("a", "b", "x", "y", "start", "select", "up", "down", "left", "right"):
            assert f"input_player{player}_{name}_btn" not in settings
            assert settings[f"input_player{player}_{name}"] == "nul"

    assert settings["input_autodetect_enable"] is True
    assert settings["input_menu_toggle_gamepad_combo"] == 4

    for name in ("0810-0001.cfg", "20bc-5500.cfg"):
        assert (system / "retroarch/padconf" / name).is_file()

    for name in ("menu_toggle", "hold_fast_forward", "rewind", "cheat_index_plus", "save_state", "load_state"):
        assert settings[f"input_{name}"] == "nul"

    assert settings["config_save_on_exit"] is False
    assert profile["gui"]["display"]["allow_vendor_einval"] is True
    assert not (system / "BOX2-ALLOW.txt").exists()
    assert not (system / "BOX2-EXPORT.txt").exists()
    builds = [(argv, env) for argv, _, env in runner.calls if argv[0] in ("cargo", "make", "sh")]

    for argv, env in (call for call in builds if call[0][0] == "cargo"):
        assert argv[argv.index("--target") + 1] == "armv7-unknown-linux-musleabihf"
        assert "-Ctarget-feature=+crt-static" in env["CARGO_ENCODED_RUSTFLAGS"]
        assert "-Clinker=rust-lld" in env["CARGO_ENCODED_RUSTFLAGS"]
        assert "PKG_CONFIG_SYSROOT_DIR" not in env

    for _, env in (call for call in builds if call[0][0] != "cargo"):
        assert env["PKG_CONFIG_SYSROOT_DIR"] == f"{sdk}"
        assert env["PKG_CONFIG_PATH"] == ""
        assert all(path.startswith(f"{sdk}") for path in env["PKG_CONFIG_LIBDIR"].split(":"))
        assert "-mcpu=cortex-a7" in env["CFLAGS"]

    cargo = [argv for argv, _ in builds if argv[0] == "cargo"]
    assert all("--release" in argv for argv in cargo)
    assert "--no-default-features" in cargo[0]
    assert cargo[0][cargo[0].index("--features") + 1] == "drm,rgb565"
    assert cargo[1][cargo[1].index("--features") + 1] == ("drm" if no_diag else "drm,diagnostics")
    configure = next(argv for argv, _ in builds if argv[0] == "sh")
    assert "--enable-kms" in configure and "--disable-x11" in configure


@pytest.mark.parametrize("platform", ["linux-x86_64", "rk3032"])
def test_no_diag_leaves_other_components_and_native_commands_unchanged(
    options: BuildOptions, platform: str,
) -> None:
    options = replace(options, toolchain=Toolchain(PROFILES[platform]))
    minimal = replace(options, no_diag=True)
    cargo_target = options.build_dir / "build/our" / platform
    payload = options.build_dir / "out_stage/system"

    unchanged = [
        component for component in options.components if not (platform == "rk3032" and component.name == "iman")
    ]

    for component in unchanged:
        source = options.source_root / component.directory
        normal_commands = build_commands(component, options, source, cargo_target, payload)
        minimal_commands = build_commands(component, minimal, source, cargo_target, payload)

        assert minimal_commands == normal_commands


def test_arm_rust_only_does_not_require_glibc_sdk(options: BuildOptions, runner: Any) -> None:
    options = replace(
        options, toolchain=Toolchain(PROFILES["rk3032"]),
        components=tuple(item for item in options.components if item.recipe == "cargo"),
    )

    Builder(options, runner=runner).run()

    assert (options.dest / "system/igui/igui").is_file()
    assert (options.dest / "system/iman/iman.json").is_file()
    assert not any(argv[0] == "pkg-config" for argv, _, _ in runner.calls)


def test_new_make_core_reuses_recipe_and_selection_without_special_build_code(
    options: BuildOptions, monkeypatch: pytest.MonkeyPatch,
) -> None:
    core = replace(
        next(component for component in COMPONENTS if component.name == "core-pcsx-rearmed"),
        name="core-new", artifact="new.so", destination="retroarch/cores/new.so", game_platforms=("NEW",)
    )
    monkeypatch.setattr(registry, "COMPONENTS", (*COMPONENTS, core))

    selected = select("linux-x86_64", [], cores=True)
    payload = options.build_dir / "out_stage/system"
    argv, output = build_commands(core, options, options.source_root, options.build_dir / "build/our", payload)

    assert core in selected
    assert argv[0][:1] == ["make"]
    assert output == payload / "retroarch/cores/new.so"
    assert f"TARGET={output}" in argv[0]


@pytest.mark.parametrize("platform", ["linux-x86_64", "rk3032"])
@pytest.mark.parametrize("names", [("custom-usb.json",), ("custom-usb.json", "other-evdev.json")])
def test_arbitrary_controls_are_published_without_named_device_requirements(
    options: BuildOptions, runner: Any, platform: str, names: tuple[str, ...],
) -> None:
    options = replace(
        options, toolchain=Toolchain(PROFILES[platform]),
        components=tuple(item for item in options.components if item.name == "igui"),
    )
    directory = options.source_root / "igui/controls"

    for path in directory.iterdir():
        path.unlink()

    contents = {name: f"{json.dumps({"synthetic": name})}\n" for name in names}

    for name, content in contents.items():
        (directory / name).write_text(content)

    report_dir = Builder(options, runner=runner).run()

    installed = options.dest / "system/igui/controls"
    assert {path.name: path.read_text() for path in installed.iterdir()} == contents
    record = json.loads((report_dir / "result.json").read_text())["components"]["igui"]
    assert {name for name in record["files"] if name.startswith("igui/controls/")} == {
        f"igui/controls/{name}" for name in names
    }


@pytest.mark.parametrize("damage", [
    pytest.param(Path.rmdir, id="missing"),
    pytest.param(lambda path: None, id="empty"),
    pytest.param(lambda path: (path / "README.txt").write_text("No profiles here.\n"), id="non-json"),
])
def test_controls_without_json_refuse_publication(
    options: BuildOptions, runner: Any, damage: Callable[[Path], object],
) -> None:
    options = replace(options, components=tuple(item for item in options.components if item.name == "igui"))
    directory = options.source_root / "igui/controls"

    for path in directory.iterdir():
        path.unlink()

    damage(directory)

    with pytest.raises(BuildError, match="No JSON resource files found: .*controls"):
        Builder(options, runner=runner).run()

    assert not options.dest.exists()


@pytest.mark.parametrize("resource", ["controls", "locales"])
def test_json_resource_symlinks_are_rejected_without_touching_target(
    options: BuildOptions, runner: Any, resource: str,
) -> None:
    directory = options.source_root / "igui" / resource
    outside = options.source_root / "outside.json"
    outside.write_bytes(b"untouched")
    (directory / "extra.json").symlink_to(outside)

    with pytest.raises(BuildError, match="Symlink"):
        Builder(options, runner=runner).run()

    assert outside.read_bytes() == b"untouched"
    assert not options.dest.exists()


@pytest.mark.parametrize(
    "left,right,relation",
    [
        (left, right, relation)
        for left, right in combinations(("dest", "downloads", "build_dir", "logs"), 2)
        for relation in ("equal", "child", "parent")
        if (left, right, relation) != ("build_dir", "logs", "child")
    ],
)
def test_data_and_destination_roots_must_not_overlap(
    options: BuildOptions, runner: Any, left: str, right: str, relation: str,
) -> None:
    parent = getattr(options, left)

    if relation == "equal":
        options = replace(options, **{right: parent})

    elif relation == "child":
        options = replace(options, **{right: parent / "nested"})

    else:
        options = replace(options, **{right: parent, left: parent / "nested"})

    with pytest.raises(BuildError, match="overlap unsafely"):
        Builder(options, runner=runner).run()

    assert not parent.exists()
    assert not runner.calls


@pytest.mark.parametrize("relative", ["logs", "reports/logs"])
def test_logs_inside_build_root_survive_work_cleanup(
    options: BuildOptions, runner: Any, relative: str,
) -> None:
    options = replace(options, logs=options.build_dir / relative)

    report = Builder(options, runner=runner).run()

    assert report.parent == options.logs
    assert (report / "result.json").is_file()
    assert (options.dest / "system/iman/iman").is_file()
    assert not (options.build_dir / "out_stage").exists()


@pytest.mark.parametrize("relative", ["logs", "reports/logs"])
@pytest.mark.parametrize(("failure_field", "error_type", "message"), COMPILER_FAILURES)
def test_failure_logs_inside_build_root_survive_work_cleanup(
    options: BuildOptions, runner: Any, relative: str,
    failure_field: str, error_type: type[BaseException], message: str,
) -> None:
    options = replace(options, logs=options.build_dir / relative)
    setattr(runner, failure_field, "cargo")

    with pytest.raises(error_type, match=message):
        Builder(options, runner=runner).run()

    assert len(list(options.logs.glob("*/failure.json"))) == 1
    assert not options.dest.exists()
    assert not (options.build_dir / "out_stage").exists()


@pytest.mark.parametrize(
    "relative",
    ["build", "build/logs", "build/our", "build/their/src", "out_stage", "out_stage/logs", "logs/../build"],
)
def test_logs_cannot_overlap_reserved_build_subdirectories(
    options: BuildOptions, runner: Any, relative: str,
) -> None:
    options = replace(options, logs=options.build_dir / relative)

    with pytest.raises(BuildError, match="overlap unsafely"):
        Builder(options, runner=runner).run()

    assert not options.build_dir.exists()
    assert not options.downloads.exists()
    assert not options.dest.exists()
    assert not runner.calls


@pytest.mark.parametrize("which", ["downloads", "build_dir", "logs"])
@pytest.mark.parametrize("relation", ["equal", "parent"])
def test_build_data_cannot_be_source_root_or_its_ancestor(
    options: BuildOptions, runner: Any, which: str, relation: str,
) -> None:
    path = options.source_root if relation == "equal" else options.source_root.parent
    options = replace(options, **{which: path})

    with pytest.raises(BuildError, match="overlap unsafely"):
        Builder(options, runner=runner).run()

    assert not options.dest.exists()
    assert not runner.calls


def test_parent_segments_cannot_hide_build_downloads_overlap(options: BuildOptions, runner: Any) -> None:
    options = replace(options, build_dir=options.downloads / "unused" / "..")

    with pytest.raises(BuildError, match="overlap unsafely"):
        Builder(options, runner=runner).run()

    assert not options.downloads.exists()
    assert not runner.calls


@pytest.mark.parametrize(
    "which", [
        "downloads", "build_dir", "logs", "build", "our", "our_platform", "their", "src", "compiled",
        "compiled_platform", "out_stage",
    ],
)
def test_build_data_symlinks_are_rejected_before_commands(
    options: BuildOptions, runner: Any, tmp_path: Path, which: str,
) -> None:
    locations = {
        "downloads": options.downloads,
        "build_dir": options.build_dir,
        "logs": options.logs,
        "build": options.build_dir / "build",
        "our": options.build_dir / "build/our",
        "our_platform": options.build_dir / "build/our" / options.toolchain.profile.name,
        "their": options.build_dir / "build/their",
        "src": options.build_dir / "build/their/src",
        "compiled": options.build_dir / "build/their/compiled",
        "compiled_platform": options.build_dir / "build/their/compiled" / options.toolchain.profile.name,
        "out_stage": options.build_dir / "out_stage",
    }
    outside = tmp_path / "outside"
    outside.mkdir()
    path = locations[which]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.symlink_to(outside, target_is_directory=True)

    with pytest.raises(BuildError, match="Symlink"):
        Builder(options, runner=runner).run()

    assert not list(outside.iterdir())
    assert not options.dest.exists()
    assert not runner.calls


@pytest.mark.parametrize("which", ["build_dir", "logs"])
def test_build_data_roots_must_be_directories(options: BuildOptions, runner: Any, which: str) -> None:
    path = getattr(options, which)
    path.write_bytes(b"untouched")

    with pytest.raises(BuildError, match="not a directory"):
        Builder(options, runner=runner).run()

    assert path.read_bytes() == b"untouched"
    assert not options.downloads.exists()
    assert not runner.calls


@pytest.mark.parametrize("name", [
    "build with spaces", "build;bad", "build$bad", "build#bad", "build'bad", "build`bad", "build:bad",
])
def test_make_rejects_unsafe_build_directory_before_commands(
    options: BuildOptions, runner: Any, name: str,
) -> None:
    options = replace(options, build_dir=options.build_dir.with_name(name))

    with pytest.raises(BuildError, match="build path without whitespace"):
        Builder(options, runner=runner).run()

    assert not options.build_dir.exists()
    assert not options.downloads.exists()
    assert not runner.calls


def test_archives_and_logs_allow_spaces_when_make_work_path_is_safe(
    options: BuildOptions, runner: Any,
) -> None:
    options = replace(
        options,
        downloads=options.downloads.with_name("source archives"),
        logs=options.logs.with_name("build logs"),
    )

    report = Builder(options, runner=runner).run()

    assert report.parent == options.logs
    assert (report / "result.json").is_file()
    assert not list(options.downloads.iterdir())
    assert not (options.build_dir / "out_stage").exists()


def test_data_directories_may_be_separate_children_of_source_root(
    options: BuildOptions, runner: Any,
) -> None:
    options = replace(
        options,
        downloads=options.source_root / "downloads",
        build_dir=options.source_root / "build",
        logs=options.source_root / "logs",
    )

    report = Builder(options, runner=runner).run()

    assert report.parent == options.logs
    assert (options.dest / "system/igui/igui").is_file()
    assert not list(options.downloads.iterdir())


def test_make_outputs_are_separated_and_linked_directly_into_stage(
    options: BuildOptions, runner: Any,
) -> None:
    Builder(options, runner=runner).run()

    platform = options.toolchain.profile.name

    for component in (item for item in options.components if item.source is not None):
        source = options.build_dir / "build/their/src" / component.name
        compiled = options.build_dir / "build/their/compiled" / platform / component.name
        cwd = compiled / component.directory
        assert (source / "LICENSE").is_file()
        assert not (source / "LICENSE").is_symlink()
        assert (compiled / "LICENSE").is_symlink()
        assert (compiled / "LICENSE").resolve() == source / "LICENSE"
        assert (cwd / "synthetic.o").is_file()
        assert not (cwd / "synthetic.o").is_symlink()
        assert not (cwd / component.artifact).exists()
        make = next(argv for argv, directory, _ in runner.calls if directory == cwd and argv[0] == "make")
        target = options.build_dir / "out_stage/system" / component.destination
        assert f"TARGET={target}" in make
        assert not list(source.rglob("*.o"))
        assert not (source / "config.h").exists()

    for component in (item for item in options.components if item.recipe == "configure"):
        compiled = options.build_dir / "build/their/compiled" / platform / component.name

        for name in ("config.h", "config.mk", "config.log"):
            assert (compiled / name).is_file()
            assert not (compiled / name).is_symlink()

    assert not (options.build_dir / "out_stage").exists()
    assert not (options.build_dir / "work").exists()
    assert not (options.build_dir / "cargo").exists()


def test_changed_recipe_keeps_sources_but_discards_only_selected_compiled_tree(
    options: BuildOptions, runner: Any,
) -> None:
    options = replace(options, components=tuple(item for item in options.components if item.source))
    Builder(options, runner=runner).run()
    source_files = list((options.build_dir / "build/their/src").rglob("LICENSE"))
    before = {path: (path.read_bytes(), path.stat().st_ino, path.stat().st_mtime_ns) for path in source_files}
    selected, *others = options.components
    root = options.build_dir / "build/their/compiled" / options.toolchain.profile.name
    stale = root / selected.name / "stale.o"
    stale.write_bytes(b"stale")
    untouched = [root / component.name / "keep.o" for component in others]

    for path in untouched:
        path.write_bytes(b"untouched")

    selected = replace(selected, makefile="another-Makefile")
    Builder(replace(options, components=(selected,)), runner=runner).run()

    assert not stale.exists()
    assert all(path.read_bytes() == b"untouched" for path in untouched)
    assert {path: (path.read_bytes(), path.stat().st_ino, path.stat().st_mtime_ns) for path in source_files} == before


def test_unfinished_stage_is_not_deleted_or_reused(options: BuildOptions, runner: Any) -> None:
    stage = options.build_dir / "out_stage"
    stage.mkdir(parents=True)
    (stage / "keep").write_bytes(b"interrupted")

    with pytest.raises(BuildError, match="Unfinished output staging"):
        Builder(options, runner=runner).run()

    assert (stage / "keep").read_bytes() == b"interrupted"
    assert not options.dest.exists()
    assert not options.logs.exists()
    assert not runner.calls


def test_unchanged_make_build_reuses_objects_and_configuration(
    options: BuildOptions, runner: Any, caplog: pytest.LogCaptureFixture,
) -> None:
    options = replace(options, components=tuple(item for item in options.components if item.source))
    builder = Builder(options, runner=runner)
    builder.run()
    root = options.build_dir / "build/their/compiled" / options.toolchain.profile.name
    cached = [*root.rglob("synthetic.o"), *root.rglob("config.h"), *root.rglob("config.mk")]
    before = {path: (path.stat().st_ino, path.stat().st_mtime_ns) for path in cached}
    runner.calls.clear()

    with caplog.at_level(logging.INFO):
        builder.run()

    assert not any(argv[:2] == ["sh", "./configure"] for argv, _, _ in runner.calls)
    assert sum(argv[0] == "make" for argv, _, _ in runner.calls) == len(options.components)
    assert {path: (path.stat().st_ino, path.stat().st_mtime_ns) for path in cached} == before
    assert len([
        record for record in caplog.records if "Reusing compiled files:" in record.getMessage()
    ]) == len(options.components)
    assert all((options.dest / "system" / component.destination).is_file() for component in options.components)
    assert not (options.build_dir / "out_stage").exists()


@pytest.mark.parametrize("platform", ["linux-x86_64", "rk3032"])
@pytest.mark.parametrize("change,modify", [
    *[
        pytest.param(name, partial(change_session_environment, name=name), id=name)
        for name in ("TERM", "XDG_SESSION_ID", "SSH_AUTH_SOCK", "MAKEFILES")
    ],
    pytest.param("jobs", lambda options, patch: replace(options, jobs=options.jobs + 1), id="jobs"),
    pytest.param("binary_removed", remove_published_binaries, id="binary-removed"),
    pytest.param(
        "destination", lambda options, patch: replace(options, dest=options.dest.with_name("another-output")),
        id="destination",
    ),
])
def test_make_cache_survives_session_jobs_and_output_changes(
    options: BuildOptions, runner: Any, sdk: Path, monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture, platform: str, change: str,
    modify: Callable[[BuildOptions, pytest.MonkeyPatch], BuildOptions],
) -> None:
    toolchains = {
        "linux-x86_64": options.toolchain,
        "rk3032": Toolchain(PROFILES["rk3032"], sdk, "/sdk/bin/arm-linux-gnueabihf-"),
    }
    options = replace(
        options, components=tuple(item for item in options.components if item.source), toolchain=toolchains[platform],
    )

    Builder(options, runner=runner).run()
    root = options.build_dir / "build/their/compiled" / platform
    cached = [*root.rglob("synthetic.o"), *root.rglob("config.h"), *root.rglob("config.mk")]
    before = {path: (path.stat().st_ino, path.stat().st_mtime_ns) for path in cached}

    options = modify(options, monkeypatch)
    runner.calls.clear()

    with caplog.at_level(logging.INFO):
        Builder(options, runner=runner).run()

    assert not any(argv[:2] == ["sh", "./configure"] for argv, _, _ in runner.calls)
    makes = [(argv, env) for argv, _, env in runner.calls if argv[0] == "make"]
    assert len(makes) == len(options.components)
    assert all(argv[argv.index("-j") + 1] == f"{options.jobs}" for argv, _ in makes)
    assert all(change not in env for _, env in makes)
    assert {path: (path.stat().st_ino, path.stat().st_mtime_ns) for path in cached} == before
    assert "Build cache miss:" not in caplog.text
    assert all((options.dest / "system" / component.destination).is_file() for component in options.components)


def test_changed_submodule_rebuilds_only_its_component(
    options: BuildOptions, runner: Any, archive: Callable[..., tuple[Path, Source]], caplog: pytest.LogCaptureFixture,
) -> None:
    options = replace(options, components=tuple(item for item in options.components if item.source))
    Builder(options, runner=runner).run()
    root = options.build_dir / "build/their/compiled" / options.toolchain.profile.name
    selected = next(component for component in options.components if component.name == "core-picodrive")
    untouched = [path for path in root.rglob("synthetic.o") if selected.name not in path.parts]
    before = {path: (path.stat().st_ino, path.stat().st_mtime_ns) for path in untouched}
    stale = root / selected.name / "stale.o"
    stale.write_bytes(b"object from previous submodule")
    _, pin = archive({"root/module.c": b"changed synthetic submodule"})
    updated = replace(selected.submodules[0], source=pin)
    selected = replace(selected, submodules=(updated, *selected.submodules[1:]))

    with caplog.at_level(logging.INFO):
        Builder(replace(options, components=(selected,)), runner=runner).run()

    assert "sources changed" in caplog.text
    assert not stale.exists()
    assert {path: (path.stat().st_ino, path.stat().st_mtime_ns) for path in untouched} == before


def test_incompatible_make_cache_format_rebuilds_once_without_replacing_sources(
    options: BuildOptions, runner: Any, caplog: pytest.LogCaptureFixture,
) -> None:
    options = replace(options, components=tuple(item for item in options.components if item.name == "retro"))
    Builder(options, runner=runner).run()
    root = options.build_dir / "build/their"
    compiled = root / "compiled" / options.toolchain.profile.name / "retro"
    marker = compiled / ".iroll-build.json"
    current = json.loads(marker.read_text())
    # Synthetic incompatible state, not a description of a historical cache schema.
    marker.write_text(json.dumps({"version": current["version"] + 1}))
    stale = compiled / "stale.o"
    stale.write_bytes(b"old object")
    source = root / "src/retro/LICENSE"
    before = (source.stat().st_ino, source.stat().st_mtime_ns)

    with caplog.at_level(logging.INFO):
        Builder(options, runner=runner).run()

    assert "cache format changed" in caplog.text
    assert not stale.exists()
    assert json.loads(marker.read_text()) == current
    assert (source.stat().st_ino, source.stat().st_mtime_ns) == before
    runner.calls.clear()
    caplog.clear()

    with caplog.at_level(logging.INFO):
        Builder(options, runner=runner).run()

    assert "Reusing compiled files:" in caplog.text
    assert "Build cache miss:" not in caplog.text
    assert not any(argv[:2] == ["sh", "./configure"] for argv, _, _ in runner.calls)


@pytest.mark.parametrize("mutate,reason", [
    pytest.param(lambda options, runner, compiled, patch: replace(
        options, toolchain=replace(options.toolchain, profile=replace(
            options.toolchain.profile, configure=(*options.toolchain.profile.configure, "--disable-extra"),
        )),
    ), "profile changed", id="flags"),
    pytest.param(lambda options, runner, compiled, patch: (
        patch.setenv("SOURCE_DATE_EPOCH", "100"), options,
    )[-1], "environment changed", id="environment"),
    pytest.param(lambda options, runner, compiled, patch: (
        (runner.headers / "changed-file").write_bytes(b"new input"), options,
    )[-1], "toolchain changed", id="headers"),
    pytest.param(lambda options, runner, compiled, patch: (
        (runner.libraries / "changed-file").write_bytes(b"new input"), options,
    )[-1], "toolchain changed", id="libraries"),
    pytest.param(lambda options, runner, compiled, patch: (
        (options.build_dir.parent / "tools/cc").write_bytes(b"replacement compiler"), options,
    )[-1], "toolchain changed", id="compiler"),
    pytest.param(lambda options, runner, compiled, patch: (
        (compiled / "config.h").unlink(), options,
    )[-1], "required configuration missing", id="config"),
    pytest.param(lambda options, runner, compiled, patch: (
        (compiled / ".iroll-build.json").write_text("incomplete JSON"), options,
    )[-1], "missing or invalid success marker", id="marker"),
])
def test_changed_inputs_or_missing_configuration_rebuild_component(
    options: BuildOptions, runner: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
    mutate: Callable[[BuildOptions, Any, Path, pytest.MonkeyPatch], BuildOptions], reason: str,
) -> None:
    options = replace(options, components=tuple(item for item in options.components if item.name == "retro"))
    Builder(options, runner=runner).run()
    compiled = options.build_dir / "build/their/compiled" / options.toolchain.profile.name / "retro"
    stale = compiled / "stale.o"
    stale.write_bytes(b"old")
    options = mutate(options, runner, compiled, monkeypatch)
    runner.calls.clear()

    with caplog.at_level(logging.INFO):
        Builder(options, runner=runner).run()

    assert not stale.exists()
    assert any(argv[:2] == ["sh", "./configure"] for argv, _, _ in runner.calls)
    assert reason in caplog.text


def test_changed_archive_replaces_compiled_links_and_previous_sources(
    options: BuildOptions, runner: Any, archive: Callable[..., tuple[Path, Source]],
) -> None:
    selected = next(component for component in options.components if component.name == "retro")
    options = replace(options, components=(selected,))
    Builder(options, runner=runner).run()
    _, pin = archive({"root/LICENSE": b"new source", "root/_artifact-name": selected.artifact.encode()})
    updated = replace(selected, source=pin)

    Builder(replace(options, components=(updated,)), runner=runner).run()

    root = options.build_dir / "build/their"
    assert {path.name for path in (root / "src").iterdir()} == {"retro", "retro.json"}
    assert (root / "src/retro/LICENSE").read_bytes() == b"new source"
    assert (root / "compiled" / options.toolchain.profile.name / "retro/LICENSE").resolve() == (
        root / "src/retro/LICENSE"
    )


@pytest.mark.parametrize("failure_field,error_type,message", COMPILER_FAILURES)
def test_interrupted_reused_build_discards_success_marker_and_rebuilds_next_time(
    options: BuildOptions, runner: Any, failure_field: str, error_type: type[BaseException], message: str,
) -> None:
    options = replace(options, components=tuple(item for item in options.components if item.name == "retro"))
    Builder(options, runner=runner).run()
    compiled = options.build_dir / "build/their/compiled" / options.toolchain.profile.name / "retro"
    (compiled / "partial.o").write_bytes(b"unfinished")

    setattr(runner, failure_field, "make")

    with pytest.raises(error_type, match=message):
        Builder(options, runner=runner).run()

    assert not (compiled / ".iroll-build.json").exists()

    runner.cancel_command = runner.fail_command = None
    runner.calls.clear()

    Builder(options, runner=runner).run()

    assert not (compiled / "partial.o").exists()
    assert any(argv[:2] == ["sh", "./configure"] for argv, _, _ in runner.calls)
    assert (compiled / ".iroll-build.json").is_file()


def test_make_state_does_not_store_environment_secrets(
    options: BuildOptions, runner: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    options = replace(options, components=tuple(item for item in options.components if item.name == "retro"))
    monkeypatch.setenv("TEST_SECRET", "private-value-must-not-be-saved")
    monkeypatch.setenv("HOME", "/private-value-must-not-be-saved")
    Builder(options, runner=runner).run()
    monkeypatch.setenv("HOME", "/another-private-value-must-not-be-saved")

    with caplog.at_level(logging.INFO):
        Builder(options, runner=runner).run()

    marker = options.build_dir / "build/their/compiled" / options.toolchain.profile.name / "retro/.iroll-build.json"
    assert "private-value" not in f"{marker.read_text()}{caplog.text}"
    assert "TEST_SECRET" not in f"{marker.read_text()}{caplog.text}"
    assert "environment changed" in caplog.text
    assert any(env.get("HOME") == "/another-private-value-must-not-be-saved" for _, _, env in runner.calls)


def test_sdk_change_rebuilds_only_selected_platform(options: BuildOptions, runner: Any, sdk: Path) -> None:
    options = replace(options, components=tuple(item for item in options.components if item.name == "retro"))
    Builder(options, runner=runner).run()
    native = options.build_dir / "build/their/compiled/linux-x86_64/retro/config.h"
    before = native.stat().st_mtime_ns
    options = replace(
        options, dest=options.dest.with_name("arm-output"),
        toolchain=Toolchain(PROFILES["rk3032"], sdk, "/sdk/bin/arm-linux-gnueabihf-"),
    )
    Builder(options, runner=runner).run()
    compiled = options.build_dir / "build/their/compiled/rk3032/retro"
    (compiled / "old.o").write_bytes(b"old")
    (sdk / "changed-header.h").write_bytes(b"new SDK header")
    runner.calls.clear()

    Builder(options, runner=runner).run()

    assert not (compiled / "old.o").exists()
    assert any(argv[:2] == ["sh", "./configure"] for argv, _, _ in runner.calls)
    assert native.stat().st_mtime_ns == before


@pytest.mark.parametrize("platform", ["linux-x86_64", "rk3032"])
def test_core_make_arguments_select_target_dynarecs_not_host_architecture(
    options: BuildOptions, platform: str,
) -> None:
    options = replace(options, toolchain=Toolchain(PROFILES[platform], Path("/sdk"), "/sdk/bin/arm-linux-gnueabihf-"))
    selected = {component.name: component for component in options.components}
    calls = {
        name: build_commands(
            core, options, options.source_root, options.build_dir, options.build_dir / "payload",
        )[0][0]
        for name, core in selected.items() if core.core
    }

    assert "LIBS=-lpthread" in calls["core-snes9x2010"]
    assert "MAKEOVERRIDES=" in calls["core-picodrive"]
    assert "CYCLONE_CXX=c++" in calls["core-picodrive"]
    assert "PLATFORM_TREMOR=1" in calls["core-picodrive"]
    assert "HAVE_PHYSICAL_CDROM=0" in calls["core-pcsx-rearmed"]

    if platform == "rk3032":
        assert "CPU_ARCH=arm" in calls["core-gpsp"]
        assert "HAVE_DYNAREC=1" in calls["core-gpsp"]
        assert "DYNAREC=ari64" in calls["core-pcsx-rearmed"]
        assert "HAVE_NEON_ASM=1" in calls["core-pcsx-rearmed"]

    else:
        assert not any(arg.startswith("CPU_ARCH=") for arg in calls["core-gpsp"])
        assert not any(arg.startswith("DYNAREC=") for arg in calls["core-pcsx-rearmed"])


def test_n64_arm_recipe_uses_gles2_target_dynarec_and_bundled_dependencies(
    arm_options: BuildOptions, runner: Any,
) -> None:
    core = next(component for component in arm_options.components if component.name == "core-mupen64plus-next")
    options = replace(arm_options, components=(core,))

    report = Builder(options, runner=runner).run()

    argv = next(argv for argv, _, _ in runner.calls if argv[0] == "make")
    effective = dict(argument.split("=", 1) for argument in argv if "=" in argument)
    assert effective["platform"] == "armv-neon-hardfloat"

    for key, value in {
        "ARCH": "arm", "WITH_DYNAREC": "arm", "FORCE_GLES": "1", "FORCE_GLES3": "0",
        "LLE": "0", "HAVE_PARALLEL_RSP": "0", "HAVE_PARALLEL_RDP": "0",
    }.items():
        assert effective[key] == value

    assert not any(key.startswith("SYSTEM_") for key in effective)
    assert effective["GIT_VERSION"] == core.source.revision[:7]
    assert set(core.host_tools) == {"awk", "strings", "tr"}
    binary = options.dest / "system" / core.destination
    assert binary.is_file()
    record = json.loads((report / paths.REPORT_FILE).read_text())["components"][core.name]
    assert record["game_platforms"] == ["N64"]
    assert record["source"]["sha256"] == core.source.sha256
    assert json.loads((report / paths.REPORT_FILE).read_text())["hardware_verified"] is False
    assert not (options.dest / "system/licenses").exists()
    assert not (options.dest / "system/iroll.json").exists()


def test_cli_visibility_does_not_invalidate_a_compiled_core_recipe(options: BuildOptions, runner: Any) -> None:
    core = next(component for component in options.components if component.name == "core-nestopia")
    options = replace(options, components=(core,))
    Builder(options, runner=runner).run()
    compiled = options.build_dir / paths.THEIR_COMPILED_DIR / options.toolchain.profile.name / core.name
    preserved = compiled / "preserved.o"
    preserved.write_bytes(b"synthetic reusable compiled object")
    before = (preserved.stat().st_ino, preserved.stat().st_mtime_ns)
    hidden = replace(options, components=(replace(core, cli_selectable=False),))

    Builder(hidden, runner=runner).run()

    assert preserved.read_bytes() == b"synthetic reusable compiled object"
    assert (preserved.stat().st_ino, preserved.stat().st_mtime_ns) == before


def test_core_submodule_provenance_is_in_pc_report_not_payload(options: BuildOptions, runner: Any) -> None:
    core = next(component for component in options.components if component.name == "core-picodrive")
    options = replace(options, components=(core,))

    report = Builder(options, runner=runner).run()

    record = json.loads((report / paths.REPORT_FILE).read_text())["components"][core.name]
    assert [module["source"]["sha256"] for module in record["submodules"]] == [
        module.source.sha256 for module in core.submodules
    ]
    assert {
        path.relative_to(options.dest / "system").as_posix()
        for path in (options.dest / "system").rglob("*") if path.is_file()
    } == {
        core.destination,
    }


def test_native_generator_compiler_change_invalidates_cross_picodrive(
    options: BuildOptions, runner: Any, sdk: Path, tmp_path: Path,
) -> None:
    options = replace(
        options,
        components=tuple(item for item in options.components if item.name == "core-picodrive"),
        toolchain=Toolchain(PROFILES["rk3032"], sdk, "/sdk/bin/arm-linux-gnueabihf-"),
    )
    Builder(options, runner=runner).run()
    compiled = options.build_dir / "build/their/compiled/rk3032/core-picodrive"
    old_object = compiled / "stale.o"
    old_object.write_bytes(b"generated using previous native toolchain")
    (tmp_path / "tools/c++").write_bytes(b"updated native compiler")

    Builder(options, runner=runner).run()

    assert not old_object.exists()


def test_missing_submodule_archive_refuses_publication_without_network(
    options: BuildOptions, runner: Any,
) -> None:
    core = next(component for component in options.components if component.name == "core-picodrive")
    options = replace(options, components=(core,))
    (options.archives / core.submodules[0].source.archive).unlink()

    with pytest.raises(BuildError, match="Offline source archive missing"):
        Builder(options, runner=runner).run()

    assert not options.dest.exists()
    assert not any(argv[0] == "make" for argv, _, _ in runner.calls)


def test_builder_constructor_has_no_side_effects(options: BuildOptions, runner: Any) -> None:
    Builder(replace(options, source_root=options.source_root / "missing"), runner=runner)

    assert not options.downloads.exists()
    assert not options.build_dir.exists()
    assert not options.logs.exists()
    assert not options.dest.exists()
    assert not runner.calls
