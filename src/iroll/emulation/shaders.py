"""Pinned, unmodified GLSL resources and platform preset publication.

Only supplied resources are interpreted here. RetroArch owns custom preset chains.
SPDX-License-Identifier: BSD-3-Clause
"""

import hashlib
import json
from collections.abc import Mapping
from importlib.resources import files
from pathlib import Path
from typing import Any

from ..common.errors import BuildError
from ..common.files import parse_relative_path, stage_resources, validate_regular_file
from ..components.registry import COMPONENTS

SHADERS_DIR = "retroarch/shaders"
PRESET = (
    "# SPDX-License-Identifier: BSD-3-Clause\n"
    "#reference \"../libretro/interpolation/sharp-bilinear-simple.glslp\"\n"
).encode()


def read_source_info() -> dict[str, Any]:
    """Provenance of the bundled resources; report on the PC, not on the SD."""
    return json.loads(files("iroll.emulation").joinpath("shader-source.json").read_bytes())


def get_default_preset(binary: str, platform: str) -> str | None:
    """Supply defaults only for known platform/core pairs, not arbitrary cores."""

    if any(component.destination == binary and platform in component.game_platforms for component in COMPONENTS):
        return f"{SHADERS_DIR}/presets/{platform}.glslp"

    return None


def parse_preset_path(value: object) -> Path:
    """Validate a system-relative GLSL preset path before accessing any file."""

    if not isinstance(value, str):
        raise BuildError("shader_preset must be a system-relative .glslp path or null")

    path = parse_relative_path(value)

    if path.suffix != ".glslp" or any(ord(char) == 127 for char in value):
        raise BuildError("shader_preset must be a system-relative .glslp path")

    return path


def stage(payload: Path, cores: Mapping[str, Mapping[str, Any]]) -> dict[str, str]:
    """Stage only referenced built-in presets and their shared upstream files."""
    selected = {
        parse_preset_path(entry["shader_preset"]).as_posix()
        for entry in cores.values() if entry.get("shader_preset") is not None
    }
    builtins = {
        get_default_preset(component.destination, platform)
        for component in COMPONENTS for platform in component.game_platforms
    }
    selected &= builtins

    if not selected:
        return {}

    pending = {name: PRESET for name in sorted(selected)}
    resource = files("iroll").joinpath("resources", "sd", "common", "system", "retroarch", "shaders", "libretro")

    for name, expected in read_source_info()["files"].items():
        relative = parse_relative_path(name)
        data = resource.joinpath(*relative.parts).read_bytes()

        if hashlib.sha256(data).hexdigest() != expected:
            raise BuildError(f"Bundled shader checksum mismatch: {name}")

        pending[f"{SHADERS_DIR}/libretro/{name}"] = data

    return stage_resources(payload, pending, resource_kind="shader")


def validate_presets(
    system: Path, cores: Mapping[str, Mapping[str, Any]], supplied: Mapping[str, Path],
) -> None:
    """Check the effective main preset; referenced files are read by RetroArch."""

    for entry in cores.values():
        value = entry.get("shader_preset")

        if value is None:
            continue

        relative = parse_preset_path(value)
        path = supplied.get(relative.as_posix(), system / relative)
        try:
            validate_regular_file(path, limit=1024 * 1024)

            if path.stat().st_nlink != 1:
                raise BuildError(f"Shader preset must have one link: {value}")

        except OSError as error:
            raise BuildError(f"Cannot read shader preset: {value}. {error}") from error
