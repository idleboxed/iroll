"""Discover a relocatable ARM hard-float SDK. SPDX-License-Identifier: BSD-3-Clause."""

import os
import re
from pathlib import Path

from ..common import paths
from ..common.errors import BuildError
from ..common.process import Runner
from ..host.platforms import Profile, Toolchain


def discover_sdk(profile: Profile, sdk: Path | None, runner: Runner, *, cwd: Path) -> Toolchain:
    """Resolve a conventional relocatable SDK; only host compiler queries run."""

    if sdk is None:

        if not profile.cross:
            return Toolchain(profile)

        sdk = cwd / paths.DEFAULT_SDK_ROOT / profile.name

    if not profile.cross:
        raise BuildError("--sdk is only supported with rk3032")

    sdk = sdk.resolve()
    pending = paths.get_sdk_pending_path(sdk)

    if pending.exists() or pending.is_symlink():
        raise BuildError(f"SDK preparation is incomplete: {pending}")

    if not re.fullmatch(r"[A-Za-z0-9_./+:-]+", f"{sdk}"):
        raise BuildError("SDK path must not contain spaces or shell metacharacters")

    if not sdk.is_dir():
        raise BuildError(f"SDK directory not found: {sdk}")

    # Inspect only conventional tool directories, not a recursive search through
    # target programs. Short aliases such as arm-linux-gcc are not extra SDKs.
    candidates = sorted(
        path for directory in (sdk / "bin", sdk / "host/bin")
        for path in directory.glob("arm*-linux-gnueabihf-gcc")
    )

    if not candidates:
        raise BuildError(f"No ARM hard-float GCC found in SDK bin/ or host/bin/: {sdk}")

    if len(candidates) != 1:
        listing = ", ".join(f"{path}" for path in candidates)
        raise BuildError(f"Ambiguous SDK: multiple ARM compilers found: {listing}")

    compiler = candidates[0]
    prefix = f"{compiler}"[:-3]

    for suffix in ("gcc", "g++", "ar"):
        path = Path(f"{prefix}{suffix}")

        if not path.is_file() or not os.access(path, os.X_OK):
            raise BuildError(f"SDK tool is missing or not executable: {path}")

    machine = runner.run([f"{compiler}", "-dumpmachine"], cwd=cwd).stdout.strip()

    if not re.fullmatch(r"arm[A-Za-z0-9_+.-]*-linux-gnueabihf", machine):
        raise BuildError(f"SDK compiler does not target ARM glibc hard-float: {machine!r}")

    value = runner.run([f"{compiler}", "-print-sysroot"], cwd=cwd).stdout.strip()
    reported = Path(value)

    if not value or not reported.is_absolute():
        raise BuildError("SDK compiler must report an absolute sysroot")

    sysroot = reported.resolve()

    if not sysroot.is_relative_to(sdk):
        raise BuildError(f"Compiler sysroot is outside the selected SDK: {sysroot}")

    toolchain = Toolchain(profile, sysroot, prefix)
    toolchain.validate()
    return toolchain
