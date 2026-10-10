"""Shader publication uses synthetic ELF/ROM fixtures, never a GPU or SD card.

SPDX-License-Identifier: BSD-3-Clause
"""

import hashlib
import json
from collections.abc import Callable
from dataclasses import replace
from importlib.resources import files
from pathlib import Path
from typing import Any

import pytest


from iroll.emulation import shaders
from iroll.rollout.build import BuildOptions, Builder
from iroll.common.errors import BuildError


def create_oversized_preset(path: Path) -> None:

    with path.open("wb") as stream:
        stream.truncate(1024 * 1024 + 1)


def test_packaged_shader_files_match_pins_and_keep_upstream_notice() -> None:
    source = shaders.read_source_info()
    resource = files("iroll").joinpath("resources", "sd", "common", "system", "retroarch", "shaders", "libretro")

    assert source["revision"] == "f8e23ff880668f0f0e837a05a316534d82a7f31b"

    for name, digest in source["files"].items():
        data = resource.joinpath(*name.split("/")).read_bytes()
        assert hashlib.sha256(data).hexdigest() == digest

    shader = resource.joinpath("interpolation", "shaders", "sharp-bilinear-simple.glsl").read_text()
    assert "License: Public domain" in shader
    assert "Author: rsn8887" in shader


def test_stage_installs_only_selected_presets_and_one_shared_shader(tmp_path: Path) -> None:
    cores = {
        "NES": {"shader_preset": "retroarch/shaders/presets/NES.glslp"},
        "FDS": {"shader_preset": "retroarch/shaders/presets/FDS.glslp"},
        "SNES": {"shader_preset": None},
        "CUSTOM": {"shader_preset": "user/custom.glslp"},
    }

    staged = shaders.stage(tmp_path, cores)

    assert len(staged) == 4
    assert len(list(tmp_path.rglob("*.glsl"))) == 1

    for platform in ("NES", "FDS"):
        preset = tmp_path / cores[platform]["shader_preset"]
        assert preset.read_bytes() == shaders.PRESET
        original = preset.parent / "../libretro/interpolation/sharp-bilinear-simple.glslp"
        assert original.is_file()
        assert (original.parent / "shaders/sharp-bilinear-simple.glsl").is_file()

    assert not (tmp_path / "retroarch/shaders/presets/SNES.glslp").exists()
    assert not (tmp_path / "user").exists()
    assert all(hashlib.sha256((tmp_path / name).read_bytes()).hexdigest() == digest for name, digest in staged.items())


def test_empty_shader_selection_creates_nothing(tmp_path: Path) -> None:
    result = shaders.stage(tmp_path, {"NES": {}, "FDS": {"shader_preset": None}})

    assert result == {}
    assert not list(tmp_path.iterdir())


def test_corrupt_package_is_rejected_before_any_shader_is_staged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    pins = shaders.read_source_info()
    pins["files"]["interpolation/shaders/sharp-bilinear-simple.glsl"] = "0" * 64
    monkeypatch.setattr(shaders, "read_source_info", lambda: pins)

    with pytest.raises(BuildError, match="Bundled shader checksum mismatch"):
        shaders.stage(tmp_path, {"NES": {"shader_preset": "retroarch/shaders/presets/NES.glslp"}})

    assert not list(tmp_path.iterdir())


def test_rollout_publishes_presets_and_keeps_source_provenance_off_sd(options: BuildOptions, runner: Any) -> None:
    report = Builder(options, runner=runner).run()
    system = options.dest / "system"
    document = json.loads((system / "iman/emulation.json").read_bytes())

    for entry in document["cores"].values():
        assert (system / entry["shader_preset"]).read_bytes() == shaders.PRESET

    assert len(list((system / "retroarch/shaders/libretro").rglob("*.glsl"))) == 1
    assert json.loads((report / "result.json").read_bytes())["bundled_shader_source"] == shaders.read_source_info()
    assert not list(options.dest.rglob("shader-source.json"))
    assert not (system / "licenses").exists()


