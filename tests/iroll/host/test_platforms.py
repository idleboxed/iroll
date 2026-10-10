"""Content-based toolchain identities. SPDX-License-Identifier: BSD-3-Clause."""

from pathlib import Path
from typing import Any

import pytest

from iroll.common.errors import BuildError
from iroll.host.platforms import PROFILES, Toolchain


def test_toolchain_fingerprint_includes_headers_libraries_and_tools(
    tmp_path: Path, runner: Any, dependency_tools: None,
) -> None:
    toolchain = Toolchain(PROFILES["linux-x86_64"])
    env = toolchain.build_environment()
    before = toolchain.fingerprint(runner, cwd=tmp_path, env=env)

    assert toolchain.fingerprint(runner, cwd=tmp_path, env=env) == before
    (runner.headers / "new.h").write_bytes(b"new header")
    updated = toolchain.fingerprint(runner, cwd=tmp_path, env=env)
    assert updated != before
    (runner.libraries / "libc.so").write_bytes(b"new library")
    assert toolchain.fingerprint(runner, cwd=tmp_path, env=env) != updated


def test_cross_environment_uses_selected_sdk_not_host_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    sdk = tmp_path / "sdk"
    sdk.mkdir()

    for key in ("CFLAGS", "CXXFLAGS", "CPPFLAGS", "LDFLAGS", "PKG_CONFIG_PATH", "CPATH", "RUSTFLAGS"):
        monkeypatch.setenv(key, "/wrong/host")

    toolchain = Toolchain(PROFILES["rk3032"], sdk)

    toolchain.validate()
    env = toolchain.build_environment()

    assert env["CC"] == "arm-linux-gnueabihf-gcc"
    assert "-mcpu=cortex-a7" in env["CFLAGS"]
    assert env["PKG_CONFIG_SYSROOT_DIR"] == f"{sdk}"
    assert env["PKG_CONFIG_PATH"] == ""
    assert all(path.startswith(f"{sdk}") for path in env["PKG_CONFIG_LIBDIR"].split(":"))
    assert "CPATH" not in env
    assert "RUSTFLAGS" not in env
    assert "/wrong/host" not in f"{env}"


@pytest.mark.parametrize("platform", ["linux-x86_64", "rk3032"])
def test_make_environment_is_explicit_without_changing_cargo_or_sdk_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, platform: str,
) -> None:
    caller = {
        "PATH": "/synthetic/bin", "HOME": "/synthetic/home", "TMPDIR": "/synthetic/tmp", "SOURCE_DATE_EPOCH": "100",
        "TERM": "synthetic-terminal", "XDG_SESSION_ID": "synthetic-session", "SSH_AUTH_SOCK": "/synthetic/socket",
        "MAKEFILES": "/untracked/makefile", "CONFIG_SITE": "/untracked/config", "LD_PRELOAD": "/untracked/library",
        "CARGO_HOME": "/synthetic/cargo", "RUSTUP_HOME": "/synthetic/rustup", "UNTRACKED_MAKE_OPTION": "1",
    }

    for key, value in caller.items():
        monkeypatch.setenv(key, value)

    toolchain = Toolchain(PROFILES[platform], tmp_path if platform == "rk3032" else None)

    env = toolchain.make_environment()

    expected = {key: caller[key] for key in ("PATH", "HOME", "TMPDIR", "SOURCE_DATE_EPOCH")}
    expected.update(LC_ALL="C", CC=toolchain.cc, CXX=toolchain.cxx, AR=toolchain.ar, PKG_CONF_PATH="pkg-config")

    if platform == "rk3032":
        configured = toolchain.build_environment()

        for key in (
            "CFLAGS", "CXXFLAGS", "CPPFLAGS", "LDFLAGS", "PKG_CONFIG_SYSROOT_DIR", "PKG_CONFIG_LIBDIR",
            "PKG_CONFIG_PATH", "PKG_CONFIG_ALLOW_CROSS",
        ):
            expected[key] = configured[key]

    assert env == expected

    for rust in (False, True):
        inherited = toolchain.build_environment(rust=rust)
        assert all(inherited[key] == value for key, value in caller.items())


@pytest.mark.parametrize("prefix", ["", "sdk with spaces/arm-", "arm-;echo"])
def test_cross_prefix_rejects_shell_unsafe_values(tmp_path: Path, prefix: str) -> None:

    with pytest.raises(BuildError, match="SDK paths and cross prefixes"):
        Toolchain(PROFILES["rk3032"], tmp_path, prefix).validate()


def test_native_sysroot_is_rejected(tmp_path: Path) -> None:

    with pytest.raises(BuildError, match="only supported with rk3032"):
        Toolchain(PROFILES["linux-x86_64"], tmp_path).validate()
