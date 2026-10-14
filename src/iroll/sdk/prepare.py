"""Reproducible RK3032 SDK preparation from pinned archives and a local NOR file.

No hardware access, vendor execution, dependency patches or implicit replacement.
SPDX-License-Identifier: BSD-3-Clause
"""

import hashlib
import json
import logging
import os
import platform
import re
import shlex
import shutil
import stat
import tempfile
from contextlib import ExitStack
from dataclasses import dataclass
from importlib.resources import files
from itertools import combinations
from pathlib import Path
from typing import Any

from ..common import paths
from ..common.errors import BuildError
from ..common.files import (
    acquire_directory_lock,
    calculate_sha256,
    create_report_directory,
    parse_relative_path,
    read_cached_json,
    read_elf_architecture,
    reject_symlinks,
    sync_directory,
    validate_regular_file,
    write_json,
)
from ..host.platforms import PROFILES, Toolchain
from .discovery import discover_sdk
from ..common.process import Runner
from ..components.registry import Source
from .archive import extract_sdk_archive, fingerprint_sdk, validate_sdk_links
from ..components.sources import SourceDownloads

logger = logging.getLogger(__name__)

PROBE_C = """#include <EGL/egl.h>
#include <GLES2/gl2.h>
#include <gbm.h>
#include <xf86drm.h>
#include <alsa/asoundlib.h>
#include <libudev.h>
#include <zlib.h>
int main(void) {
    struct gbm_device *device = gbm_create_device(-1);
    snd_pcm_t *pcm = 0;
    eglGetDisplay(EGL_DEFAULT_DISPLAY);
    glClear(0);
    drmGetVersion(-1);
    snd_pcm_open(&pcm, "default", SND_PCM_STREAM_PLAYBACK, 0);
    udev_new();
    zlibVersion();
    gbm_device_destroy(device);
    return 0;
}
"""
PROBE_CXX = """#include <string>
int main(int argc, char **argv) {
    std::string value(argc ? argv[0] : "sdk");
    return value.size();
}
"""


@dataclass(frozen=True)
class SdkOptions:
    nor: Path
    sdk: Path
    downloads: Path
    logs: Path


def load_recipe() -> dict[str, Any]:
    """Read the packaged, pinned RK3032 SDK recipe."""
    return json.loads(files("iroll.sdk").joinpath("sdk-rk3032.json").read_text(encoding="utf-8"))


