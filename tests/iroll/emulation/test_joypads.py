"""RetroArch controller profiles from the board input test.

SPDX-License-Identifier: BSD-3-Clause
"""

import hashlib
from importlib.resources import files
from pathlib import Path

import pytest

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


@pytest.mark.parametrize(
    "name,device,vendor,product,buttons,axes,directions",
    [
        (
            "045e-028e.cfg", "Microsoft X-Box 360 pad", "1118", "654",
            {"b": "0", "a": "1", "y": "2", "x": "3", "select": "6", "start": "7",
             "l": "4", "r": "5", "l3": "9", "r3": "10", "menu_toggle": "8"},
            {"l_x_minus": "-0", "l_x_plus": "+0", "l_y_minus": "-1", "l_y_plus": "+1",
             "r_x_minus": "-3", "r_x_plus": "+3", "r_y_minus": "-4", "r_y_plus": "+4",
             "l2": "+2", "r2": "+5"},
            {"input_up_btn": "h0up", "input_down_btn": "h0down",
             "input_left_btn": "h0left", "input_right_btn": "h0right"},
        ),
        (
            "0079-181c.cfg", "ShanWan Wireless Gamepad", "121", "6172",
            {"b": "0", "a": "1", "y": "3", "x": "4", "select": "10", "start": "11",
             "l": "6", "r": "7", "l2": "8", "r2": "9", "l3": "13", "r3": "14", "menu_toggle": "12"},
            {"l_x_minus": "-0", "l_x_plus": "+0", "l_y_minus": "-1", "l_y_plus": "+1",
             "r_x_minus": "-2", "r_x_plus": "+2", "r_y_minus": "-3", "r_y_plus": "+3"},
            {"input_up_axis": "-1", "input_down_axis": "+1", "input_left_axis": "-0", "input_right_axis": "+0"},
        ),
    ],
)
def test_new_profiles_keep_measured_codes_separate_from_retroarch_indices(
    name: str, device: str, vendor: str, product: str,
    buttons: dict[str, str], axes: dict[str, str], directions: dict[str, str],
) -> None:
    resource = files("iroll").joinpath("resources", "sd", "common", "system", "retroarch", "padconf")

    profile = parse_profile(resource.joinpath(name).read_text())

    expected = {
        "input_driver": "udev", "input_device": device,
        "input_vendor_id": vendor, "input_product_id": product,
        **{f"input_{button}_btn": index for button, index in buttons.items()},
        **{f"input_{axis}_axis": index for axis, index in axes.items()},
        **directions,
    }
    assert profile == expected


def test_stage_publishes_only_the_registered_udev_profiles(tmp_path: Path) -> None:
    staged = joypads.stage(tmp_path)

    assert set(staged) == {f"{joypads.AUTOCONFIG_DIR}/{name}" for name in joypads.PROFILES}
    assert set(joypads.PROFILES) == {"0810-0001.cfg", "20bc-5500.cfg", "045e-028e.cfg", "0079-181c.cfg"}

    for name, digest in staged.items():
        assert hashlib.sha256((tmp_path / name).read_bytes()).hexdigest() == digest
