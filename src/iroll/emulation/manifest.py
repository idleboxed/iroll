"""Compose runtime manifests from platform and core profiles. SPDX-License-Identifier: BSD-3-Clause."""

import json
from importlib.resources import files
from typing import Any

from ..common.errors import BuildError
from ..host.platforms import Profile
from . import shaders


def build_runtime_manifest(components: dict[str, Any], profile: Profile) -> dict[str, Any]:
    def build_entry(record: dict[str, Any]) -> dict[str, str]:
        return {"path": record["binary"]}

    # Defaults are data, scoped to both the binary and platform. Do not apply
    # a shipped core's input semantics to a different user-assigned core.
    core_profiles = json.loads(files("iroll.emulation").joinpath("emulation-cores.json").read_bytes())
    cores = {}

    for record in components.values():

        for platform in record["game_platforms"]:

            if platform in cores:
                raise BuildError(f"Multiple cores claim platform {platform}; refusing ambiguous runtime manifest")

            cores[platform] = build_entry(record)
            cores[platform].update(record.get("platform_options", {}).get(platform, {}))
            cores[platform].update(core_profiles.get(record["binary"], {}).get(platform, {}))
            preset = shaders.get_default_preset(record["binary"], platform)

            if preset is not None:
                cores[platform].setdefault("shader_preset", preset)

    settings = json.loads(files("iroll.emulation").joinpath(f"emulation-{profile.name}.json").read_bytes())
    return {
        "architecture": profile.architecture,
        "retroarch": build_entry(components["retro"]), "cores": cores, "settings": settings,
    }
