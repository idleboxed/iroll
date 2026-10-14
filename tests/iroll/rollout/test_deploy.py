"""Overlay publication with synthetic outputs and injected failures. SPDX-License-Identifier: BSD-3-Clause."""

import json
import logging
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, NoReturn

import pytest


from iroll.rollout import deploy
from iroll.emulation import joypads
from iroll.common import paths
from iroll.rollout.build import BuildOptions, Builder
from iroll.common.errors import BuildError
from iroll.common.files import acquire_directory_lock
from iroll.host.platforms import PROFILES, Toolchain
from iroll.components.registry import COMPONENTS


@pytest.mark.parametrize("content", [
    pytest.param(b"{", id="incomplete-json"),
    pytest.param(b"\xff", id="invalid-encoding"),
    pytest.param(b"[" * 20_000 + b"]" * 20_000, id="excessive-nesting"),
])
def test_invalid_manifest_is_reported_as_an_expected_error(tmp_path: Path, content: bytes) -> None:
    manifest = tmp_path / "emulation.json"
    manifest.write_bytes(content)

    with pytest.raises(BuildError, match="Invalid manifest"):
        deploy.load_json(manifest)


def test_partial_update_preserves_unselected_components_unknown_files_and_roms(
    options: BuildOptions, runner: Any, caplog: pytest.LogCaptureFixture,
) -> None:

    with caplog.at_level(logging.INFO, logger="iroll.rollout.deploy"):
        Builder(options, runner=runner).run()

    messages = [record.getMessage() for record in caplog.records if record.name == "iroll.rollout.deploy"]
    installed = [message for message in messages if message.startswith("Installed: ")]
    assert installed
    assert messages[-1] == f"Published {len(installed)} changed files"
    system = options.dest / "system"
    owned = [
        system / "owner.txt", options.dest / "games/rom.nes", options.dest / "games/catalogue.json",
        options.dest / "prefs.json", options.dest / "states/save.state", options.dest / "saves/save.srm",
        options.dest / "games/states/save.state3", options.dest / "games/saves/save.srm",
        options.dest / "iman-reports/report.txt",
    ]

    for path in owned:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"preserve")

    tracked = [*owned, system / "retroarch/cores/nestopia_libretro.so", system]
    before = {path: (path.stat().st_ino, path.stat().st_mtime_ns) for path in tracked}
    options = replace(options, components=tuple(item for item in options.components if item.name == "igui"))
    (options.source_root / "igui/locales/ru.json").write_text("{\"new\": \"translation\"}\n")

    Builder(options, runner=runner).run()

    assert {path: (path.stat().st_ino, path.stat().st_mtime_ns) for path in tracked} == before
    assert all(path.read_bytes() == b"preserve" for path in owned)
    assert set(json.loads((system / "iman/emulation.json").read_text())["cores"]) == {
        platform for component in COMPONENTS if "linux-x86_64" in component.platforms
        for platform in component.game_platforms
    }
    assert not list(options.dest.glob(f"{paths.STAGE_PREFIX}*"))


@pytest.mark.parametrize("profile_state", ["missing", "modified"])
def test_iman_only_rollout_restores_joypad_profiles_without_rebuilding_retroarch(
    options: BuildOptions, runner: Any, sdk: Path, profile_state: str,
) -> None:
    options = replace(options, toolchain=Toolchain(PROFILES["rk3032"], sdk, "/sdk/bin/arm-linux-gnueabihf-"))
    Builder(options, runner=runner).run()
    system = options.dest / "system"
    profiles = [system / joypads.AUTOCONFIG_DIR / name for name in joypads.PROFILES]
    original = {path: path.read_bytes() for path in profiles}
    retroarch = system / "retroarch/retroarch"
    before = (retroarch.read_bytes(), retroarch.stat().st_ino, retroarch.stat().st_mtime_ns)

    for path in profiles:

        if profile_state == "missing":
            path.unlink()

        else:
            path.write_bytes(b"synthetic old profile\n")

    runner.calls.clear()
    partial = replace(options, components=tuple(item for item in options.components if item.name == "iman"))

    Builder(partial, runner=runner).run()

    assert {path: path.read_bytes() for path in profiles} == original
    assert (retroarch.read_bytes(), retroarch.stat().st_ino, retroarch.stat().st_mtime_ns) == before
    assert not any(argv[0] in ("make", "sh") for argv, _, _ in runner.calls)


