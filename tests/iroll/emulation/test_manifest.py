"""Shipped RetroArch menu visibility and verified controller bindings.

SPDX-License-Identifier: BSD-3-Clause
"""

from pathlib import Path
from typing import Any

import pytest

from iroll.emulation.manifest import build_runtime_manifest
from iroll.host.platforms import PROFILES
from iroll.components.registry import COMPONENTS


RETROPAD_BUTTONS = {
    "b": 0, "y": 1, "select": 2, "start": 3,
    "up": 4, "down": 5, "left": 6, "right": 7,
    "a": 8, "x": 9, "l": 10, "r": 11, "l2": 12, "r2": 13, "l3": 14, "r3": 15,
}

CONTROLLER_PROFILES = [
    ("gpsp_libretro.so", "GBA", "gpSP", "a b l r select start up down left right"),
    ("gambatte_libretro.so", "GB", "Gambatte", "a b select start up down left right"),
    ("gambatte_libretro.so", "GBC", "Gambatte", "a b select start up down left right"),
    ("nestopia_libretro.so", "NES", "Nestopia", "a b x y select start up down left right"),
    ("nestopia_libretro.so", "FDS", "Nestopia", "a b x y select start up down left right"),
    ("snes9x2010_libretro.so", "SNES", "Snes9x 2010", "a b x y l r select start up down left right"),
    ("picodrive_libretro.so", "MD", "PicoDrive", "a b x y l r select start up down left right"),
    ("picodrive_libretro.so", "SCD", "PicoDrive", "a b x y l r select start up down left right"),
    ("picodrive_libretro.so", "32X", "PicoDrive", "a b x y l r select start up down left right"),
    ("picodrive_libretro.so", "SMS", "PicoDrive", "a b start up down left right"),
    ("picodrive_libretro.so", "GG", "PicoDrive", "a b start up down left right"),
    ("picodrive_libretro.so", "SG1000", "PicoDrive", "a b up down left right"),
    ("picodrive_libretro.so", "SC3000", "PicoDrive", "a b up down left right"),
    ("picodrive_libretro.so", "PICO", "PicoDrive", "a b x y l r start up down left right"),
    ("pcsx_rearmed_libretro.so", "PSX", "PCSX-ReARMed", " ".join(RETROPAD_BUTTONS)),
    ("mupen64plus_next_libretro.so", "N64", "Mupen64Plus-Next", "a b x y l r l2 r2 select start up down left right"),
]


PLATFORM_REMAPS = {
    **{platform: {"btn_a": 0, "btn_b": 8} for platform in ("GBA", "GB", "GBC", "NES", "FDS", "SNES")},
    "PSX": {"btn_x": 1, "btn_y": 9},
    "N64": {f"stk_r_{axis}{sign}": -1 for axis in ("x", "y") for sign in ("+", "-")},
}
PLAYER_REMAPS = {("FDS", 1): {"btn_x": 10, "btn_y": 11}}
SEGA_CORE_OPTIONS = {"picodrive_input1": "6 button pad", "picodrive_input2": "6 button pad"}
PLATFORM_CORE_OPTIONS = {
    **{platform: SEGA_CORE_OPTIONS for platform in ("MD", "SCD", "32X")},
    "N64": {"mupen64plus-alt-map": "True"},
}
CORE_OPTION_FIELDS = {"N64": ("mupen64plus-alt-map",)}


@pytest.mark.parametrize("platform,expected", [
    pytest.param("rk3032", {"audio_driver": "alsa", "audio_latency": 232}, id="arm-audio"),
    pytest.param("linux-x86_64", {"audio_driver": "pulse"}, id="desktop-audio"),
])
def test_audio_driver_and_board_latency_use_platform_defaults(platform: str, expected: dict[str, str | int]) -> None:
    components = {"retro": {"binary": "retroarch/retroarch", "game_platforms": []}}

    settings = build_runtime_manifest(components, PROFILES[platform])["settings"]

    assert {key: settings[key] for key in ("audio_driver", "audio_latency") if key in settings} == expected


