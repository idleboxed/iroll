"""Synthetic sources, firmware, card metadata and tool responses; no hardware or network.

SPDX-License-Identifier: BSD-3-Clause
"""

import hashlib
import io
import json
import os
import stat
import subprocess
import tarfile
import tomllib
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from iroll.card import linux as linux
from iroll.host import dependencies
from iroll.common import paths
from iroll.rollout.build import BuildOptions
from iroll.card.journal import CardJournal
from iroll.card.layout import CardPlan, CardTarget
from iroll.common.errors import BuildError
from iroll.common.files import calculate_sha256
from iroll.host.platforms import PROFILES, Toolchain
from iroll.components.registry import COMPONENTS, Source


@pytest.fixture
def target() -> CardTarget:
    return CardTarget("/dev/sdz", "8:240", 2 * 1024**3, 42, "Synthetic SD", "synthetic", ("8:240", "8:241"))


@pytest.fixture
def archive(tmp_path: Path) -> Callable[..., tuple[Path, Source]]:
    counter = 0

    def make(members: Mapping[str, bytes | tarfile.TarInfo] | None = None) -> tuple[Path, Source]:
        nonlocal counter
        counter += 1
        path = tmp_path / f"source-{counter}.tar.gz"

        with tarfile.open(path, "w:gz") as output:

            for name, content in (members or {"root/Makefile": b"synthetic"}).items():

                if isinstance(content, tarfile.TarInfo):
                    content.name = name
                    output.addfile(content)

                else:
                    info = tarfile.TarInfo(name)
                    info.size = len(content)
                    output.addfile(info, io.BytesIO(content))

        source = Source(path.name, calculate_sha256(path), "https://example.invalid/source.tar.gz", "synthetic")
        return path, source

    return make


@pytest.fixture
def runner(tmp_path: Path) -> Any:
    class FakeRunner:
        def __init__(self) -> None:
            self.calls = []
            self.rust_version = "rustc 1.91.1"
            self.libdir = tmp_path / "rust-std"
            self.libdir.mkdir()
            (self.libdir / "libstd-synthetic.rlib").write_bytes(b"synthetic")
            self.missing_libraries = set()
            self.fail_command = None
            self.cancel_command = None
            self.sdk_machine = "arm-buildroot-linux-gnueabihf"
            self.sdk_sysroot = ""
            self.headers = tmp_path / "toolchain/include"
            self.libraries = tmp_path / "toolchain/lib"
            self.programs = tmp_path / "toolchain/bin"

            for directory in (self.headers, self.libraries, self.programs):
                directory.mkdir(parents=True)

        def run(
            self, argv: Sequence[str], *, cwd: Path, env: Mapping[str, str] | None = None,
            log: Path | None = None, check: bool = True, input_data: str | None = None,
        ) -> subprocess.CompletedProcess[str]:
            self.calls.append((list(argv), cwd, dict(env or {})))

            if argv[0] == self.fail_command:
                raise BuildError("Synthetic compiler failure")

            if argv[0] == self.cancel_command:
                raise KeyboardInterrupt

            if log:
                log.write_text("synthetic compiler output\n")

            status, stdout = 0, ""
            # Header only: synthetic output, never executable machine code.
            arm = "armv7-unknown-linux-musleabihf" in argv or Path((env or {}).get("CC", "")).name.startswith("arm")
            elf = bytearray(64)
            elf[:7] = b"\x7fELF" + bytes((1 if arm else 2, 1, 1))
            elf[18:20] = (40 if arm else 62).to_bytes(2, "little")

            if argv[-1] == "-print-search-dirs":
                stdout = f"install: {self.programs}\nprograms: ={self.programs}\nlibraries: ={self.libraries}\n"

            elif "-E" in argv and "-v" in argv:
                stdout = f"#include <...> search starts here:\n {self.headers}\nEnd of search list.\n"

            elif argv[-1] == "-dumpmachine":
                stdout = f"{self.sdk_machine}\n"

            elif argv[-1] == "-print-sysroot":
                stdout = f"{self.sdk_sysroot}\n"

            elif argv[:2] == ["rustc", "--version"]:
                stdout = self.rust_version

            elif argv[:3] == ["rustc", "--print", "target-libdir"]:
                stdout = f"{self.libdir}"

            elif argv[0] == "pkg-config":
                status = int(argv[-1] in self.missing_libraries)

            elif argv[:2] == ["cargo", "build"]:
                manifest = Path(argv[argv.index("--manifest-path") + 1])
                name = tomllib.loads(manifest.read_text())["package"]["name"]
                target = Path(argv[argv.index("--target-dir") + 1]) / argv[argv.index("--target") + 1] / "release"
                target.mkdir(parents=True, exist_ok=True)
                (target / name).write_bytes(elf)

            elif argv[:2] == ["sh", "./configure"]:
                (cwd / "config.h").write_text("/* synthetic configure output */\n")
                (cwd / "config.mk").write_text("# synthetic configuration\n")
                (cwd / "config.log").write_text("synthetic configure log\n")

            elif argv[0] == "make":
                target = Path(next(arg.removeprefix("TARGET=") for arg in argv if arg.startswith("TARGET=")))

                if not (cwd / "synthetic.o").exists():
                    (cwd / "synthetic.o").write_bytes(b"synthetic object")

                target.write_bytes(elf)

            return subprocess.CompletedProcess(argv, status, stdout, "")

    return FakeRunner()


