"""Synthetic firmware only; no copyrighted BIOS, compiler, media or network access.

SPDX-License-Identifier: BSD-3-Clause
"""

import json
import os
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from iroll.emulation import bios
from iroll.rollout import deploy
from iroll.rollout.build import BuildOptions, Builder
from iroll.common.errors import BuildError
from iroll.common.files import calculate_sha256


def test_missing_bios_is_optional_and_does_not_create_input_or_destination_directory(
    options: BuildOptions, runner: Any,
) -> None:
    Builder(options, runner=runner).run()

    assert not options.bios.exists()
    assert not (options.dest / "system/bios").exists()
    manifest = json.loads((options.dest / "system/iman/emulation.json").read_bytes())
    assert manifest["cores"]["FDS"]["path"] == manifest["cores"]["NES"]["path"]


def test_bios_uses_common_publication_report_and_does_not_rewrite_identical_files(
    options: BuildOptions, runner: Any, firmware: Path,
) -> None:
    before = (firmware.stat().st_ino, firmware.stat().st_mtime_ns, firmware.read_bytes())
    report = Builder(options, runner=runner).run()
    installed = options.dest / "system/bios/FDS/disksys.rom"
    published = (installed.stat().st_ino, installed.stat().st_mtime_ns)

    Builder(options, runner=runner, confirm_replace=lambda _: pytest.fail("Unchanged BIOS must not ask")).run()

    assert installed.read_bytes() == firmware.read_bytes()
    assert (installed.stat().st_ino, installed.stat().st_mtime_ns) == published
    assert (firmware.stat().st_ino, firmware.stat().st_mtime_ns, firmware.read_bytes()) == before
    assert json.loads((report / "result.json").read_bytes())["bios"] == {
        "bios/FDS/disksys.rom": calculate_sha256(firmware),
    }


def test_partial_rollout_deploys_bios_and_adds_fds_alias_to_old_nes_manifest(
    options: BuildOptions, runner: Any, firmware: Path,
) -> None:
    Builder(replace(options, bios=None), runner=runner).run()
    manifest = options.dest / "system/iman/emulation.json"
    document = json.loads(manifest.read_bytes())
    del document["cores"]["FDS"]
    manifest.write_text(json.dumps(document))
    options = replace(
        options, components=tuple(component for component in options.components if component.name == "iman"),
    )

    Builder(options, runner=runner).run()

    updated = json.loads(manifest.read_bytes())
    assert updated["cores"]["FDS"]["path"] == updated["cores"]["NES"]["path"]
    assert (options.dest / "system/bios/FDS/disksys.rom").read_bytes() == firmware.read_bytes()


@pytest.mark.parametrize("allow", [False, True])
def test_bios_replacement_respects_ask_and_missing_source_preserves_installed_file(
    options: BuildOptions, runner: Any, firmware: Path, allow: bool,
) -> None:
    Builder(options, runner=runner).run()
    installed = options.dest / "system/bios/FDS/disksys.rom"
    old = installed.read_bytes()
    firmware.write_bytes(b"changed".ljust(8192, b"\0"))
    asked = []

    def confirm(path: Path) -> bool:
        asked.append(path)

        return allow

    Builder(options, runner=runner, confirm_replace=confirm).run()
    expected = firmware.read_bytes() if allow else old
    firmware.unlink()
    Builder(options, runner=runner).run()

    assert asked == [installed]
    assert installed.read_bytes() == expected


@pytest.mark.parametrize("part", ["root", "platform", "file"])
def test_bios_symlinks_are_rejected_before_build(
    options: BuildOptions, runner: Any, firmware: Path, part: str,
) -> None:
    source = {"root": options.bios, "platform": firmware.parent, "file": firmware}[part]
    moved = source.with_name(f"{source.name}-real")
    source.rename(moved)
    source.symlink_to(moved)

    with pytest.raises(BuildError, match="Symlink"):
        Builder(options, runner=runner).run()

    assert not runner.calls
    assert not options.dest.exists()