def test_arm_profile_retains_independent_joypads_without_keyboard_merging() -> None:
    components = {"retro": {"binary": "retroarch/retroarch", "game_platforms": []}}

    settings = build_runtime_manifest(components, PROFILES["rk3032"])["settings"]

    assert settings["input_driver"] == "udev"
    assert settings["input_joypad_driver"] == "udev"
    assert settings["input_autodetect_enable"] is True
    assert settings["input_menu_toggle_gamepad_combo"] == 4

    for player in (1, 2):
        assert settings[f"input_player{player}_joypad_index"] == player - 1
        assert settings[f"input_libretro_device_p{player}"] == 1

        for button in ("a", "b", "x", "y", "start", "select", "up", "down", "left", "right"):
            assert f"input_player{player}_{button}_btn" not in settings

        for binding in (
            "a", "b", "x", "y", "start", "select", "up", "down", "left", "right",
            "l", "r", "l2", "r2", "l3", "r3", "l_x_plus", "l_x_minus", "l_y_plus", "l_y_minus",
            "r_x_plus", "r_x_minus", "r_y_plus", "r_y_minus", "turbo",
        ):
            assert settings[f"input_player{player}_{binding}"] == "nul"


@pytest.mark.parametrize("host,input_driver,binding", [
    pytest.param("rk3032", "udev", "nul", id="arm-keyboard-disabled"),
    pytest.param("linux-x86_64", "x", None, id="desktop-keyboard-defaults"),
])
def test_host_keyboard_policy_does_not_change_desktop_defaults(
    host: str, input_driver: str, binding: str | None,
) -> None:
    components = {"retro": {"binary": "retroarch/retroarch", "game_platforms": []}}
    keyboard_keys = (
        "input_menu_toggle", "input_exit_emulator", "input_reset",
        "input_toggle_fast_forward", "input_hold_fast_forward",
        "input_hold_slowmotion", "input_rewind", "input_pause_toggle", "input_frame_advance", "input_audio_mute",
        "input_volume_up", "input_volume_down", "input_load_state", "input_save_state", "input_state_slot_increase",
        "input_state_slot_decrease", "input_shader_toggle", "input_shader_next",
        "input_shader_prev", "input_cheat_toggle",
        "input_cheat_index_plus", "input_cheat_index_minus", "input_screenshot", "input_grab_mouse_toggle",
        "input_game_focus_toggle", "input_toggle_fullscreen", "input_desktop_menu_toggle", "input_fps_toggle",
        "input_netplay_game_watch", "input_netplay_player_chat", "input_player1_a",
    )

    settings = build_runtime_manifest(components, PROFILES[host])["settings"]

    assert settings["input_driver"] == input_driver
    assert {key: settings[key] for key in keyboard_keys if key in settings} == {
        key: binding for key in keyboard_keys if binding is not None
    }


@pytest.mark.parametrize("host", ["rk3032", "linux-x86_64"])
@pytest.mark.parametrize("binary,platform,library_name,original_buttons", CONTROLLER_PROFILES)
def test_platform_remaps_preserve_original_buttons_and_only_agreed_exceptions_on_both_hosts(
    host: str, binary: str, platform: str, library_name: str, original_buttons: str,
) -> None:
    components = {
        "retro": {"binary": "retroarch/retroarch", "game_platforms": []},
        "core": {"binary": f"retroarch/cores/{binary}", "game_platforms": [platform]},
    }

    original = original_buttons.split()
    expected_remap = {
        f"input_player{player}_btn_{button}": -1
        for player in (1, 2) for button in RETROPAD_BUTTONS if button not in original
    }

    for player in (1, 2):
        overrides = PLATFORM_REMAPS.get(platform, {}) | PLAYER_REMAPS.get((platform, player), {})
        expected_remap.update({
            f"input_player{player}_{name}": value for name, value in overrides.items()
        })

    entry = build_runtime_manifest(components, PROFILES[host])["cores"][platform]

    assert entry["library_name"] == library_name
    assert entry["remap"] == expected_remap
    assert all(
        entry["remap"].get(f"input_player{player}_btn_{button}", RETROPAD_BUTTONS[button]) >= 0
        for player in (1, 2) for button in original
    )
    options = entry.get("core_options", {})
    assert {key: options[key] for key in CORE_OPTION_FIELDS.get(platform, options)} == (
        PLATFORM_CORE_OPTIONS.get(platform, {})
    )
    assert ("core_options" in entry) == (platform in PLATFORM_CORE_OPTIONS)


