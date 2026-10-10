"""Read-only dependency checks and profile environments. SPDX-License-Identifier: BSD-3-Clause."""

from pathlib import Path
from typing import Any

import pytest

from iroll.host import dependencies
from iroll.rollout.build import BuildOptions
from iroll.host.platforms import PROFILES, Toolchain
from iroll.components.registry import select


def test_core_build_does_not_need_rust_gui_libraries_or_elf_inspector(options: BuildOptions, runner: Any) -> None:
    cores = select("linux-x86_64", [], cores=True)

    tools, libraries = dependencies.collect_requirements(cores, options.toolchain)
    report = dependencies.check(cores, options.toolchain, runner, cwd=options.source_root)

    assert tools == {"cc", "c++", "ar", "sh", "make"}
    assert not libraries
    assert report.ready
    assert not runner.calls


def test_missing_dependencies_offer_commands_without_executing_them(
    options: BuildOptions, runner: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(dependencies.shutil, "which", lambda tool: None if tool in ("make", "cc") else tool)
    runner.missing_libraries.add("x11")

    report = dependencies.check(options.components, options.toolchain, runner, cwd=options.source_root)

    assert not report.ready
    assert "make : NOT FOUND" in report.missing
    assert "x11 : NOT FOUND" in report.missing
    assert "libpulse : OK" in report.checks
    assert not any(
        "tool" in item.lower() or "library found" in item.lower() for item in (*report.checks, *report.missing)
    )
    assert "sudo apt-get install build-essential libx11-dev make" in report.instructions
    assert not any(argv[0] == "sudo" for argv, _, _ in runner.calls)


def test_missing_or_old_rust_gets_rustup_suggestions(options: BuildOptions, runner: Any) -> None:
    runner.rust_version = "rustc 1.84.0"

    for file in runner.libdir.iterdir():
        file.unlink()

    report = dependencies.check(options.components, options.toolchain, runner, cwd=options.source_root)

    assert not report.ready
    assert "Rust 1.85 or newer is required" in report.missing
    assert "Rust standard library (x86_64-unknown-linux-gnu) : NOT FOUND" in report.missing
    assert "rustup target add x86_64-unknown-linux-gnu" in report.instructions


def test_arm_without_sdk_reports_missing_prerequisites_not_success(options: BuildOptions, runner: Any) -> None:
    report = dependencies.check(select("rk3032", []), Toolchain(PROFILES["rk3032"]), runner, cwd=options.source_root)

    assert not report.ready
    assert any("--sdk" in item for item in report.missing)
    assert "Place the ARMv7 hard-float SDK at ./in/sdk/rk3032 or select another root via --sdk." in report.instructions
    assert not any(argv[0] == "pkg-config" for argv, _, _ in runner.calls)


@pytest.mark.parametrize(
    "absent, instruction",
    [
        (set(), None),
        ({"mkfs.exfat"}, "sudo apt-get install exfatprogs"),
        ({"mount", "umount", "sfdisk", "lsblk"}, "sudo apt-get install fdisk mount util-linux"),
    ],
)
def test_card_dependencies_use_system_path_and_deduplicate_packages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, absent: set[str], instruction: str | None,
) -> None:
    release = tmp_path / "os-release"
    release.write_text("ID=ubuntu\n")
    monkeypatch.setattr(dependencies.paths, "OS_RELEASE", release)
    monkeypatch.setattr(dependencies.platform, "system", lambda: "Linux")
    searched = []

    def which(tool: str, *, path: str) -> str | None:
        searched.append((tool, path))

        return None if tool in absent else f"/usr/bin/{tool}"

    monkeypatch.setattr(dependencies.shutil, "which", which)

    report = dependencies.check_card()

    assert report.ready == (not absent)
    assert report.checks == tuple(
        f"{tool} : OK" for tool in sorted(dependencies.CARD_PACKAGES.keys() - absent)
    )
    assert report.missing == tuple(f"{tool} : NOT FOUND" for tool in sorted(absent))
    assert report.instructions == (() if instruction is None else (instruction,))
    assert set(searched) == {(tool, dependencies.CARD_TOOL_PATH) for tool in dependencies.CARD_PACKAGES}
    assert {path.name for path in tmp_path.iterdir()} == {"os-release"}


def test_card_dependencies_do_not_suggest_apt_for_other_distributions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    release = tmp_path / "os-release"
    release.write_text("ID=fedora\n")
    monkeypatch.setattr(dependencies.paths, "OS_RELEASE", release)
    monkeypatch.setattr(dependencies.shutil, "which", lambda tool, **kwargs: None)

    report = dependencies.check_card()

    assert not report.ready
    assert "package manager" in report.instructions[0]
    assert not any("apt" in instruction for instruction in report.instructions)


def test_cross_picodrive_requires_native_cyclone_compilers(options: BuildOptions) -> None:
    toolchain = Toolchain(PROFILES["rk3032"], options.source_root, "/sdk/bin/arm-linux-gnueabihf-")

    tools, libraries = dependencies.collect_requirements(select("rk3032", ["core-picodrive"]), toolchain)

    assert {"cc", "c++", toolchain.cc, toolchain.cxx, toolchain.ar, "make", "sh"} <= tools
    assert not libraries
