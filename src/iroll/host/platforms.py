"""Build profiles and isolated native/cross environments.

RK3032 is a cross-build profile, not a claim of working hardware integration.
SPDX-License-Identifier: BSD-3-Clause
"""

import hashlib
import json
import os
import re
import shlex
import shutil
from dataclasses import dataclass
from pathlib import Path

from ..common import paths
from ..common.errors import BuildError
from ..common.files import fingerprint_paths
from ..common.process import Runner

COMMON_CONFIGURE = (
    "--disable-debug",
    "--enable-rgui",
    "--disable-ozone",
    "--disable-xmb",
    "--disable-materialui",
    "--disable-qt",
    "--disable-ffmpeg",
    "--disable-mpv",
    "--disable-vulkan",
    "--disable-wayland",
    "--disable-networking",
    "--disable-cheevos",
    "--disable-discord",
    "--disable-online_updater",
    "--disable-gfx_widgets",
)
ARM_FLAGS = ("-mcpu=cortex-a7", "-mfpu=neon-vfpv4", "-mfloat-abi=hard", "-marm")
MIN_RUST = (1, 85, 0)
DEFAULT_CROSS_PREFIX = "arm-linux-gnueabihf-"


@dataclass(frozen=True)
class Profile:
    name: str
    architecture: str
    rust_target: str
    configure: tuple[str, ...]
    libraries: tuple[str, ...]
    cross: bool = False


PROFILES = {
    "linux-x86_64": Profile(
        "linux-x86_64",
        "x86_64",
        "x86_64-unknown-linux-gnu",
        ("--enable-x11", "--enable-opengl", "--enable-udev", "--enable-pulse", "--disable-kms"),
        ("x11", "x11-xcb", "xext", "xinerama", "xrandr", "xxf86vm", "gl", "libudev", "libpulse", "zlib"),
    ),
    "rk3032": Profile(
        "rk3032",
        "arm",
        "armv7-unknown-linux-musleabihf",
        (
            "--host=arm-linux-gnueabihf",
            "--disable-x11",
            "--disable-opengl",
            "--enable-opengles",
            "--enable-egl",
            "--enable-kms",
            "--enable-udev",
            "--enable-alsa",
            "--disable-pulse",
            "--disable-slang",
            "--disable-glslang",
        ),
        ("egl", "glesv2", "gbm", "libdrm", "libudev", "alsa", "zlib"),
        cross=True,
    ),
}
DEFAULT_PLATFORM = "linux-x86_64"


