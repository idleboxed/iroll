"""Synthetic sources, firmware, card metadata and tool responses; no hardware or network.

SPDX-License-Identifier: BSD-3-Clause
"""

import os
import stat
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from iroll.card import linux as linux
from iroll.card.layout import BASIC_DATA_TYPE, CardPlan, CardTarget


@pytest.fixture
def table(target: CardTarget) -> dict[str, Any]:
    plan = CardPlan(target)
    return {"partitiontable": {
        "label": "gpt", "unit": "sectors", "sectorsize": 512,
        "firstlba": 2048, "lastlba": plan.last_sector,
        "partitions": [{"start": 2048, "size": plan.partition_sectors,
                        "type": BASIC_DATA_TYPE, "name": "idlebox"}],
    }}


@pytest.fixture
def guard_io(target: CardTarget, monkeypatch: pytest.MonkeyPatch) -> dict[int, int]:
    values = {
        linux.BLKGETSIZE64: target.size, linux.BLKGETDISKSEQ: 42,
        linux.BLKSSZGET: 512, linux.BLKROGET: 0,
    }
    info = SimpleNamespace(st_mode=stat.S_IFBLK, st_rdev=os.makedev(8, 240))
    monkeypatch.setattr(linux.os, "fstat", lambda fd: info)
    monkeypatch.setattr(linux.os, "lstat", lambda path: info)
    monkeypatch.setattr(linux, "read_ioctl_value", lambda fd, request, fmt: values[request])

    return values


@pytest.fixture
def volume(
    target: CardTarget, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> tuple[linux.CardVolume, Path, list[tuple[str, str]], list[list[str]]]:
    backend = linux.LinuxCardBackend()
    result = linux.CardVolume(backend, 100, 101, Path("/dev/loop9"), CardPlan(target))
    mountpoint = tmp_path / "mount"
    records, calls = [], []
    real_read = Path.read_text
    monkeypatch.setattr(result, "guard", lambda: None)
    monkeypatch.setattr(linux.os, "fstat", lambda fd: SimpleNamespace(st_rdev=os.makedev(7, 9)))

    def make_mount(**kwargs: object) -> str:
        mountpoint.mkdir()

        return f"{mountpoint}"

    def read(path: Path, *args: Any, **kwargs: Any) -> str:

        if f"{path}" == "/proc/self/mountinfo":
            return "\n".join(f"1 0 {number} / {where} rw - exfat /dev/loop9 rw" for number, where in records)

        return real_read(path, *args, **kwargs)

    def run_command(argv: list[str], **kwargs: object) -> str:
        calls.append(argv)

        if argv[0] == "mount":
            records.append(("7:9", f"{mountpoint}"))

        elif argv[0] == "umount":
            (mountpoint / "INSTRUCTIONS.md").unlink(missing_ok=True)

            if (mountpoint / "games").exists():
                assert (mountpoint / "games/states").is_dir()
                assert (mountpoint / "games/saves").is_dir()
                (mountpoint / "games/states").rmdir()
                (mountpoint / "games/saves").rmdir()
                (mountpoint / "games").rmdir()

            records.clear()

        return ""

    monkeypatch.setattr(linux.tempfile, "mkdtemp", make_mount)
    monkeypatch.setattr(Path, "read_text", read)
    monkeypatch.setattr(backend, "run_command", run_command)

    return result, mountpoint, records, calls