@pytest.fixture
def sdk(tmp_path: Path, runner: Any) -> Path:
    directory = tmp_path / "sdk"
    (directory / "bin").mkdir(parents=True)
    sysroot = directory / "arm-buildroot-linux-gnueabihf/sysroot"
    sysroot.mkdir(parents=True)
    runner.sdk_sysroot = f"{sysroot}"

    for suffix in ("gcc", "g++", "ar"):
        path = directory / "bin" / f"{runner.sdk_machine}-{suffix}"
        path.write_bytes(b"synthetic tool; never executed\n")
        path.chmod(0o755)

    return directory


@pytest.fixture
def dependency_tools(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dependencies.platform, "system", lambda: "Linux")
    monkeypatch.setattr(dependencies.platform, "machine", lambda: "x86_64")
    tools = tmp_path / "tools"
    tools.mkdir()

    for name in ("cc", "c++", "ar", "make", "sh", "pkg-config", "awk", "strings", "tr"):
        (tools / name).write_bytes(b"synthetic tool; never executed")

    monkeypatch.setattr(dependencies.shutil, "which", lambda tool, **kwargs: f"{tools / Path(tool).name}")
    release = tmp_path / "os-release"
    release.write_text("ID=ubuntu\n")
    monkeypatch.setattr(paths, "OS_RELEASE", release)


@pytest.fixture
def options(tmp_path: Path, archive: Callable[..., tuple[Path, Source]], dependency_tools: None) -> BuildOptions:
    root = tmp_path / "project"
    root.mkdir()
    components = []

    for component in COMPONENTS:

        if "linux-x86_64" not in component.platforms:
            continue

        if component.recipe == "cargo":
            source = root / component.directory
            source.mkdir()
            (source / "Cargo.toml").write_text(f"[package]\nname = \"{component.name}\"\n")
            (source / "Cargo.lock").write_text("version = 4\n")

            for resource in component.resources:
                directory = source / resource.directory
                directory.mkdir()
                # Synthetic examples, not a production list of required device profiles.
                names = (
                    ("2563-0555-hidraw.json", "20bc-5500-evdev.json", "2563-0555-evdev.json")
                    if resource.directory == "controls" else ("ru.json",)
                )

                for name in names:
                    (directory / name).write_text("{}\n")

            (source / "LICENSE").write_text("Synthetic license\n")

        else:
            directory = "root" if component.directory == "." else f"root/{component.directory}"
            _, pin = archive(
                {
                    f"{directory}/_artifact-name": component.artifact.encode(),
                    "root/LICENSE": b"Synthetic license\n",
                }
            )
            modules = []

            for module in component.submodules:
                _, module_pin = archive({"root/module.c": b"synthetic submodule source\n"})
                modules.append(replace(module, source=module_pin))

            component = replace(component, source=pin, submodules=tuple(modules))

        components.append(component)

    return BuildOptions(
        source_root=root,
        bios=tmp_path / "bios",
        dest=root / "sample",
        downloads=tmp_path / "downloads",
        build_dir=tmp_path / "build",
        logs=tmp_path / "logs",
        toolchain=Toolchain(PROFILES["linux-x86_64"]),
        components=tuple(components),
        offline=True,
        archives=tmp_path,
    )


@pytest.fixture
def arm_options(options: BuildOptions, sdk: Path, archive: Callable[..., tuple[Path, Source]]) -> BuildOptions:
    core = next(component for component in COMPONENTS if component.name == "core-mupen64plus-next")
    _, pin = archive({"root/_artifact-name": core.artifact.encode(), "root/LICENSE": b"Synthetic license\n"})

    return replace(
        options,
        toolchain=Toolchain(PROFILES["rk3032"], sdk, "/sdk/bin/arm-linux-gnueabihf-"),
        components=(*options.components, replace(core, source=pin)),
    )