@dataclass(frozen=True)
class Toolchain:
    profile: Profile
    sysroot: Path | None = None
    prefix: str = DEFAULT_CROSS_PREFIX

    def validate(self, *, needs_sdk: bool = True) -> None:
        """Check whether SDK settings are appropriate and complete for this platform."""

        if not self.profile.cross:

            if self.sysroot is not None:
                raise BuildError("--sdk is only supported with rk3032")

            return

        if self.sysroot is None and not needs_sdk:
            return

        if self.sysroot is None or not self.sysroot.is_dir():
            raise BuildError("rk3032 requires --sdk pointing to a compatible ARM hard-float development SDK")

        # Upstream configure/Makefiles split compiler variables as shell words.

        for value in (f"{self.sysroot}", self.prefix):

            if not value or not re.fullmatch(r"[A-Za-z0-9_./+:-]+", value):
                raise BuildError("SDK paths and cross prefixes must not contain spaces or shell metacharacters")

    @property
    def cc(self) -> str:
        """Return the native or cross C compiler command."""
        return f"{self.prefix}gcc" if self.profile.cross else "cc"

    @property
    def cxx(self) -> str:
        """Return the native or cross C++ compiler command."""
        return f"{self.prefix}g++" if self.profile.cross else "c++"

    @property
    def ar(self) -> str:
        """Return the native or cross archive-tool command."""
        return f"{self.prefix}ar" if self.profile.cross else "ar"

    def fingerprint(self, runner: Runner, *, cwd: Path, env: dict[str, str]) -> str:
        """Identify compilers, system headers/libraries and the selected SDK once per run."""
        roots: dict[Path, bool] = {}
        probes = []
        prefix = self.prefix if self.profile.cross else ""

        for tool in (self.cc, self.cxx, self.ar, f"{prefix}as", f"{prefix}ld", "make", "sh", "pkg-config"):
            executable = shutil.which(tool, path=env.get("PATH"))

            if executable:
                roots[Path(executable)] = False

        if self.sysroot is not None:
            roots[self.sysroot] = True

        for compiler, language in ((self.cc, "c"), (self.cxx, "c++")):
            sysroot = [f"--sysroot={self.sysroot}"] if self.sysroot else []
            search = runner.run([compiler, *sysroot, "-print-search-dirs"], cwd=cwd, env=env).stdout
            probes.append(search)

            for line in search.splitlines():

                if line.startswith(("install:", "programs:", "libraries:")):

                    for value in line.split(":", 1)[1].strip().removeprefix("=").split(os.pathsep):

                        if value and Path(value).is_absolute():
                            directory = Path(value).resolve()
                            roots.setdefault(directory, line.startswith("install:"))

            headers = runner.run(
                [compiler, *sysroot, "-E", "-x", language, "-", "-v"], cwd=cwd, env=env, input_data="",
            ).stdout
            listing = headers.partition("#include <...> search starts here:")[2].partition("End of search list.")[0]

            if not listing.strip():
                raise BuildError(f"Compiler did not report system header paths: {compiler}")

            for line in listing.splitlines():
                directory = Path(line.strip())

                if directory.is_absolute():
                    roots[directory.resolve()] = True

        # pkg-config may add headers/libraries outside the compiler's default paths.

        if shutil.which("pkg-config", path=env.get("PATH")):

            for library in self.profile.libraries:
                result = runner.run(
                    ["pkg-config", "--cflags", "--libs", library], cwd=cwd, env=env, check=False,
                )
                probes.append([result.returncode, result.stdout])

                if result.returncode == 0:

                    for flag in shlex.split(result.stdout):

                        if flag.startswith(("-I", "-L")) and Path(flag[2:]).is_absolute():
                            directory = Path(flag[2:]).resolve()
                            roots[directory] = roots.get(directory, False) or flag.startswith("-I")

        return hashlib.sha256(json.dumps([probes, fingerprint_paths(roots)]).encode()).hexdigest()

    def build_environment(self, *, rust: bool = False) -> dict[str, str]:
        """Prepare a build environment without leaking host headers or caller target flags."""
        env = dict(os.environ)
        # Do not accidentally use host headers, caller's target, or native CPU flags.

        for key in tuple(env):

            if key.startswith(("CARGO_TARGET_", "PKG_CONFIG_")) or key in {
                "RUSTFLAGS",
                "CARGO_ENCODED_RUSTFLAGS",
                "CARGO_BUILD_TARGET",
                "CARGO_BUILD_RUSTFLAGS",
                "CFLAGS",
                "CXXFLAGS",
                "CPPFLAGS",
                "LDFLAGS",
                "CPATH",
                "C_INCLUDE_PATH",
                "CPLUS_INCLUDE_PATH",
                "LIBRARY_PATH",
                "MAKEFLAGS",
                "MFLAGS",
                "CROSS_COMPILE",
                "PKG_CONF_PATH",
            }:
                del env[key]

        env.update(LC_ALL="C", CC=self.cc, CXX=self.cxx, AR=self.ar, PKG_CONF_PATH="pkg-config")
        linker_key = f"CARGO_TARGET_{self.profile.rust_target.upper().replace("-", "_")}_LINKER"
        env[linker_key] = self.cc
        env["CARGO_ENCODED_RUSTFLAGS"] = ""

        if self.profile.cross and rust:
            # Our Rust programs use the self-contained musl target, not the
            # firmware's glibc SDK or the host's desktop libraries.
            env[linker_key] = "rust-lld"
            env["CARGO_ENCODED_RUSTFLAGS"] = "\x1f".join(
                (
                    "-Ctarget-cpu=cortex-a7", "-Clink-self-contained=yes",
                    "-Ctarget-feature=+crt-static", "-Clinker=rust-lld",
                )
            )
            env["PKG_CONFIG_LIBDIR"] = ""
            env["PKG_CONFIG_PATH"] = ""

        elif self.profile.cross:
            assert self.sysroot is not None  # validate() is called before invoking tools.
            flags = " ".join((f"--sysroot={self.sysroot}", *ARM_FLAGS))
            cppflags = f"--sysroot={self.sysroot} -DMESA_EGL_NO_X11_HEADERS"
            # RetroArch's configure probes consume CFLAGS, not CPPFLAGS alone.
            cflags = f"{flags} -DMESA_EGL_NO_X11_HEADERS"
            env.update(CFLAGS=cflags, CXXFLAGS=cflags, CPPFLAGS=cppflags, LDFLAGS=flags)
            env["PKG_CONFIG_SYSROOT_DIR"] = f"{self.sysroot}"
            env["PKG_CONFIG_LIBDIR"] = os.pathsep.join(
                f"{self.sysroot / path}" for path in paths.SYSROOT_PKGCONFIG_DIRS
            )
            env["PKG_CONFIG_PATH"] = ""
            env["PKG_CONFIG_ALLOW_CROSS"] = "1"
            env["CARGO_ENCODED_RUSTFLAGS"] = "\x1f".join(
                (
                    "-Ctarget-cpu=cortex-a7",
                    f"-Clink-arg=--sysroot={self.sysroot}",
                    *(f"-Clink-arg={flag}" for flag in ARM_FLAGS),
                )
            )

        return env

    def make_environment(self) -> dict[str, str]:
        """Use the same explicit inputs for Make execution and its cache key.

        Keep Cargo and SDK preparation on their existing environment policy.
        Unknown caller variables must not reach Make without being tracked.
        """
        allowed = {
            "PATH", "HOME", "TMPDIR", "SOURCE_DATE_EPOCH",
            "LC_ALL", "CC", "CXX", "AR", "CFLAGS", "CXXFLAGS", "CPPFLAGS", "LDFLAGS", "PKG_CONF_PATH",
            "PKG_CONFIG_SYSROOT_DIR", "PKG_CONFIG_LIBDIR", "PKG_CONFIG_PATH", "PKG_CONFIG_ALLOW_CROSS",
        }
        return {key: value for key, value in self.build_environment().items() if key in allowed}