def test_identical_rollout_does_not_stage_or_replace_any_files(
    options: BuildOptions, runner: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    Builder(options, runner=runner).run()
    before = {path: (path.stat().st_ino, path.stat().st_mtime_ns) for path in options.dest.rglob("*")}

    def forbidden(*args: object, **kwargs: object) -> NoReturn:
        raise AssertionError("Identical release must not replace files")

    monkeypatch.setattr(deploy.os, "replace", forbidden)

    Builder(options, runner=runner, confirm_replace=forbidden).run()

    assert {path: (path.stat().st_ino, path.stat().st_mtime_ns) for path in options.dest.rglob("*")} == before


def test_gui_only_rollout_prepares_both_save_directories_and_preserves_root_saves(
    options: BuildOptions, runner: Any,
) -> None:
    options = replace(options, components=tuple(item for item in options.components if item.name == "igui"))
    legacy = options.dest / "states/unchanged.state4"
    legacy.parent.mkdir(parents=True)
    legacy.write_bytes(b"synthetic old state")

    Builder(options, runner=runner).run()

    assert (options.dest / "games/states").is_dir()
    assert (options.dest / "games/saves").is_dir()
    assert legacy.read_bytes() == b"synthetic old state"


def test_changed_selected_core_is_replaced_and_unselected_core_is_preserved(
    options: BuildOptions, runner: Any,
) -> None:
    Builder(options, runner=runner).run()
    system = options.dest / "system"
    core = system / "retroarch/cores/nestopia_libretro.so"
    old = core.read_bytes()
    core.write_bytes(b"edited")
    options = replace(options, components=tuple(item for item in options.components if item.name == "core-nestopia"))

    Builder(options, runner=runner).run()

    assert core.read_bytes() == old
    assert set(json.loads((system / "iman/emulation.json").read_text())["cores"]) == {
        platform for component in COMPONENTS if "linux-x86_64" in component.platforms
        for platform in component.game_platforms
    }


def test_invalid_unselected_elf_is_rejected_before_build(options: BuildOptions, runner: Any) -> None:
    Builder(options, runner=runner).run()
    (options.dest / "system/retroarch/cores/nestopia_libretro.so").write_bytes(b"edited")
    runner.calls.clear()
    options = replace(options, components=tuple(item for item in options.components if item.name == "igui"))

    with pytest.raises(BuildError, match="ELF|architecture"):
        Builder(options, runner=runner).run()

    assert not any(argv[0] in ("cargo", "make", "./configure") for argv, _, _ in runner.calls)


def test_failed_staging_preserves_destination(
    options: BuildOptions, runner: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    Builder(options, runner=runner).run()
    locale = options.dest / "system/igui/locales/ru.json"
    (options.source_root / "igui/locales/ru.json").write_text("{\"new\": \"value\"}\n")
    original = deploy.shutil.copyfile

    def fail_stage(source: Path, target: Path, **kwargs: Any) -> Path:

        if any(parent.name.startswith(paths.STAGE_PREFIX) for parent in Path(target).parents):
            raise OSError("Synthetic copy failure")

        return original(source, target, **kwargs)

    monkeypatch.setattr(deploy.shutil, "copyfile", fail_stage)

    with pytest.raises(OSError, match="copy failure"):
        Builder(options, runner=runner).run()

    assert locale.read_text() == "{}\n"
    assert not list(options.dest.glob(f"{paths.STAGE_PREFIX}*"))


def test_manifest_is_published_last_and_failed_commit_is_reported(
    options: BuildOptions, runner: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    Builder(options, runner=runner).run()
    core = options.dest / "system/retroarch/cores/nestopia_libretro.so"
    core.write_bytes(b"edited")
    manifest = options.dest / "system/iman/emulation.json"
    manifest.write_bytes(b"edited manifest")
    calls = []
    original = deploy.os.replace

    def fail_manifest(source: Path, target: Path) -> None:
        calls.append(target)

        if target == manifest:
            raise OSError("Synthetic manifest rename failure")

        original(source, target)

    monkeypatch.setattr(deploy.os, "replace", fail_manifest)

    with pytest.raises(OSError, match="manifest rename failure"):
        Builder(options, runner=runner).run()

    assert calls[-1] == manifest
    assert core.read_bytes().startswith(b"\x7fELF")
    assert manifest.read_bytes() == b"edited manifest"
    failure = next(options.logs.glob("*/failure.json"))
    assert json.loads(failure.read_text())["publication_may_be_partial"] is True


@pytest.mark.parametrize("location", [
    "system", "system/igui", "system/igui/igui", "INSTRUCTIONS.md", "games", "games/states", "games/saves",
])
def test_release_path_symlinks_are_rejected(options: BuildOptions, runner: Any, location: str) -> None:
    path = options.dest / location
    path.parent.mkdir(parents=True, exist_ok=True)
    outside = options.source_root / "outside"
    outside.mkdir()
    path.symlink_to(outside)

    with pytest.raises(BuildError, match="Symlink"):
        Builder(options, runner=runner).run()

    assert not options.downloads.exists()


def test_unrelated_symlink_and_large_user_file_are_not_traversed(
    options: BuildOptions, runner: Any, tmp_path: Path,
) -> None:
    system = options.dest / "system"
    system.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.write_bytes(b"untouched")
    (system / "user-link").symlink_to(outside)
    big = system / "user-large-file"

    with big.open("wb") as stream:
        stream.truncate(300 * 1024 * 1024)

    before = big.stat()

    Builder(options, runner=runner).run()

    assert (system / "user-link").is_symlink()
    assert outside.read_bytes() == b"untouched"
    assert big.stat().st_ino == before.st_ino
    assert big.stat().st_mtime_ns == before.st_mtime_ns


def test_different_manifest_platform_refuses_partial_update(options: BuildOptions, runner: Any) -> None:
    Builder(options, runner=runner).run()
    options = replace(
        options, toolchain=Toolchain(PROFILES["rk3032"]),
        components=tuple(item for item in options.components if item.name == "iman"),
    )

    with pytest.raises(BuildError, match="different architecture"):
        Builder(options, runner=runner).run()


@pytest.mark.parametrize(
    "location", ["system/igui", "system/iman", "system/retroarch", "games", "games/states", "games/saves"],
)
def test_file_in_required_directory_is_rejected_before_build(
    options: BuildOptions, runner: Any, location: str,
) -> None:
    path = options.dest / location
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"user file")

    with pytest.raises(BuildError, match="File/directory conflict"):
        Builder(options, runner=runner).run()

    assert path.read_bytes() == b"user file"
    assert not options.downloads.exists()


@pytest.mark.parametrize("location", ["system/igui/igui", "INSTRUCTIONS.md", "system/iman/emulation.json"])
def test_directory_at_release_file_path_is_never_deleted(
    options: BuildOptions, runner: Any, location: str,
) -> None:
    path = options.dest / location
    path.mkdir(parents=True)
    (path / "user.txt").write_bytes(b"preserve")

    with pytest.raises(BuildError, match="File/directory conflict"):
        Builder(options, runner=runner).run()

    assert (path / "user.txt").read_bytes() == b"preserve"
    assert not options.downloads.exists()


def test_free_space_checked_before_staging(
    options: BuildOptions, runner: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    options.dest.mkdir()
    monkeypatch.setattr(deploy.os, "statvfs", lambda path: SimpleNamespace(f_bavail=0, f_frsize=4096))

    with pytest.raises(BuildError, match="Not enough free space"):
        Builder(options, runner=runner).run()

    assert not list(options.dest.iterdir())


def test_destination_mutation_during_staging_refuses_commit(
    options: BuildOptions, runner: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    options.dest.mkdir()
    target = options.dest / "INSTRUCTIONS.md"
    target.write_bytes(b"old")
    original = deploy.shutil.copyfile

    def mutate(source: Path, dest: Path, **kwargs: Any) -> Path:
        result = original(source, dest, **kwargs)

        if any(parent.name.startswith(paths.STAGE_PREFIX) for parent in Path(dest).parents):
            target.write_bytes(b"concurrent user edit")

        return result

    monkeypatch.setattr(deploy.shutil, "copyfile", mutate)

    with pytest.raises(BuildError, match="changed during staging"):
        Builder(options, runner=runner).run()

    assert target.read_bytes() == b"concurrent user edit"
    assert not (options.dest / "system").exists()


@pytest.mark.parametrize("prefix", [paths.STAGE_PREFIX, paths.BACKUP_PREFIX])
def test_unfinished_old_publication_is_not_deleted(options: BuildOptions, runner: Any, prefix: str) -> None:
    leftover = options.dest / f"{prefix}interrupted"
    leftover.mkdir(parents=True)

    with pytest.raises(BuildError, match="Unfinished publication"):
        Builder(options, runner=runner).run()

    assert leftover.is_dir()


@pytest.mark.parametrize("which", ["downloads", "build_dir", "logs", "dest"])
def test_directory_lock_blocks_competing_rollout_without_lock_file(
    options: BuildOptions, runner: Any, which: str,
) -> None:
    path = getattr(options, which)
    path.mkdir()

    with acquire_directory_lock(path), pytest.raises(BuildError, match="holds the lock"):
        Builder(options, runner=runner).run()

    assert not (path / ".iroll.lock").exists()


def test_changed_guide_is_updated(options: BuildOptions, runner: Any) -> None:
    options.dest.mkdir()
    guide = options.dest / "INSTRUCTIONS.md"
    guide.write_bytes(b"old guide")

    Builder(options, runner=runner).run()

    assert guide.read_bytes() == deploy.files("iroll").joinpath("resources", "sd", "common", "INSTRUCTIONS.md").read_bytes()


@pytest.mark.parametrize("platform", ["linux-x86_64", "rk3032"])
@pytest.mark.parametrize("partial", [False, True])
def test_updates_replace_external_emulation_settings_with_platform_defaults(
    options: BuildOptions, runner: Any, toolchains: dict[str, Toolchain], platform: str, partial: bool,
) -> None:

    options = replace(options, toolchain=toolchains[platform])

    Builder(options, runner=runner).run()
    manifest = options.dest / "system/iman/emulation.json"
    original = manifest.read_bytes()
    document = json.loads(original)
    document["version"] = 999
    document["future"] = True
    document["settings"]["audio_driver"] = "alsathread"
    document["settings"]["video_autoswitch_refresh_rate"] = 2
    document["settings"]["input_player2_joypad_index"] = 99

    for key in (
        "quick_menu_show_add_to_favorites",
        "menu_show_latency",
        "menu_show_rewind",
        "quick_menu_show_save_core_overrides",
        "quick_menu_show_save_content_dir_overrides",
        "quick_menu_show_save_game_overrides",
        "quick_menu_show_cheats",
    ):
        document["settings"][key] = True

    manifest.write_text(f"{json.dumps(document)}\n")

    components = {
        False: options.components,
        True: tuple(item for item in options.components if item.name == "iman"),
    }
    options = replace(options, components=components[partial])

    Builder(options, runner=runner).run()

    # Reused core fields can have a different order; every JSON value must match.
    assert json.loads(manifest.read_bytes()) == json.loads(original)


@pytest.mark.parametrize("partial", [False, True])
@pytest.mark.parametrize("settings", ["missing", [], None])
def test_existing_settings_are_repaired_from_platform_defaults(
    options: BuildOptions, runner: Any, settings: str | list[Any] | None, partial: bool,
) -> None:
    Builder(options, runner=runner).run()
    manifest = options.dest / "system/iman/emulation.json"
    original = manifest.read_bytes()
    document = json.loads(original)

    if settings == "missing":
        del document["settings"]

    else:
        document["settings"] = settings

    manifest.write_text(json.dumps(document))

    components = {
        False: options.components,
        True: tuple(item for item in options.components if item.name == "iman"),
    }
    options = replace(options, components=components[partial])

    Builder(options, runner=runner).run()

    assert json.loads(manifest.read_bytes()) == json.loads(original)


@pytest.mark.parametrize("platform", ["linux-x86_64", "rk3032"])
@pytest.mark.parametrize("accepted", [False, True])
def test_confirmation_applies_to_each_changed_release_file_but_not_user_data(
    options: BuildOptions, runner: Any, toolchains: dict[str, Toolchain], caplog: pytest.LogCaptureFixture,
    platform: str, accepted: bool,
) -> None:

    options = replace(options, toolchain=toolchains[platform])

    Builder(options, runner=runner).run()
    names = [
        "INSTRUCTIONS.md", "system/igui/igui", "system/igui/locales/ru.json", "system/iman/emulation.json",
        "system/igui/controls/2563-0555-hidraw.json", "system/igui/controls/20bc-5500-evdev.json",
        "system/igui/controls/2563-0555-evdev.json",
        "system/retroarch/padconf/0810-0001.cfg",
        "system/retroarch/padconf/20bc-5500.cfg",
        "system/retroarch/padconf/045e-028e.cfg",
        "system/retroarch/padconf/0079-181c.cfg",
    ]

    names.extend({"rk3032": ["system/iman/iman.json"], "linux-x86_64": []}[platform])

    original = {name: (options.dest / name).read_bytes() for name in names}

    for name in names:
        (options.dest / name).write_bytes(original[name] + b" ")

    user_files = [options.dest / "prefs.json", options.dest / "games/catalogue.json"]

    for path in user_files:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"preserve user data")

    before = {
        path: (path.read_bytes(), path.stat().st_ino, path.stat().st_mtime_ns)
        for path in options.dest.rglob("*") if path.is_file()
    }
    (options.source_root / "igui/locales/de.json").write_text("{}\n")
    prompted = []

    def confirm(path: Path) -> bool:
        assert not list(options.dest.glob(f"{paths.STAGE_PREFIX}*"))
        assert not (options.dest / "system/igui/locales/de.json").exists()
        prompted.append(path.relative_to(options.dest).as_posix())

        return accepted

    with caplog.at_level(logging.INFO):
        report = Builder(options, runner=runner, confirm_replace=confirm).run()

    assert prompted == sorted(names)

    replaced = {options.dest / name for name in names if accepted}
    expected_contents = {path: state[0] for path, state in before.items()}
    expected_contents.update({path: original[path.relative_to(options.dest).as_posix()] for path in replaced})
    assert {path: path.read_bytes() for path in before} == expected_contents

    for path, state in ((path, state) for path, state in before.items() if path not in replaced):
        assert (path.read_bytes(), path.stat().st_ino, path.stat().st_mtime_ns) == state

    assert (options.dest / "system/igui/locales/de.json").read_text() == "{}\n"
    kept = json.loads((report / "result.json").read_text())["kept_files"]
    assert kept == sorted(name for name in names if not accepted)
    assert ("Kept existing:" in caplog.text) is not accepted


def test_first_install_never_requests_replacement_confirmation(options: BuildOptions, runner: Any) -> None:
    def forbidden(path: Path) -> NoReturn:
        raise AssertionError(f"New files need no confirmation: {path}")

    Builder(options, runner=runner, confirm_replace=forbidden).run()

    assert (options.dest / "system/iman/emulation.json").is_file()


def test_declining_all_changes_does_not_stage_or_claim_up_to_date(
    options: BuildOptions, runner: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    Builder(options, runner=runner).run()
    manifest = options.dest / "system/iman/emulation.json"
    manifest.write_bytes(manifest.read_bytes() + b" ")
    before = {path: (path.stat().st_ino, path.stat().st_mtime_ns) for path in options.dest.rglob("*")}

    def forbidden(*args: object, **kwargs: object) -> NoReturn:
        raise AssertionError("Declined replacements must not be staged")

    monkeypatch.setattr(deploy.tempfile, "TemporaryDirectory", forbidden)
    # Builder also uses tempfile; test publication directly with a minimal payload.
    payload = options.source_root / "payload"
    payload.mkdir()

    with caplog.at_level(logging.INFO):
        kept = deploy.publish(payload, options.dest, {}, options.toolchain.profile, confirm_replace=lambda path: False)

    assert kept == ("system/iman/emulation.json",)
    assert {path: (path.stat().st_ino, path.stat().st_mtime_ns) for path in options.dest.rglob("*")} == before
    assert "already up to date" not in caplog.text


@pytest.mark.parametrize("failure", [KeyboardInterrupt(), OSError("Synthetic confirmation failure")])
def test_confirmation_failure_before_publication_preserves_files_and_releases_locks(
    options: BuildOptions, runner: Any, failure: BaseException,
) -> None:
    Builder(options, runner=runner).run()

    for name in ("INSTRUCTIONS.md", "system/igui/locales/ru.json"):
        (options.dest / name).write_bytes(b"old version")

    before = {
        path: (path.read_bytes(), path.stat().st_ino, path.stat().st_mtime_ns)
        for path in options.dest.rglob("*") if path.is_file()
    }
    (options.source_root / "igui/locales/de.json").write_text("{}\n")
    prompted = []

    def confirm(path: Path) -> bool:
        prompted.append(path)

        if len(prompted) == 2:
            raise failure

        return True

    with pytest.raises(type(failure), match=f"{failure}" or "^$"):
        Builder(options, runner=runner, confirm_replace=confirm).run()

    assert len(prompted) == 2
    assert {
        path: (path.read_bytes(), path.stat().st_ino, path.stat().st_mtime_ns)
        for path in options.dest.rglob("*") if path.is_file()
    } == before
    assert not list(options.dest.glob(f"{paths.STAGE_PREFIX}*"))

    with (
        acquire_directory_lock(options.dest),
        acquire_directory_lock(options.downloads),
        acquire_directory_lock(options.build_dir),
        acquire_directory_lock(options.logs),
    ):
        pass


def test_destination_change_during_confirmation_is_rejected_before_publication(
    options: BuildOptions, runner: Any,
) -> None:
    Builder(options, runner=runner).run()
    guide = options.dest / "INSTRUCTIONS.md"
    guide.write_bytes(b"old guide")
    locale = options.dest / "system/igui/locales/ru.json"
    original_locale = locale.read_bytes()
    (options.source_root / "igui/locales/ru.json").write_text("{\"new\": \"translation\"}\n")

    def confirm(path: Path) -> bool:

        if path == guide:
            guide.write_bytes(b"concurrent edit")
            return False

        return True

    with pytest.raises(BuildError, match="changed during staging"):
        Builder(options, runner=runner, confirm_replace=confirm).run()

    assert guide.read_bytes() == b"concurrent edit"
    assert locale.read_bytes() == original_locale
    assert not list(options.dest.glob(f"{paths.STAGE_PREFIX}*"))


def test_valid_updated_unselected_core_needs_no_manifest_hash_change(
    options: BuildOptions, runner: Any,
) -> None:
    Builder(options, runner=runner).run()
    manifest = options.dest / "system/iman/emulation.json"
    document = json.loads(manifest.read_text())
    assert set(document["retroarch"]) == {"path"}
    assert all("sha256" not in core for core in document["cores"].values())
    assert all(
        set(core) == {"path", "library_name", "remap", "shader_preset"} | (
            {"core_options", "system_directory"} if platform == "N64"
            else {"core_options"} if platform in ("MD", "SCD", "32X") else set()
        )
        for platform, core in document["cores"].items()
    )
    core = options.dest / "system/retroarch/cores/nestopia_libretro.so"
    changed = core.read_bytes() + b"updated synthetic core"
    core.write_bytes(changed)
    partial = replace(options, components=tuple(item for item in options.components if item.name == "igui"))

    Builder(partial, runner=runner).run()

    assert core.read_bytes() == changed
    assert json.loads(manifest.read_text()) == document


def test_mixed_replacement_answers_keep_only_declined_files(options: BuildOptions, runner: Any) -> None:
    Builder(options, runner=runner).run()
    guide = options.dest / "INSTRUCTIONS.md"
    locale = options.dest / "system/igui/locales/ru.json"
    guide.write_bytes(b"keep this guide")
    locale.write_bytes(b"replace this locale")
    prompted = []

    def confirm(path: Path) -> bool:
        prompted.append(path)

        return path != guide

    report = Builder(options, runner=runner, confirm_replace=confirm).run()

    assert prompted == [guide, locale]
    assert guide.read_bytes() == b"keep this guide"
    assert locale.read_text() == "{}\n"
    assert json.loads((report / "result.json").read_text())["kept_files"] == ["INSTRUCTIONS.md"]


def test_controls_are_replaced_by_default_on_igui_only_update(options: BuildOptions, runner: Any) -> None:
    Builder(options, runner=runner).run()
    control = options.dest / "system/igui/controls/20bc-5500-evdev.json"
    control.write_bytes(b"old control settings")
    source = options.source_root / "igui/controls/20bc-5500-evdev.json"
    source.write_bytes(b"{\"new\": \"control settings\"}\n")
    options = replace(options, components=tuple(item for item in options.components if item.name == "igui"))

    Builder(options, runner=runner).run()

    assert control.read_bytes() == source.read_bytes()


def test_picodrive_replaces_old_md_assignment_without_deleting_old_binary(
    options: BuildOptions, runner: Any,
) -> None:
    Builder(options, runner=runner).run()
    system = options.dest / "system"
    manifest_path = system / "iman/emulation.json"
    manifest = json.loads(manifest_path.read_text())
    pico = next(component for component in options.components if component.name == "core-picodrive")
    legacy = system / "retroarch/cores/genesis_plus_gx_libretro.so"
    legacy.write_bytes((system / pico.destination).read_bytes())
    manifest["cores"] = {"MD": {"path": "retroarch/cores/genesis_plus_gx_libretro.so"}}
    manifest_path.write_text(json.dumps(manifest))

    Builder(replace(options, components=(pico,)), runner=runner).run()

    updated = json.loads(manifest_path.read_text())

    for platform in pico.game_platforms:
        assert updated["cores"][platform]["path"] == pico.destination

    assert legacy.is_file()


def test_partial_rollout_adds_fds_defaults_but_keeps_unprofiled_platform_options(
    options: BuildOptions, runner: Any,
) -> None:
    Builder(options, runner=runner).run()
    manifest = options.dest / "system/iman/emulation.json"
    document = json.loads(manifest.read_bytes())
    document["cores"]["FDS"].pop("remap")
    document["cores"]["FDS"].pop("library_name")
    document["cores"]["OTHER"] = {
        "path": "retroarch/cores/picodrive_libretro.so", "library_name": "PicoDrive",
        "remap": {"input_player1_btn_a": 11}, "core_options": {"picodrive_input1": "3 button pad"},
    }
    manifest.write_text(json.dumps(document))
    options = replace(options, components=tuple(item for item in options.components if item.name == "iman"))

    Builder(options, runner=runner).run()

    updated = json.loads(manifest.read_bytes())
    assert updated["cores"]["OTHER"] == document["cores"]["OTHER"]
    assert updated["cores"]["FDS"]["remap"] == {
        "input_player1_btn_a": 0, "input_player1_btn_b": 8,
        "input_player1_btn_x": 10, "input_player1_btn_y": 11,
        "input_player2_btn_a": 0, "input_player2_btn_b": 8,
        **{
            f"input_player{player}_btn_{button}": -1
            for player in (1, 2) for button in ("l", "r", "l2", "r2", "l3", "r3")
        },
    }
    assert not list(options.dest.rglob("*.rmp"))


@pytest.mark.parametrize("host", ["rk3032", "linux-x86_64"])
def test_partial_rollout_replaces_legacy_controller_maps_and_options_but_keeps_other_defaults(
    options: BuildOptions, runner: Any, toolchains: dict[str, Toolchain], host: str,
) -> None:
    options = replace(options, toolchain=toolchains[host])
    Builder(options, runner=runner).run()
    manifest = options.dest / "system/iman/emulation.json"
    expected = json.loads(manifest.read_bytes())
    document = json.loads(manifest.read_bytes())

    for platform in document["cores"]:
        document["cores"][platform].update(library_name="Legacy Core", remap={"input_player1_btn_a": 8})

    for platform in ("MD", "SCD", "32X"):
        document["cores"][platform]["core_options"] = {"picodrive_input1": "3 button pad"}

    document["settings"]["input_turbo_enable"] = True
    manifest.write_text(json.dumps(document))
    partial = replace(options, components=tuple(item for item in options.components if item.name == "iman"))

    Builder(partial, runner=runner).run()

    assert json.loads(manifest.read_bytes()) == expected
    assert not list(options.dest.rglob("*.rmp"))


def test_partial_rollout_preserves_foreign_core_remap_without_applying_nestopia_defaults(
    options: BuildOptions, runner: Any,
) -> None:
    Builder(options, runner=runner).run()
    system = options.dest / "system"
    foreign = system / "retroarch/cores/foreign.so"
    foreign.write_bytes((system / "retroarch/cores/nestopia_libretro.so").read_bytes())
    manifest = system / "iman/emulation.json"
    document = json.loads(manifest.read_bytes())
    document["cores"]["FDS"] = {
        "path": "retroarch/cores/foreign.so", "library_name": "Other Core",
        "remap": {"input_player1_btn_y": 8},
    }
    manifest.write_text(json.dumps(document))
    options = replace(options, components=tuple(item for item in options.components if item.name == "igui"))

    Builder(options, runner=runner).run()

    assert json.loads(manifest.read_bytes())["cores"]["FDS"] == document["cores"]["FDS"]


def test_declining_manifest_replacement_preserves_existing_remap_bytes(
    options: BuildOptions, runner: Any,
) -> None:
    Builder(options, runner=runner).run()
    manifest = options.dest / "system/iman/emulation.json"
    document = json.loads(manifest.read_bytes())
    document["cores"]["FDS"]["remap"] = {"input_player1_btn_y": 10}
    manifest.write_text(json.dumps(document))
    before = manifest.read_bytes()
    options = replace(options, components=tuple(item for item in options.components if item.name == "iman"))

    Builder(options, runner=runner, confirm_replace=lambda path: path != manifest).run()

    assert manifest.read_bytes() == before


def test_partial_gui_rollout_preserves_foreign_shared_core_options_and_system_policy(
    options: BuildOptions, runner: Any,
) -> None:
    Builder(options, runner=runner).run()
    system = options.dest / "system"
    foreign = system / "retroarch/cores/foreign.so"
    foreign.write_bytes((system / "retroarch/cores/nestopia_libretro.so").read_bytes())
    manifest = system / "iman/emulation.json"
    document = json.loads(manifest.read_bytes())
    document["cores"]["N64"] = {
        "path": "retroarch/cores/foreign.so", "system_directory": "bios",
        "core_options": {"other-resolution": "native"},
    }
    document["cores"]["OTHER"] = {
        "path": "retroarch/cores/foreign.so", "system_directory": "ram",
        "core_options": {"other-option": "Enabled"},
    }
    manifest.write_text(json.dumps(document))
    before = (foreign.read_bytes(), foreign.stat().st_ino, foreign.stat().st_mtime_ns)
    partial = replace(options, components=tuple(item for item in options.components if item.name == "igui"))

    Builder(partial, runner=runner).run()

    updated = json.loads(manifest.read_bytes())
    assert updated["cores"]["N64"] == document["cores"]["N64"]
    assert updated["cores"]["OTHER"] == document["cores"]["OTHER"]
    assert (foreign.read_bytes(), foreign.stat().st_ino, foreign.stat().st_mtime_ns) == before


def test_manager_only_rollout_applies_depth_copy_trial_and_original_controls_without_rebuilding_n64(
    arm_options: BuildOptions, runner: Any,
) -> None:
    Builder(arm_options, runner=runner).run()
    system = arm_options.dest / "system"
    binary = system / "retroarch/cores/mupen64plus_next_libretro.so"
    before = (binary.read_bytes(), binary.stat().st_ino, binary.stat().st_mtime_ns)
    manifest = system / "iman/emulation.json"
    document = json.loads(manifest.read_bytes())
    profile = json.loads(manifest.read_bytes())["cores"]["N64"]
    document["cores"]["N64"]["core_options"]["mupen64plus-EnableCopyDepthToRDRAM"] = "Software"
    document["cores"]["N64"]["core_options"]["mupen64plus-alt-map"] = "False"
    document["cores"]["N64"].pop("remap")
    document["cores"]["N64"].pop("library_name")
    manifest.write_text(json.dumps(document))
    runner.calls.clear()
    partial = replace(arm_options, components=tuple(item for item in arm_options.components if item.name == "iman"))

    Builder(partial, runner=runner).run()

    assert json.loads(manifest.read_bytes())["cores"]["N64"] == profile
    assert profile["system_directory"] == "ram"
    assert profile["core_options"]["mupen64plus-EnableCopyDepthToRDRAM"] == "Off"
    assert profile["core_options"]["mupen64plus-alt-map"] == "True"
    assert profile["remap"]["input_player1_btn_r3"] == -1
    assert (binary.read_bytes(), binary.stat().st_ino, binary.stat().st_mtime_ns) == before
    assert not any(argv[0] in ("make", "sh") for argv, _, _ in runner.calls)
