"""Device-specific RetroArch joypad autoconfiguration profiles.

SPDX-License-Identifier: BSD-3-Clause
"""

from importlib.resources import files
from pathlib import Path

from ..common.files import stage_resources

AUTOCONFIG_DIR = "retroarch/padconf"
PROFILES = (
    "0810-0001.cfg",
    "20bc-5500.cfg",
    "045e-028e.cfg",
    "0079-181c.cfg",
)


def stage(payload: Path) -> dict[str, str]:
    """Stage the complete, fixed set of controller profiles."""
    resource = files("iroll").joinpath("resources", "sd", "common", "system", "retroarch", "padconf")
    pending = {
        f"{AUTOCONFIG_DIR}/{name}": resource.joinpath(*name.split("/")).read_bytes()
        for name in PROFILES
    }

    return stage_resources(payload, pending, resource_kind="joypad profile")