@pytest.mark.parametrize("size", [0, 8191, 8192, 8193, 40976])
def test_bios_does_not_impose_a_platform_specific_size(
    options: BuildOptions, runner: Any, firmware: Path, size: int,
) -> None:
    firmware.write_bytes(b"x" * size)

    Builder(options, runner=runner).run()

    assert (options.dest / "system/bios/FDS/disksys.rom").stat().st_size == size


@pytest.mark.parametrize("damage", [
    pytest.param(lambda options, firmware: os.mkfifo(firmware.parent / "pipe"), id="fifo"),
    pytest.param(lambda options, firmware: firmware.parent.rename(options.bios / "fds"), id="lowercase"),
    pytest.param(lambda options, firmware: (options.bios / "loose.rom").write_bytes(b"loose"), id="root-file"),
])
def test_bios_layout_and_file_types_are_checked(
    options: BuildOptions, firmware: Path, damage: Callable[[BuildOptions, Path], object],
) -> None:
    damage(options, firmware)

    with pytest.raises(BuildError, match="regular file|platform directory"):
        bios.collect(options.bios)


@pytest.mark.parametrize("destination", ["dest", "downloads", "build_dir", "logs", "source_root"])
def test_bios_overlaps_are_rejected(options: BuildOptions, runner: Any, destination: str) -> None:
    options = replace(options, bios=getattr(options, destination) / "bios")

    with pytest.raises(BuildError, match="BIOS inputs overlap"):
        Builder(options, runner=runner).run()

    assert not runner.calls