def test_partial_update_restores_missing_supplied_presets_without_rebuilding_retroarch(
    options: BuildOptions, runner: Any,
) -> None:
    Builder(options, runner=runner).run()
    system = options.dest / "system"
    preset = system / "retroarch/shaders/presets/NES.glslp"
    preset.unlink()
    original = system / "retroarch/shaders/libretro/interpolation/shaders/sharp-bilinear-simple.glsl"
    original.unlink()
    retroarch = system / "retroarch/retroarch"
    before = retroarch.stat().st_ino
    runner.calls.clear()
    partial = replace(options, components=tuple(item for item in options.components if item.name == "iman"))

    Builder(partial, runner=runner).run()

    assert preset.read_bytes() == shaders.PRESET
    assert original.is_file()
    assert retroarch.stat().st_ino == before
    assert not any(argv[0] in ("make", "sh") for argv, _, _ in runner.calls)


def test_partial_update_preserves_custom_shader_and_explicit_disable(options: BuildOptions, runner: Any) -> None:
    Builder(options, runner=runner).run()
    system = options.dest / "system"
    manifest = system / "iman/emulation.json"
    document = json.loads(manifest.read_bytes())
    custom = system / "custom.glslp"
    custom.write_bytes(b"# synthetic owner preset\n")
    document["cores"]["NES"]["shader_preset"] = "custom.glslp"
    document["cores"]["FDS"]["shader_preset"] = None
    manifest.write_text(json.dumps(document))
    before = custom.stat().st_mtime_ns
    partial = replace(options, components=tuple(item for item in options.components if item.name == "igui"))

    Builder(partial, runner=runner).run()

    updated = json.loads(manifest.read_bytes())
    assert updated["cores"]["NES"]["shader_preset"] == "custom.glslp"
    assert updated["cores"]["FDS"]["shader_preset"] is None
    assert custom.stat().st_mtime_ns == before


@pytest.mark.parametrize("value", [
    True, 3, [], {}, "", "../escape.glslp", "/absolute.glslp",
    "a/../b.glslp", "a//b.glslp", "a\\b.glslp", "a\nb.glslp", "a.slangp",
])
def test_invalid_shader_paths_fail_without_creating_payload_files(tmp_path: Path, value: Any) -> None:

    with pytest.raises(BuildError, match="path|shader_preset"):
        shaders.stage(tmp_path, {"NES": {"shader_preset": value}})

    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("create_preset", [
    pytest.param(lambda path, reference: None, id="missing"),
    pytest.param(lambda path, reference: path.symlink_to(reference), id="symlink"),
    pytest.param(lambda path, reference: path.hardlink_to(reference), id="hardlink"),
    pytest.param(lambda path, reference: path.mkdir(), id="directory"),
    pytest.param(lambda path, reference: create_oversized_preset(path), id="oversized"),
])
def test_unavailable_custom_preset_refuses_publication_before_destination_changes(
    options: BuildOptions, runner: Any, create_preset: Callable[[Path, Path], object],
) -> None:
    Builder(options, runner=runner).run()
    system = options.dest / "system"
    manifest = system / "iman/emulation.json"
    document = json.loads(manifest.read_bytes())
    document["cores"]["NES"]["shader_preset"] = "custom.glslp"
    manifest.write_text(json.dumps(document))
    create_preset(system / "custom.glslp", system / "retroarch/shaders/presets/NES.glslp")
    before = manifest.read_bytes()
    partial = replace(options, components=tuple(item for item in options.components if item.name == "iman"))

    with pytest.raises(BuildError, match="shader preset|Shader preset|Symlink|regular file"):
        Builder(partial, runner=runner).run()

    assert manifest.read_bytes() == before
    assert not list(options.dest.glob(".iroll-stage-*"))


def test_preset_replacement_can_be_declined_without_rewriting_owner_settings(
    options: BuildOptions, runner: Any,
) -> None:
    Builder(options, runner=runner).run()
    preset = options.dest / "system/retroarch/shaders/presets/NES.glslp"
    owner = b"# synthetic owner preset\n"
    preset.write_bytes(owner)
    asked = []

    def confirm(path: Path) -> bool:
        asked.append(path)

        return False

    report = Builder(options, runner=runner, confirm_replace=confirm).run()

    assert asked == [preset]
    assert preset.read_bytes() == owner
    assert json.loads((report / "result.json").read_bytes())["kept_files"] == [
        "system/retroarch/shaders/presets/NES.glslp",
    ]
