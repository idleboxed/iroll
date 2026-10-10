"""RetroArch controller profiles from the board input test.

SPDX-License-Identifier: BSD-3-Clause
"""

import hashlib
from importlib.resources import files
from pathlib import Path

from iroll.emulation import joypads


def parse_profile(data: str) -> dict[str, str]:
    return {
        key.strip(): value.strip().strip("\"")
        for line in data.splitlines()
        if line and not line.startswith("#")
        for key, value in [line.split("=", 1)]
    }


def test_packaged_profiles_keep_device_specific_retroarch_indices() -> None:
    resource = files("iroll").joinpath("resources", "sd", "common", "system", "retroarch", "padconf")
    hid = parse_profile(resource.joinpath("0810-0001.cfg").read_text())
    shanwan = parse_profile(resource.joinpath("20bc-5500.cfg").read_text())

    assert (hid["input_vendor_id"], hid["input_product_id"]) == ("2064", "1")
    assert (shanwan["input_vendor_id"], shanwan["input_product_id"]) == ("8380", "21760")
    keys = (
        "input_down_btn", "input_start_btn", "input_l2_btn", "input_r2_btn",
        "input_x_btn", "input_y_btn", "input_b_btn", "input_a_btn",
    )
    assert {key: hid[key] for key in keys} == {
        "input_down_btn": "3",
        "input_start_btn": "7",
        "input_l2_btn": "10",
        "input_r2_btn": "12",
        "input_x_btn": "11",
        "input_y_btn": "6",
        "input_b_btn": "8",
        "input_a_btn": "13",
    }
    assert {key: shanwan[key] for key in keys} == {
        "input_down_btn": "4",
        "input_start_btn": "8",
        "input_l2_btn": "11",
        "input_r2_btn": "13",
        "input_x_btn": "12",
        "input_y_btn": "7",
        "input_b_btn": "9",
        "input_a_btn": "14",
    }
    assert "input_l3_btn" not in hid and "input_r3_btn" not in hid
    assert "input_l3_btn" not in shanwan and "input_r3_btn" not in shanwan


def test_stage_publishes_only_the_two_udev_profiles(tmp_path: Path) -> None:
    staged = joypads.stage(tmp_path)

    assert set(staged) == {f"{joypads.AUTOCONFIG_DIR}/{name}" for name in joypads.PROFILES}

    for name, digest in staged.items():
        assert hashlib.sha256((tmp_path / name).read_bytes()).hexdigest() == digest
