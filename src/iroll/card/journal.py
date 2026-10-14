"""Durable, bounded phase journal on the PC, independent of logging.

SPDX-License-Identifier: BSD-3-Clause
"""

import json
import os
import stat
from pathlib import Path

from .layout import CardTarget, require
from .mounts import get_filesystem_types
from ..common.files import reject_symlinks, sync_directory


def _validate_storage(path: Path, target: CardTarget, mountinfo: str) -> None:
    metadata = path.stat()
    number = f"{os.major(metadata.st_dev)}:{os.minor(metadata.st_dev)}"
    require(number not in target.device_numbers, "Journal must be outside the card")
    types = get_filesystem_types(mountinfo, number)
    require(
        bool(types) and all(kind not in {"tmpfs", "ramfs"} for kind in types), "Journal must use durable PC storage",
    )


def validate_journal_root(path: Path, target: CardTarget, mountinfo: str) -> None:
    """Require a safe PC directory outside the card for operation reports."""
    require(path.is_absolute(), "Journal root must be absolute and on the PC")
    reject_symlinks(path)
    existing = path

    while not existing.exists():
        require(existing != existing.parent, "Journal root has no existing parent")
        existing = existing.parent

    require(existing.is_dir(), "Journal root parent must be a directory")
    _validate_storage(existing, target, mountinfo)


def validate_journal_parent(path: Path, target: CardTarget, mountinfo: str) -> None:
    """Require a new journal path in an existing safe PC directory."""
    require(path.is_absolute(), "Journal path must be absolute and on the PC")
    reject_symlinks(path)
    require(not path.exists(), "Journal already exists; use a new path")
    metadata = path.parent.stat()
    require(stat.S_ISDIR(metadata.st_mode), "Journal parent must be an existing directory")
    _validate_storage(path.parent, target, mountinfo)


class CardJournal:
    def __init__(self, path: Path, *, owner: tuple[int, int] | None = None) -> None:
        self.path = path
        self.owner = owner
        self.stream = None

    def __enter__(self) -> "CardJournal":
        reject_symlinks(self.path)
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        try:

            if self.owner is not None:
                os.fchown(fd, *self.owner)

            self.stream = os.fdopen(fd, "w", encoding="utf-8")

        except BaseException:
            os.close(fd)
            raise

        try:
            sync_directory(self.path.parent)

        except BaseException:
            self.stream.close()
            raise

        return self

    def record_event(self, phase: str, **fields: object) -> None:
        """Append and synchronize an operation event before returning."""
        assert self.stream is not None
        self.stream.write(json.dumps({"format": "IROLL-CARD-01", "phase": phase, **fields}) + "\n")
        self.stream.flush()
        os.fsync(self.stream.fileno())

    def __exit__(self, *exc: object) -> None:
        assert self.stream is not None
        self.stream.close()