class SdkPreparer:
    def __init__(
        self, options: SdkOptions, *, runner: Runner | None = None,
        sources: SourceDownloads | None = None, recipe: dict[str, Any] | None = None,
    ) -> None:
        self.options = options
        self.runner = runner if runner is not None else Runner()
        # Package resources and the filesystem are read only by run(), not construction.
        self.recipe = recipe
        self.sources = sources

    def validate_inputs(self) -> None:
        """Require supported host architecture and safe absolute input and output paths."""
        options = self.options

        if platform.system() != "Linux" or platform.machine() not in ("x86_64", "AMD64"):
            raise BuildError("SDK preparation requires Linux x86_64")

        for path in (options.nor, options.sdk, options.downloads, options.logs):

            if not path.is_absolute():
                raise BuildError("SDK preparation requires absolute paths")

            reject_symlinks(path)

        for path in (options.sdk, options.downloads, options.logs):

            if path.exists() and not path.is_dir():
                raise BuildError(f"SDK path is not a directory: {path}")

        roots = [path.resolve() for path in (options.sdk, options.downloads, options.logs)]

        if any(
            left == right or left in right.parents or right in left.parents for left, right in combinations(roots, 2)
        ):
            raise BuildError("SDK, downloads and logs paths overlap unsafely")

        nor = options.nor.resolve()

        if any(nor == root or root in nor.parents for root in (roots[0], roots[2])):
            raise BuildError("NOR input overlaps SDK or logs output")

        if not re.fullmatch(r"[A-Za-z0-9_./+-]+", f"{options.sdk}") or options.sdk == Path("/"):
            raise BuildError("SDK path must not contain spaces or shell metacharacters")

        reject_symlinks(options.downloads / "sdk/rk3032")
        reject_symlinks(self.state_path)
        pending = paths.get_sdk_pending_path(options.sdk)

        if pending.exists() or pending.is_symlink():
            raise BuildError(f"SDK preparation is incomplete; inspect before removing: {pending}")

    @property
    def state_path(self) -> Path:
        """Return the SDK provenance file path beside the SDK directory."""
        return self.options.sdk.with_name(f".{self.options.sdk.name}.iroll-sdk.json")

    def run(self) -> dict[str, Any]:
        """Prepare and validate the SDK from pinned sources and a local NOR file."""
        self.validate_inputs()
        # Validate links before resolving: normalization must not hide a forbidden alias.
        self.options = SdkOptions(
            nor=self.options.nor.resolve(), sdk=self.options.sdk.resolve(),
            downloads=self.options.downloads.resolve(), logs=self.options.logs.resolve(),
        )
        recipe = self.recipe if self.recipe is not None else load_recipe()
        recipe_sha = hashlib.sha256(json.dumps(recipe, sort_keys=True).encode()).hexdigest()
        options = self.options
        expected = {"recipe_sha256": recipe_sha, "nor_sha256": recipe["nor"]["sha256"]}
        metadata = options.nor.lstat()

        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size != recipe["nor"]["size"]:
            raise BuildError("NOR input must be a regular file of the pinned size")

        with ExitStack() as stack:
            # Refuse devices before opening; recheck the opened inode, then retain it through unsquashfs.
            fd = os.open(options.nor, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            nor = stack.enter_context(os.fdopen(fd, "rb"))
            metadata = os.fstat(fd)

            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size != recipe["nor"]["size"]:
                raise BuildError("NOR input must be a regular file of the pinned size")

            if hashlib.file_digest(nor, "sha256").hexdigest() != recipe["nor"]["sha256"]:
                raise BuildError("NOR SHA-256 mismatch; only the preserved physical reference is accepted")

            options.sdk.parent.mkdir(parents=True, exist_ok=True)
            stack.enter_context(acquire_directory_lock(options.sdk.parent))
            self.validate_inputs()
            options.logs.mkdir(parents=True, exist_ok=True)
            stack.enter_context(acquire_directory_lock(options.logs))
            report = create_report_directory(options.logs)
            logger.info("SDK preparation reports: %s", report)

            if options.sdk.exists():
                state = read_cached_json(self.state_path)

                if not isinstance(state, dict) or any(state.get(key) != value for key, value in expected.items()):
                    raise BuildError(
                        "Existing SDK has no matching preparation record; not modified. Use another --sdk."
                    )

                if state.get("tree_sha256") != fingerprint_sdk(options.sdk):
                    raise BuildError(
                        "Prepared SDK changed; not overwritten. Use another --sdk or inspect it manually."
                    )

                logger.info("Reusing prepared SDK: %s", options.sdk)
                result = {
                    "status": "SDK_REUSED", "sdk": f"{options.sdk}",
                    "reports": f"{report}", "hardware_verified": False,
                }
                write_json(report / paths.REPORT_FILE, result)
                return result

            if self.state_path.exists():
                raise BuildError(f"SDK record exists without its SDK; inspect it first: {self.state_path}")

            needed = ("sh", "make", "pkg-config", "unsquashfs", "readelf", "file", "grep", "sed", "readlink")
            missing = [name for name in needed if shutil.which(name) is None]

            if missing:
                raise BuildError(f"Missing SDK preparation tools: {", ".join(missing)}")

            options.downloads.mkdir(parents=True, exist_ok=True)
            stack.enter_context(acquire_directory_lock(options.downloads))
            pending = paths.get_sdk_pending_path(options.sdk)
            published = False
            identity = None
            try:

                with tempfile.TemporaryDirectory(prefix=".iroll-sdk-", dir=options.sdk.parent) as temporary:
                    work = Path(temporary)
                    sdk = work / "sdk"
                    downloads = self.sources if self.sources is not None else SourceDownloads(
                        options.downloads / "sdk/rk3032"
                    )
                    extracted = {}

                    for name, data in recipe["sources"].items():
                        source = Source(**data)
                        archive = downloads.obtain(source)
                        destination = sdk if name == "bootlin" else work / name
                        logger.info("Preparing SDK input %s", name)
                        extract_sdk_archive(archive, destination, source)
                        extracted[name] = destination

                    self.relocate(sdk, recipe, report)
                    self.install_nor(sdk, work / "nor", nor.fileno(), recipe, report)
                    self.install_headers(sdk, extracted, recipe, report)
                    self.check_payload(sdk, recipe)
                    self.probe(sdk, work, recipe, report, discover=True)

                    if options.sdk.exists() or self.state_path.exists():
                        raise BuildError("SDK destination changed during preparation")

                    write_json(pending, expected)
                    sdk.rename(options.sdk)
                    published = True
                    identity = options.sdk.stat().st_ino
                    # Relocation embeds absolute paths, so it must run again at the final location.
                    self.relocate(options.sdk, recipe, report)
                    self.check_payload(options.sdk, recipe)
                    self.probe(options.sdk, work, recipe, report, discover=False)
                    state = {**expected, "tree_sha256": fingerprint_sdk(options.sdk)}
                    write_json(self.state_path, state)
                    pending.unlink()
                    sync_directory(options.sdk.parent)

                result = {
                    "status": "SDK_PREPARED", "sdk": f"{options.sdk}", "reports": f"{report}",
                    "recipe": recipe["id"], "nor_sha256": expected["nor_sha256"],
                    "hardware_verified": False, "target_programs_executed": False,
                }
                write_json(report / paths.REPORT_FILE, result)
                logger.info("SDK prepared: %s", options.sdk)
                return result

            except BaseException as exc:
                # Existing SDKs are never replaced. Remove only our newly published tree.

                if published and options.sdk.is_dir() and not options.sdk.is_symlink():

                    if options.sdk.stat().st_ino == identity:
                        shutil.rmtree(options.sdk)
                        self.state_path.unlink(missing_ok=True)

                if not options.sdk.exists():
                    pending.unlink(missing_ok=True)

                try:
                    write_json(report / paths.FAILURE_FILE, {"error": f"{exc}" or type(exc).__name__})

                except OSError:
                    logger.error("Could not save SDK failure report in %s", report)

                raise

    def create_toolchain(self, sdk: Path, recipe: dict[str, Any]) -> Toolchain:
        """Describe the cross toolchain and sysroot at the supplied SDK path."""
        triplet = recipe["triplet"]
        prefix = sdk / "bin" / f"{triplet}-"
        return Toolchain(PROFILES["rk3032"], sdk / triplet / "sysroot", f"{prefix}")

    def build_environment(self, sdk: Path, recipe: dict[str, Any]) -> dict[str, str]:
        """Prepare a controlled SDK environment without caller-injected shell or compiler settings."""
        env = self.create_toolchain(sdk, recipe).build_environment()

        for name in (
            "CONFIG_SHELL", "BASH_ENV", "ENV", "CDPATH", "LD_PRELOAD", "LD_LIBRARY_PATH", "DESTDIR",
            "MAKEFILES", "GCC_EXEC_PREFIX", "COMPILER_PATH", "LD_RUN_PATH",
        ):
            env.pop(name, None)

        env["CONFIG_SITE"] = "/dev/null"
        return env

    def relocate(self, sdk: Path, recipe: dict[str, Any], report: Path) -> None:
        """Run the SDK's relocation script and verify its recorded location."""
        script = sdk / "relocate-sdk.sh"
        validate_regular_file(script)
        self.runner.run(
            ["sh", f"{script}"], cwd=sdk, env=self.build_environment(sdk, recipe), log=report / "relocate.log",
        )
        location = sdk / "share/buildroot/sdk-location"
        validate_regular_file(location, limit=4096)

        if location.read_text().strip() != f"{sdk}":
            raise BuildError("SDK relocation did not record its actual location")

    def install_nor(self, sdk: Path, extracted: Path, fd: int, recipe: dict[str, Any], report: Path) -> None:
        """Extract the specified libraries from the held NOR file into the SDK sysroot."""
        self.runner.run(
            ["unsquashfs", "-no-progress", "-no-xattrs", "-no-wildcards", "-processors", "1",
             "-d", f"{extracted}", "-o", f"{recipe["nor"]["offset"]}", f"/proc/self/fd/{fd}",
             *(item["source"] for item in recipe["libraries"])],
            cwd=sdk, env=self.build_environment(sdk, recipe), log=report / "nor.log", pass_fds=(fd,),
        )
        sysroot = self.create_toolchain(sdk, recipe).sysroot

        for item in recipe["libraries"]:
            source = extracted / parse_relative_path(item["source"])

            if calculate_sha256(source) != item["sha256"] or read_elf_architecture(source) != "arm":
                raise BuildError(f"Extracted NOR library mismatch: {source}")

            destination = sysroot / parse_relative_path(item["destination"])
            reject_symlinks(destination)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
            destination.chmod(0o755)

        for name, target in recipe["links"].items():
            destination = sysroot / parse_relative_path(name)
            reject_symlinks(destination.parent)

            if destination.exists() and not destination.is_symlink():
                raise BuildError(f"SDK linker alias conflicts with a regular file: {destination}")

            destination.unlink(missing_ok=True)
            destination.symlink_to(target)

        directory = sysroot / "usr/lib/pkgconfig"
        directory.mkdir(parents=True, exist_ok=True)

        for name, content in recipe["pkgconfig"].items():
            target = directory / parse_relative_path(f"{name}.pc")
            reject_symlinks(target)
            target.write_text(content, encoding="utf-8")

    def install_headers(
        self, sdk: Path, extracted: dict[str, Path], recipe: dict[str, Any], report: Path,
    ) -> None:
        """Install the recipe's checked header trees into the SDK sysroot."""
        sysroot = self.create_toolchain(sdk, recipe).sysroot
        include = sysroot / "usr/include"

        for component, source_name, destination_name in recipe["header_copies"]:
            source = extracted[component] / parse_relative_path(source_name)
            destination = include / parse_relative_path(destination_name)
            reject_symlinks(source)
            reject_symlinks(destination)

            if source.is_dir():
                shutil.copytree(source, destination)

            else:
                validate_regular_file(source)
                shutil.copyfile(source, destination)

        alsa = extracted["alsa"]
        env = self.build_environment(sdk, recipe)
        # Explicit --build makes Autoconf cross mode unconditional, even with binfmt/QEMU installed.
        self.runner.run(
            ["sh", "./configure", "--build=x86_64-pc-linux-gnu", f"--host={recipe["triplet"]}",
             "--prefix=/usr", "--disable-python"],
            cwd=alsa, env=env, log=report / "alsa-headers.log",
        )
        self.runner.run(
            ["make", "-C", "include", "install", f"DESTDIR={sysroot}"],
            cwd=alsa, env=env, log=report / "alsa-headers.log",
        )

    def check_payload(self, sdk: Path, recipe: dict[str, Any]) -> None:
        """Validate installed headers, libraries and links against the pinned recipe."""
        validate_sdk_links(sdk)
        sysroot = self.create_toolchain(sdk, recipe).sysroot

        for name, expected in recipe["headers"].items():

            if calculate_sha256(sysroot / "usr/include" / parse_relative_path(name)) != expected:
                raise BuildError(f"SDK header mismatch: {name}")

        for item in recipe["libraries"]:

            if calculate_sha256(sysroot / parse_relative_path(item["destination"])) != item["sha256"]:
                raise BuildError(f"SDK NOR library changed: {item["destination"]}")

        for name, content in recipe["pkgconfig"].items():
            path = sysroot / "usr/lib/pkgconfig" / parse_relative_path(f"{name}.pc")
            validate_regular_file(path, limit=64 * 1024)

            if path.read_text(encoding="utf-8") != content:
                raise BuildError(f"SDK pkg-config mismatch: {name}")

        for name, target in recipe["links"].items():
            path = sysroot / parse_relative_path(name)

            if not path.is_symlink() or os.readlink(path) != target:
                raise BuildError(f"SDK linker alias mismatch: {name}")

    def probe(self, sdk: Path, work: Path, recipe: dict[str, Any], report: Path, *, discover: bool) -> None:
        """Compile and inspect a toolchain probe, optionally checking SDK discovery."""
        toolchain = self.create_toolchain(sdk, recipe)

        if discover:
            detected = discover_sdk(PROFILES["rk3032"], sdk, self.runner, cwd=work)

            if detected != toolchain:
                raise BuildError("Prepared SDK toolchain differs from the recipe")

        else:
            actual = self.runner.run([toolchain.cc, "-print-sysroot"], cwd=work).stdout.strip()

            if actual != f"{toolchain.sysroot}":
                raise BuildError("Final SDK compiler reports an incorrect sysroot")

        env = self.build_environment(sdk, recipe)
        packages = list(recipe["pkgconfig"])
        flags = self.runner.run(["pkg-config", "--cflags", "--libs", *packages], cwd=work, env=env).stdout
        # pkg-config emits shell-quoted paths; SDK path guards already exclude metacharacters.

        for suffix, code, compiler in (("c", PROBE_C, toolchain.cc), ("cc", PROBE_CXX, toolchain.cxx)):
            source = work / f"probe.{suffix}"
            binary = work / f"probe-{suffix}"
            source.write_text(code, encoding="utf-8")
            self.runner.run(
                [compiler, f"--sysroot={toolchain.sysroot}", "-mfloat-abi=hard",
                 "-DMESA_EGL_NO_X11_HEADERS", f"{source}", "-o", f"{binary}", *shlex.split(flags)],
                cwd=work, env=env, log=report / "probe.log",
            )

            if read_elf_architecture(binary) != "arm":
                raise BuildError("SDK probe did not produce an ARM ELF")

            output = self.runner.run(["readelf", "-lW", "-VW", f"{binary}"], cwd=work).stdout
            versions = re.findall(r"GLIBC_(\d+)\.(\d+)(?:\.(\d+))?", output)

            if (
                "/lib/ld-linux-armhf.so.3" not in output
                or any(tuple(int(number or 0) for number in version) > (2, 29, 0) for version in versions)
            ):
                raise BuildError("SDK probe requires an incompatible loader or glibc newer than 2.29")
