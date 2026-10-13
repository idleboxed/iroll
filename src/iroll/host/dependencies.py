"""Read-only dependency checks and installation suggestions, without sudo execution.

SPDX-License-Identifier: BSD-3-Clause
"""

import platform
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from ..common import paths
from ..common.errors import BuildError
from .platforms import MIN_RUST, Toolchain
from ..common.process import Runner
from ..components.registry import Component

APT_TOOLS = {
    "cc": "build-essential",
    "c++": "build-essential",
    "ar": "binutils",
    "make": "make",
    "sh": "dash",
    "pkg-config": "pkg-config",
}
APT_LIBRARIES = {
    "x11": "libx11-dev",
    "x11-xcb": "libx11-xcb-dev",
    "xext": "libxext-dev",
    "xinerama": "libxinerama-dev",
    "xrandr": "libxrandr-dev",
    "xxf86vm": "libxxf86vm-dev",
    "gl": "libgl-dev",
    "libudev": "libudev-dev",
    "libpulse": "libpulse-dev",
    "zlib": "zlib1g-dev",
}
CARD_TOOL_PATH = "/usr/sbin:/usr/bin:/sbin:/bin"
CARD_PACKAGES = {
    "lsblk": "util-linux",
    "sfdisk": "fdisk",
    "mkfs.exfat": "exfatprogs",
    "mount": "mount",
    "umount": "mount",
}


@dataclass(frozen=True)
class DependencyReport:
    checks: tuple[str, ...]
    missing: tuple[str, ...]
    instructions: tuple[str, ...]

    @property
    def ready(self) -> bool:
        return not self.missing


def _format_status(name: str, *, found: bool) -> str:
    return f"{name} : {"OK" if found else "NOT FOUND"}"


def is_debian_host() -> bool:
    try:
        os_release = paths.OS_RELEASE.read_text(encoding="utf-8")

    except OSError:
        return False

    return bool(re.search(r"(?m)^ID(?:_LIKE)?=.*(?:debian|ubuntu)", os_release))


def check_card() -> DependencyReport:
    """Check external card tools without root, device access or driver loading."""
    absent = {tool for tool in CARD_PACKAGES if shutil.which(tool, path=CARD_TOOL_PATH) is None}
    checks = tuple(_format_status(tool, found=True) for tool in sorted(CARD_PACKAGES.keys() - absent))
    missing = [_format_status(tool, found=False) for tool in sorted(absent)]
    instructions = []

    if platform.system() != "Linux":
        missing.append("Card initialization requires Linux")

    if absent:

        if is_debian_host():
            packages = " ".join(sorted({CARD_PACKAGES[tool] for tool in absent}))
            instructions.append(f"sudo apt-get install {packages}")

        else:
            instructions.append("Install the listed tools using your distribution's package manager.")

    return DependencyReport(checks, tuple(missing), tuple(instructions))


def collect_requirements(components: tuple[Component, ...], toolchain: Toolchain) -> tuple[set[str], set[str]]:
    tools = {tool for component in components for tool in component.host_tools}
    libraries: set[str] = set()

    if any(component.recipe in ("cargo", "trampoline") for component in components):
        tools.update(("cargo", "rustc"))

        if not toolchain.profile.cross:
            tools.add(toolchain.cc)

    if any(component.recipe in ("make", "configure") for component in components):
        tools.update((toolchain.cc, toolchain.cxx, toolchain.ar, "make", "sh"))

    if any(component.recipe == "configure" for component in components):
        libraries.update(toolchain.profile.libraries)

    if not toolchain.profile.cross and any(component.name == "igui" for component in components):
        libraries.add("x11")

    if libraries:
        tools.add("pkg-config")

    return tools, libraries


def check(
    components: tuple[Component, ...],
    toolchain: Toolchain,
    runner: Runner,
    *,
    cwd: Path,
) -> DependencyReport:
    checks, missing, instructions = [], [], []

    if platform.system() != "Linux" or platform.machine() not in ("x86_64", "AMD64"):
        missing.append("This version builds on Linux x86_64 hosts only")

    valid_sdk = True
    needs_sdk = any(component.recipe not in ("cargo", "trampoline") for component in components)
    try:
        toolchain.validate(needs_sdk=needs_sdk)

    except BuildError as exc:
        missing.append(f"{exc}")
        valid_sdk = False

    tools, libraries = collect_requirements(components, toolchain)
    absent = {tool for tool in tools if shutil.which(tool) is None}
    missing.extend(_format_status(tool, found=False) for tool in sorted(absent))
    checks.extend(_format_status(tool, found=True) for tool in sorted(tools - absent))
    env = toolchain.build_environment(rust=not needs_sdk) if valid_sdk else None

    if valid_sdk and toolchain.sysroot is not None:
        checks.append(f"SDK sysroot: {toolchain.sysroot}")

    missing_rust_std = False

    if "rustc" in tools and "rustc" not in absent:
        version = runner.run(["rustc", "--version"], cwd=cwd, env=env, check=False)
        match = re.match(r"rustc (\d+)\.(\d+)\.(\d+)", version.stdout)

        if version.returncode or not match or tuple(map(int, match.groups())) < MIN_RUST:
            missing.append("Rust 1.85 or newer is required")

        target = runner.run(
            ["rustc", "--print", "target-libdir", "--target", toolchain.profile.rust_target],
            cwd=cwd,
            env=env,
            check=False,
        )
        libdir = Path(target.stdout.strip())

        if target.returncode or not libdir.is_dir() or not any(libdir.glob("libstd-*.rlib")):
            missing_rust_std = True
            missing.append(_format_status(f"Rust standard library ({toolchain.profile.rust_target})", found=False))

    missing_libs = set()

    if libraries and "pkg-config" not in absent and valid_sdk:

        for library in sorted(libraries):
            result = runner.run(["pkg-config", "--exists", library], cwd=cwd, env=env, check=False)

            if result.returncode:
                missing_libs.add(library)
                missing.append(_format_status(library, found=False))

            else:
                checks.append(_format_status(library, found=True))

    elif libraries:
        missing.append("Library checks unavailable until pkg-config and the SDK are ready")

    if is_debian_host():
        packages = {APT_TOOLS[tool] for tool in absent if tool in APT_TOOLS}

        if not toolchain.profile.cross:
            packages.update(APT_LIBRARIES[library] for library in missing_libs if library in APT_LIBRARIES)

        if packages:
            instructions.append(f"sudo apt-get install {" ".join(sorted(packages))}")

    elif absent or missing_libs:
        instructions.append("Install the listed tools/libraries using your distribution's package manager.")

    if "rustc" in tools:

        if "rustc" in absent or "cargo" in absent or any("Rust 1.85" in message for message in missing):
            instructions.append("Install/update Rust 1.85+ with rustup: https://www.rust-lang.org/tools/install")

        if missing_rust_std:
            instructions.append(f"rustup target add {toolchain.profile.rust_target}")

    if toolchain.profile.cross and needs_sdk:
        instructions.extend(
            (
                f"Place the ARMv7 hard-float SDK at ./{paths.DEFAULT_SDK_ROOT}/rk3032 "
                "or select another root via --sdk.",
                "The SDK must target glibc <= 2.29; a current distro cross compiler alone is not sufficient.",
            )
        )

        if libraries:
            instructions.append(
                f"Required target .pc files, headers and libraries in the SDK: {", ".join(sorted(libraries))}"
            )

        instructions.append(
            "Mali/GBM/EGL, kernel compatibility and application runtime still require separate hardware validation."
        )

    return DependencyReport(tuple(checks), tuple(missing), tuple(instructions))
