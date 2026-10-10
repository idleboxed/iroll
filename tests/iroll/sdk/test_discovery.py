"""SDK discovery uses synthetic host compiler responses, never target executables.

SPDX-License-Identifier: BSD-3-Clause
"""

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from iroll.common.errors import BuildError
from iroll.host.platforms import PROFILES
from iroll.sdk.discovery import discover_sdk


@pytest.mark.parametrize("tools_directory", ["bin", "host/bin"])
def test_sdk_discovers_complete_toolchain_and_ignores_short_alias(
    sdk: Path, runner: Any, tools_directory: str,
) -> None:
    (sdk / "host").mkdir()
    (sdk / "bin").rename(sdk / "tools")
    (sdk / "tools").rename(sdk / tools_directory)
    (sdk / tools_directory / "arm-linux-gcc").symlink_to(f"{runner.sdk_machine}-gcc")

    toolchain = discover_sdk(PROFILES["rk3032"], sdk, runner, cwd=sdk.parent)

    prefix = f"{sdk / tools_directory / f"{runner.sdk_machine}-"}"
    assert toolchain.cc == f"{prefix}gcc"
    assert toolchain.cxx == f"{prefix}g++"
    assert toolchain.ar == f"{prefix}ar"
    assert f"{toolchain.sysroot}" == runner.sdk_sysroot
    assert [argv for argv, _, _ in runner.calls] == [
        [f"{prefix}gcc", "-dumpmachine"], [f"{prefix}gcc", "-print-sysroot"],
    ]
    assert toolchain.build_environment()["PKG_CONFIG_SYSROOT_DIR"] == runner.sdk_sysroot


@pytest.mark.parametrize("platform,directory,prepare", [
    pytest.param("rk3032", "missing", lambda path: None, id="missing"),
    pytest.param("rk3032", "empty", Path.mkdir, id="empty"),
    pytest.param("rk3032", "space in path", Path.mkdir, id="unsafe"),
    pytest.param("linux-x86_64", ".", lambda path: None, id="native"),
])
def test_invalid_sdk_refuses_before_any_compiler_query(
    sdk: Path, runner: Any, platform: str, directory: str, prepare: Callable[[Path], None],
) -> None:
    sdk = sdk / directory
    prepare(sdk)

    with pytest.raises(BuildError, match="SDK|--sdk"):
        discover_sdk(PROFILES[platform], sdk, runner, cwd=sdk.parent)

    assert not runner.calls


def test_sdk_ambiguity_is_not_resolved_by_guessing(sdk: Path, runner: Any) -> None:
    compiler = sdk / "bin/arm-other-linux-gnueabihf-gcc"
    compiler.write_bytes(b"another synthetic tool")
    compiler.chmod(0o755)

    with pytest.raises(BuildError, match="Ambiguous SDK"):
        discover_sdk(PROFILES["rk3032"], sdk, runner, cwd=sdk.parent)

    assert not runner.calls


@pytest.mark.parametrize("suffix", ["gcc", "g++", "ar"])
@pytest.mark.parametrize("damage", [
    pytest.param(Path.unlink, id="missing"),
    pytest.param(lambda path: path.chmod(0o644), id="non-executable"),
    pytest.param(lambda path: (path.unlink(), path.symlink_to("missing-tool")), id="dangling-symlink"),
])
def test_incomplete_sdk_refuses_without_path_fallback(
    sdk: Path, runner: Any, suffix: str, damage: Callable[[Path], object],
) -> None:
    path = sdk / "bin" / f"{runner.sdk_machine}-{suffix}"
    damage(path)

    with pytest.raises(BuildError, match="GCC found|missing or not executable"):
        discover_sdk(PROFILES["rk3032"], sdk, runner, cwd=sdk.parent)

    assert not runner.calls


@pytest.mark.parametrize("machine", ["", "aarch64-linux-gnu", "arm-linux-gnueabi", "arm-linux-musleabihf"])
def test_compiler_report_must_confirm_arm_glibc_hard_float(sdk: Path, runner: Any, machine: str) -> None:
    runner.sdk_machine = machine

    with pytest.raises(BuildError, match="does not target ARM glibc hard-float"):
        discover_sdk(PROFILES["rk3032"], sdk, runner, cwd=sdk.parent)

    assert len(runner.calls) == 1


@pytest.mark.parametrize("report_sysroot", [
    pytest.param(lambda sdk, outside: "", id="empty"),
    pytest.param(lambda sdk, outside: "relative/sysroot", id="relative"),
    pytest.param(lambda sdk, outside: f"{sdk / "missing"}", id="missing"),
    pytest.param(lambda sdk, outside: f"{outside}", id="outside"),
    pytest.param(lambda sdk, outside: f"{sdk / "external"}", id="escaped-symlink"),
])
def test_sysroot_must_be_existing_and_inside_selected_sdk(
    sdk: Path, runner: Any, tmp_path: Path, report_sysroot: Callable[[Path, Path], str],
) -> None:
    (sdk / "external").symlink_to(tmp_path)
    runner.sdk_sysroot = report_sysroot(sdk, tmp_path)

    with pytest.raises(BuildError, match="sysroot|requires --sdk"):
        discover_sdk(PROFILES["rk3032"], sdk, runner, cwd=sdk.parent)


def test_sdk_compiler_failure_is_not_silently_ignored(sdk: Path, runner: Any) -> None:
    runner.fail_command = f"{sdk / "bin" / f"{runner.sdk_machine}-gcc"}"

    with pytest.raises(BuildError, match="Synthetic compiler failure"):
        discover_sdk(PROFILES["rk3032"], sdk, runner, cwd=sdk.parent)

    assert len(runner.calls) == 1


def test_sdk_discovery_does_not_fall_back_to_old_working_directory_layout(sdk: Path, runner: Any) -> None:
    cwd = sdk.parent
    original_sdk = cwd / "original-sdk"
    sdk.rename(original_sdk)
    old_sdk = cwd / "sdk/rk3032"
    old_sdk.parent.mkdir()
    original_sdk.rename(old_sdk)
    runner.sdk_sysroot = f"{old_sdk / "arm-buildroot-linux-gnueabihf/sysroot"}"

    with pytest.raises(BuildError, match="SDK directory not found: .*in/sdk/rk3032"):
        discover_sdk(PROFILES["rk3032"], None, runner, cwd=cwd)

    assert not runner.calls