@pytest.fixture
def backend(target: CardTarget, tmp_path: Path) -> Any:
    class FakeBackend:
        def __init__(self) -> None:
            self.calls = []
            self.failure = None
            self.target = target
            self.content = None
            device = tmp_path.stat().st_dev
            number = f"{os.major(device)}:{os.minor(device)}"
            self.mountinfo = f"1 0 {number} / / rw - ext4 synthetic rw\n"

        def read_mountinfo(self) -> str:
            return self.mountinfo

        def step(self, name: str) -> None:
            self.calls.append(name)

            if self.failure and self.failure[0] == name:
                raise self.failure[1]

        def inspect(self, device: str) -> CardTarget:
            self.step("inspect")
            assert device == target.device
            return self.target

        def validate_initialization(self, target: CardTarget, journal: Path) -> None:
            self.step("preflight")

        def list_mounted_paths(self, target: CardTarget) -> tuple[str, ...]:
            self.step("mounted_paths")
            return target.mountpoints

        def unmount(self, target: CardTarget) -> tuple[str, ...]:
            self.step("unmount")
            return target.mountpoints

        @contextmanager
        def open(self, target: CardTarget) -> Iterator[int]:
            self.step("open")
            try:
                yield 101
            finally:
                self.step("close")

        @contextmanager
        def open_volume(self, fd: int, plan: CardPlan, journal: CardJournal) -> Iterator[Any]:
            self.step("volume")
            try:
                yield self
            finally:
                self.step("release_volume")

        def partition(self, fd: int, plan: CardPlan) -> None:
            self.step("partition")

        def format(self) -> None:
            self.step("format")

        def copy_instructions(self, content: bytes, journal: CardJournal) -> str:
            self.step("copy")
            self.content = content
            return hashlib.sha256(content).hexdigest()

        def finish(self, fd: int, plan: CardPlan) -> None:
            self.step("finish")

    return FakeBackend()


@pytest.fixture
def disk_listing(target: CardTarget) -> dict[str, Any]:
    return {
        "path": target.device, "type": "disk", "size": target.size, "rm": True,
        "ro": False, "tran": "usb", "maj:min": "8:240", "model": target.model,
        "serial": target.serial, "mountpoints": [None],
        "children": [{"path": "/dev/sdz1", "type": "part", "ro": False,
                      "maj:min": "8:241", "mountpoints": [None]}],
    }


@pytest.fixture
def inspection(
    target: CardTarget, disk_listing: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
) -> tuple[linux.LinuxCardBackend, dict[str, str], list[Path]]:
    backend = linux.LinuxCardBackend()
    texts = {
        "/sys/class/block/sdz/diskseq": "42\n",
        "/sys/class/block/sdz/queue/logical_block_size": "512\n",
        "/sys/class/block/sdz/dev": "8:240\n",
        "/sys/class/block/sdz1/dev": "8:241\n",
        "/proc/self/mountinfo": "1 0 8:1 / / rw - ext4 /dev/sda1 rw\n",
        "/proc/swaps": "Filename Type Size Used Priority\n",
    }
    original_read, original_iter, original_resolve, original_lstat = (
        Path.read_text, Path.iterdir, Path.resolve, linux.os.lstat,
    )
    holders = []

    def read(path: Path, *args: Any, **kwargs: Any) -> str:
        name = f"{path}"

        return texts[name] if name in texts else original_read(path, *args, **kwargs)

    def iterdir(path: Path) -> Iterator[Path]:

        if f"{path}".startswith("/sys/class/block/"):
            return iter(holders)

        return original_iter(path)

    def resolve(path: Path, *args: Any, **kwargs: Any) -> Path:

        if f"{path}" == "/sys/class/block/sdz":
            return Path("/synthetic/sdz")

        if f"{path}" == "/sys/class/block/sdz1":
            return Path("/synthetic/sdz/sdz1")

        return original_resolve(path, *args, **kwargs)

    def lstat(path: str | Path, *args: Any, **kwargs: Any) -> os.stat_result | SimpleNamespace:

        if f"{path}" == "/dev/sdz":
            return SimpleNamespace(st_mode=stat.S_IFBLK, st_rdev=os.makedev(8, 240))

        return original_lstat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read)
    monkeypatch.setattr(Path, "iterdir", iterdir)
    monkeypatch.setattr(Path, "resolve", resolve)
    monkeypatch.setattr(linux.os, "lstat", lstat)
    monkeypatch.setattr(backend, "run_command", lambda *args, **kwargs: json.dumps({"blockdevices": [disk_listing]}))

    return backend, texts, holders


@pytest.fixture
def toolchains(options: BuildOptions, sdk: Path) -> dict[str, Toolchain]:
    return {
        "linux-x86_64": options.toolchain,
        "rk3032": Toolchain(PROFILES["rk3032"], sdk, "/sdk/bin/arm-linux-gnueabihf-"),
    }
