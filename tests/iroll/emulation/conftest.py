"""Synthetic sources, firmware, card metadata and tool responses; no hardware or network.

SPDX-License-Identifier: BSD-3-Clause
"""

from pathlib import Path
from typing import Any

import pytest

from iroll.rollout.build import BuildOptions
from iroll.emulation.manifest import build_runtime_manifest
from iroll.host.platforms import PROFILES


@pytest.fixture
def firmware(options: BuildOptions) -> Path:
    source = options.bios / "FDS/disksys.rom"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"synthetic firmware".ljust(8192, b"\0"))

    return source


@pytest.fixture(params=["linux-x86_64", "rk3032"])
def emulation_settings(request: pytest.FixtureRequest) -> dict[str, Any]:
    components = {"retro": {"binary": "retroarch/retroarch", "game_platforms": []}}
    return build_runtime_manifest(components, PROFILES[request.param])["settings"]