@pytest.mark.parametrize("host", ["rk3032", "linux-x86_64"])
@pytest.mark.parametrize("binary,platform", [
    ("foreign.so", platform) for _, platform, _, _ in CONTROLLER_PROFILES
] + [(Path(component.destination).name, "OTHER") for component in COMPONENTS if component.core] + [
    (Path(component.destination).name, "GBA" if "NES" in component.game_platforms else "NES")
    for component in COMPONENTS if component.core
])
def test_controller_profiles_do_not_change_other_cores_or_platforms(binary: str, platform: str, host: str) -> None:
    components = {
        "retro": {"binary": "retroarch/retroarch", "game_platforms": []},
        "core": {"binary": f"retroarch/cores/{binary}", "game_platforms": [platform]},
    }
    expected_settings = build_runtime_manifest({"retro": components["retro"]}, PROFILES[host])["settings"]

    manifest = build_runtime_manifest(components, PROFILES[host])

    assert "remap" not in manifest["cores"][platform]
    assert "library_name" not in manifest["cores"][platform]
    assert "core_options" not in manifest["cores"][platform]
    assert manifest["settings"] == expected_settings

    assert manifest["settings"]["auto_remaps_enable"] is False
    assert manifest["settings"]["remap_save_on_exit"] is False
    assert manifest["settings"]["input_menu_toggle_gamepad_combo"] == 4


def test_controller_matrix_covers_every_registered_platform() -> None:
    registered = {
        (Path(component.destination).name, platform)
        for component in COMPONENTS if component.core for platform in component.game_platforms
    }

    assert {(binary, platform) for binary, platform, _, _ in CONTROLLER_PROFILES} == registered


def test_game_menu_hides_recording_replay_and_frame_throttle_settings(emulation_settings: dict[str, Any]) -> None:
    hidden = (
        "quick_menu_show_start_recording",
        "quick_menu_show_start_streaming",
        "quick_menu_show_replay",
        "settings_show_frame_throttle",
        "settings_show_recording",
    )

    assert all(emulation_settings[key] is False for key in hidden)
    assert "quick_menu_show_fastforward" not in emulation_settings
    assert emulation_settings.get("content_show_settings", True) is True


def test_game_menu_hides_favorites_latency_rewind_overrides_and_cheats(emulation_settings: dict[str, Any]) -> None:
    hidden = (
        "quick_menu_show_add_to_favorites",
        "menu_show_latency",
        "menu_show_rewind",
        "quick_menu_show_save_core_overrides",
        "quick_menu_show_save_content_dir_overrides",
        "quick_menu_show_save_game_overrides",
        "quick_menu_show_cheats",
    )

    assert all(emulation_settings[key] is False for key in hidden)

    for key in (
        "quick_menu_show_shaders",
        "quick_menu_show_information",
        "quick_menu_show_options",
    ):
        assert emulation_settings.get(key, True) is True


def test_game_menu_preserves_state_screenshot_and_exit_actions(emulation_settings: dict[str, Any]) -> None:
    visible = (
        "quick_menu_show_save_load_state",
        "quick_menu_show_take_screenshot",
        "quick_menu_show_close_content",
        "menu_show_quit_retroarch",
    )

    assert all(emulation_settings[key] is True for key in visible)
    assert emulation_settings["menu_driver"] == "rgui"
    assert emulation_settings["menu_pause_libretro"] is True
    assert emulation_settings["quit_on_close_content"] == 1


def test_frontend_turbo_is_explicitly_disabled_on_both_hosts(emulation_settings: dict[str, Any]) -> None:
    assert emulation_settings["input_turbo_enable"] is False


def test_unmeasured_trigger_indices_do_not_replace_start_select_combo(emulation_settings: dict[str, Any]) -> None:
    assert emulation_settings["input_menu_toggle_gamepad_combo"] == 4

    for key in (
        "input_enable_hotkey_btn",
        "input_menu_toggle_btn",
        "input_save_state_btn",
        "input_load_state_btn",
    ):
        assert key not in emulation_settings


def test_sram_checks_every_minute_without_automatic_state_or_configuration_saves(
    emulation_settings: dict[str, Any],
) -> None:
    assert emulation_settings["autosave_interval"] == 60

    for key in (
        "config_save_on_exit",
        "remap_save_on_exit",
        "savestate_auto_save",
        "savestate_auto_load",
        "savestate_auto_index",
    ):
        assert emulation_settings[key] is False

    assert "savefile_directory" not in emulation_settings


