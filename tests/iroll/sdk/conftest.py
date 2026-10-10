"""Synthetic sources, firmware, card metadata and tool responses; no hardware or network.

SPDX-License-Identifier: BSD-3-Clause
"""

import hashlib
import os
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pytest

from iroll.common.errors import BuildError
from iroll.common.files import calculate_sha256
from iroll.components.registry import Source
from iroll.sdk.prepare import SdkOptions


@pytest.fixture
def preparation(
    tmp_path: Path, archive: Callable[..., tuple[Path, Source]], monkeypatch: pytest.MonkeyPatch,
) -> tuple[SdkOptions, dict[str, Any], Any]:
    monkeypatch.setattr("iroll.sdk.prepare.platform.system", lambda: "Linux")
    monkeypatch.setattr("iroll.sdk.prepare.platform.machine", lambda: "x86_64")
    monkeypatch.setattr("iroll.sdk.prepare.shutil.which", lambda name: f"/synthetic/{name}")
    triplet = "arm-buildroot-linux-gnueabihf"
    header = b"/* synthetic SDK header */\n"
    elf = bytearray(64)
    elf[:7] = b"\x7fELF\x01\x01\x01"
    elf[18:20] = (40).to_bytes(2, "little")
    library = bytes(elf)
    bootlin = {
        "root/relocate-sdk.sh": b"# synthetic script; never executed\n",
        "root/share/buildroot/sdk-location": b"/old\n",
        f"root/{triplet}/sysroot/usr/include/keep.h": b"base header\n",
    }

    for name in ("gcc", "g++", "ar"):
        bootlin[f"root/bin/{triplet}-{name}"] = b"synthetic compiler\n"

    sources = {}
    downloads = tmp_path / "in/downloads"
    archive_dir = downloads / "sdk/rk3032"
    archive_dir.mkdir(parents=True)

    for name, members in (("bootlin", bootlin), ("alsa", {"root/configure": b"synthetic"})):
        path, pin = archive(members)
        path.rename(archive_dir / path.name)
        sources[name] = asdict(pin)

    nor = tmp_path / "reference.bin"
    nor.write_bytes(b"synthetic NOR image")
    recipe = {
        "id": "synthetic-sdk",
        "triplet": triplet,
        "sources": sources,
        "nor": {"size": nor.stat().st_size, "offset": 8, "sha256": calculate_sha256(nor)},
        "libraries": [{
            "source": "lib/vendor.so", "destination": "usr/lib/vendor.so",
            "sha256": hashlib.sha256(library).hexdigest(),
        }],
        "links": {"usr/lib/vendor-alias.so": "vendor.so"},
        "header_copies": [],
        "headers": {"alsa/asoundlib.h": hashlib.sha256(header).hexdigest()},
        "pkgconfig": {"alsa": "Name: synthetic\n"},
    }
    options = SdkOptions(
        nor=nor, sdk=tmp_path / "in/sdk/rk3032", downloads=downloads, logs=tmp_path / "tmp/logs",
    )

    class FakeSdkRunner:
        def __init__(self) -> None:
            self.calls = []
            self.fail = None
            self.cancel = None
            self.corrupt_library = False
            self.new_glibc = False
            self.bad_arch = False
            self.corrupt_header = False
            self.corrupt_pkgconfig = False
            self.after_nor = None

        def run(
            self, argv: Sequence[str], *, cwd: Path, env: Mapping[str, str] | None = None,
            log: Path | None = None, pass_fds: Sequence[int] = (), **kwargs: Any,
        ) -> subprocess.CompletedProcess[str]:
            self.calls.append(list(argv))
            operation = "relocate" if argv[0] == "sh" and argv[1].endswith("relocate-sdk.sh") else argv[0]

            if operation == self.fail:
                raise BuildError("Synthetic SDK tool failure")

            if operation == self.cancel:
                raise KeyboardInterrupt

            if log:

                with log.open("a") as stream:
                    stream.write("synthetic tool output\n")

            output = ""

            if operation == "relocate":
                (cwd / "share/buildroot/sdk-location").write_text(f"{cwd}\n")

                for suffix in ("gcc", "g++", "ar"):
                    (cwd / "bin" / f"{triplet}-{suffix}").chmod(0o755)

            elif argv[0] == "unsquashfs":
                assert len(pass_fds) == 1
                assert os.fstat(pass_fds[0]).st_size == recipe["nor"]["size"]
                extracted = Path(argv[argv.index("-d") + 1])
                (extracted / "lib").mkdir(parents=True)
                (extracted / "lib/vendor.so").write_bytes(b"wrong" if self.corrupt_library else library)

                if self.after_nor:
                    self.after_nor()

            elif argv[0] == "make":
                sysroot = Path(next(arg.removeprefix("DESTDIR=") for arg in argv if arg.startswith("DESTDIR=")))
                dest = sysroot / "usr/include/alsa"
                dest.mkdir()
                (dest / "asoundlib.h").write_bytes(b"changed" if self.corrupt_header else header)

                if self.corrupt_pkgconfig:
                    (sysroot / "usr/lib/pkgconfig/alsa.pc").write_text("changed")

            elif argv[-1] == "-dumpmachine":
                output = f"{triplet}\n"

            elif argv[-1] == "-print-sysroot":
                output = f"{Path(argv[0]).parent.parent / triplet / "sysroot"}\n"

            elif argv[0] == "readelf":
                output = f"/lib/ld-linux-armhf.so.3 GLIBC_{"2.30" if self.new_glibc else "2.29"}"

            elif "-o" in argv:
                target = Path(argv[argv.index("-o") + 1])
                target.write_bytes(b"not ELF" if self.bad_arch else library)

            return subprocess.CompletedProcess(argv, 0, output, "")

    runner = FakeSdkRunner()

    return options, recipe, runner