def test_changed_bios_during_build_does_not_publish_anything(
    options: BuildOptions, runner: Any, firmware: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = runner.run

    def change(argv: Sequence[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        result = original(argv, **kwargs)

        if argv[:2] == ["cargo", "build"]:
            firmware.write_bytes(b"changed".ljust(8192, b"\0"))

        return result

    monkeypatch.setattr(runner, "run", change)

    with pytest.raises(BuildError, match="BIOS inputs changed"):
        Builder(options, runner=runner).run()

    assert not options.dest.exists()
    assert not (options.build_dir / "out_stage").exists()


def test_bios_copy_failure_keeps_destination_and_cleans_staging(
    options: BuildOptions, runner: Any, firmware: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    Builder(options, runner=runner).run()
    installed = options.dest / "system/bios/FDS/disksys.rom"
    old = installed.read_bytes()
    firmware.write_bytes(b"new".ljust(8192, b"\0"))
    original = deploy.shutil.copyfile

    def fail(source: Path, target: Path, **kwargs: Any) -> Path:

        if f"{target}".endswith("bios/FDS/disksys.rom"):
            raise OSError("Synthetic BIOS publication failure")

        return original(source, target, **kwargs)

    monkeypatch.setattr(deploy.shutil, "copyfile", fail)

    with pytest.raises(OSError, match="BIOS publication failure"):
        Builder(options, runner=runner).run()

    assert installed.read_bytes() == old
    assert not list(options.dest.glob(".iroll-stage-*"))


def test_bios_destination_symlink_is_rejected_before_build(
    options: BuildOptions, runner: Any, firmware: Path,
) -> None:
    target = options.dest / "system/bios/FDS/disksys.rom"
    target.parent.mkdir(parents=True)
    target.symlink_to(firmware)

    with pytest.raises(BuildError, match="Symlink"):
        Builder(options, runner=runner).run()

    assert not runner.calls


@pytest.mark.parametrize("keep_nes", [False, True])
def test_partial_rollout_preserves_explicit_fds_core_assignment(
    options: BuildOptions, runner: Any, keep_nes: bool,
) -> None:
    Builder(options, runner=runner).run()
    manifest = options.dest / "system/iman/emulation.json"
    document = json.loads(manifest.read_bytes())
    # Synthetic compatible ELF at another path, representing an explicit owner choice.
    custom = options.dest / "system/retroarch/cores/custom-fds.so"
    custom.write_bytes((options.dest / "system/retroarch/cores/nestopia_libretro.so").read_bytes())
    document["cores"]["FDS"]["path"] = "retroarch/cores/custom-fds.so"

    if not keep_nes:
        del document["cores"]["NES"]

    manifest.write_text(json.dumps(document))
    options = replace(
        options, components=tuple(component for component in options.components if component.name == "iman"),
    )

    Builder(options, runner=runner).run()

    assert json.loads(manifest.read_bytes())["cores"]["FDS"]["path"] == "retroarch/cores/custom-fds.so"


def test_nested_multifile_bios_for_unknown_platform_is_published_unchanged(
    options: BuildOptions, runner: Any, firmware: Path,
) -> None:
    source = options.bios / "NEW/sub/日本語.bin"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"nested firmware")
    (source.parent.parent / "boot.bin").write_bytes(b"boot")
    (source.parent / "empty").mkdir()

    report = Builder(options, runner=runner).run()

    assert (options.dest / "system/bios/NEW/sub/日本語.bin").read_bytes() == b"nested firmware"
    assert (options.dest / "system/bios/NEW/boot.bin").read_bytes() == b"boot"
    assert not (options.dest / "system/bios/NEW/sub/empty").exists()
    records = json.loads((report / "result.json").read_bytes())["bios"]
    assert records["bios/NEW/sub/日本語.bin"] == calculate_sha256(source)
    manifest = json.loads((options.dest / "system/iman/emulation.json").read_bytes())
    assert "NEW" not in manifest["cores"]
    assert "bios" not in manifest


@pytest.mark.parametrize("create_bad_file", [
    pytest.param(lambda path, firmware: path.symlink_to(firmware), id="file-symlink"),
    pytest.param(lambda path, firmware: path.symlink_to(path.parent), id="directory-symlink"),
    pytest.param(lambda path, firmware: os.link(firmware, path), id="hardlink"),
    pytest.param(lambda path, firmware: os.mkfifo(path), id="fifo"),
])
def test_nested_bios_rejects_links_and_special_files(
    options: BuildOptions, firmware: Path, create_bad_file: Callable[[Path, Path], object],
) -> None:
    directory = options.bios / "NEW/nested"
    directory.mkdir(parents=True)
    create_bad_file(directory / "bad", firmware)

    with pytest.raises(BuildError, match="Symlink|single-link|regular file"):
        bios.collect(options.bios)


def test_bios_has_no_ram_budget_or_artificial_tree_depth_limit(options: BuildOptions) -> None:
    platform = options.bios / "NEW"
    platform.mkdir(parents=True)

    for name in ["a", "b"]:

        with (platform / name).open("wb") as stream:
            stream.truncate(17 * 1024 * 1024)

    deep = platform.joinpath(*(["sub"] * 12), "firmware.bin")
    deep.parent.mkdir(parents=True)
    deep.write_bytes(b"firmware")

    assert len(bios.collect(options.bios)) == 3


def test_nested_bios_source_change_after_preflight_prevents_publication(
    options: BuildOptions, runner: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = options.bios / "NEW/sub/boot.bin"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"old")
    original = runner.run

    def change(argv: Sequence[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        result = original(argv, **kwargs)

        if argv[:2] == ["cargo", "build"]:
            source.write_bytes(b"new")

        return result

    monkeypatch.setattr(runner, "run", change)

    with pytest.raises(BuildError, match="BIOS inputs changed"):
        Builder(options, runner=runner).run()

    assert not options.dest.exists()


def test_nested_bios_destination_symlink_is_rejected_before_build(
    options: BuildOptions, runner: Any, tmp_path: Path,
) -> None:
    source = options.bios / "NEW/sub/boot.bin"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"firmware")
    outside = tmp_path / "outside"
    outside.mkdir()
    parent = options.dest / "system/bios/NEW"
    parent.mkdir(parents=True)
    (parent / "sub").symlink_to(outside)

    with pytest.raises(BuildError, match="Symlink"):
        Builder(options, runner=runner).run()

    assert not runner.calls
    assert not list(outside.iterdir())


@pytest.mark.parametrize("name", ["bad\x7f.bin", "bad\x85.bin", os.fsdecode(b"bad\xff.bin")])
def test_bios_rejects_control_characters_and_non_utf8_names(options: BuildOptions, name: str) -> None:
    platform = options.bios / "NEW"
    platform.mkdir(parents=True)
    (platform / name).write_bytes(b"firmware")

    with pytest.raises(BuildError, match="filename"):
        bios.collect(options.bios)