@pytest.mark.parametrize("platform", ["linux-x86_64", "rk3032"])
def test_runtime_manifest_supplies_nestopia_remaps_on_both_hosts(platform: str) -> None:
    records = {
        "retro": {"binary": "retroarch/retroarch", "game_platforms": []},
        "nestopia": {"binary": "retroarch/cores/nestopia_libretro.so", "game_platforms": ["NES", "FDS"]},
    }

    manifest = build_runtime_manifest(records, PROFILES[platform])
    nestopia_remap = {
        "input_player1_btn_a": 0, "input_player1_btn_b": 8,
        "input_player2_btn_a": 0, "input_player2_btn_b": 8,
        **{
            f"input_player{player}_btn_{button}": -1
            for player in (1, 2) for button in ("l", "r", "l2", "r2", "l3", "r3")
        },
    }

    assert manifest["cores"]["NES"] == {
        "path": "retroarch/cores/nestopia_libretro.so", "shader_preset": "retroarch/shaders/presets/NES.glslp",
        "library_name": "Nestopia", "remap": nestopia_remap,
    }
    assert manifest["cores"]["FDS"] == {
        "path": "retroarch/cores/nestopia_libretro.so", "library_name": "Nestopia",
        "remap": {
            "input_player1_btn_x": 10, "input_player1_btn_y": 11,
            **nestopia_remap,
        },
        "shader_preset": "retroarch/shaders/presets/FDS.glslp",
    }
    assert manifest["settings"]["remap_save_on_exit"] is False
    assert manifest["settings"]["auto_remaps_enable"] is False

@pytest.mark.parametrize("binary,platform,expected", [
    pytest.param("mupen64plus_next_libretro.so", "N64", {
        "system_directory": "ram",
        "core_options": {
            "mupen64plus-rdp-plugin": "gliden64", "mupen64plus-rsp-plugin": "hle",
            "mupen64plus-43screensize": "320x240", "mupen64plus-aspect": "4:3",
            "mupen64plus-EnableCopyDepthToRDRAM": "Off",
            "mupen64plus-txHiresEnable": "False", "mupen64plus-EnableTextureCache": "False",
            "mupen64plus-EnableEnhancedTextureStorage": "False", "mupen64plus-EnableEnhancedHighResStorage": "False",
            "mupen64plus-alt-map": "True",
        },
        "library_name": "Mupen64Plus-Next",
        "remap": {
            **{f"input_player{player}_btn_{button}": -1 for player in (1, 2) for button in ("l3", "r3")},
            **{
                f"input_player{player}_stk_r_{axis}{sign}": -1
                for player in (1, 2) for axis in ("x", "y") for sign in ("+", "-")
            },
        },
    }, id="matching-binary-and-platform"),
    pytest.param("foreign.so", "N64", {}, id="foreign-binary"),
    pytest.param("mupen64plus_next_libretro.so", "OTHER", {}, id="other-platform"),
    pytest.param("foreign.so", "OTHER", {}, id="neither-matches"),
])
def test_n64_runtime_profile_is_scoped_to_exact_binary_and_platform(
    binary: str, platform: str, expected: dict[str, Any],
) -> None:
    records = {
        "retro": {"binary": "retroarch/retroarch", "game_platforms": []},
        "candidate": {"binary": f"retroarch/cores/{binary}", "game_platforms": [platform]},
    }

    entry = build_runtime_manifest(records, PROFILES["rk3032"])["cores"][platform]

    assert {
        key: entry[key] for key in ("system_directory", "core_options", "library_name", "remap") if key in entry
    } == expected


@pytest.mark.parametrize("host", ["linux-x86_64", "rk3032"])
def test_defaults_are_per_platform_not_per_shared_core(host: str) -> None:
    components = {
        component.name: {"binary": component.destination, "game_platforms": component.game_platforms}
        for component in COMPONENTS if component.name == "retro" or component.core
    }

    document = build_runtime_manifest(components, PROFILES[host])

    for platform, entry in document["cores"].items():
        assert entry["shader_preset"] == f"retroarch/shaders/presets/{platform}.glslp"

    assert document["cores"]["NES"]["path"] == document["cores"]["FDS"]["path"]
    assert document["cores"]["NES"]["shader_preset"] != document["cores"]["FDS"]["shader_preset"]
    assert document["settings"]["auto_shaders_enable"] is False
