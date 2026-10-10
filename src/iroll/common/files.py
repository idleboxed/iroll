"""Bounded file access, path guards and cooperative locks.

SPDX-License-Identifier: BSD-3-Clause
"""

import fcntl
import hashlib
import json
import os
import stat
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from time import time
from typing import Any

from .errors import BuildError

MAX_ARTIFACT = 256 * 1024 * 1024


def create_report(root: Path, *, owner: tuple[int, int] | None = None) -> Path:
    reject_symlinks(root)
    missing = []
    directory = root

    while not directory.exists():
        missing.append(directory)
        directory = directory.parent

    for directory in reversed(missing):
        directory.mkdir()

        if owner is not None:
            os.chown(directory, *owner)

    report = create_report_directory(root)

    if owner is not None:
        os.chown(report, *owner)

    return report


def parse_relative_path(value: str) -> Path:

    if not value or "\\" in value or any(ord(char) < 32 for char in value):
        raise BuildError(f"Invalid relative path: {value!r}")

    parts = value.split("/")

    if any(part in ("", ".", "..") for part in parts) or PurePosixPath(value).is_absolute():
        raise BuildError(f"Unsafe relative path: {value!r}")

    return Path(*parts)


def reject_symlinks(path: Path) -> None:

    for item in (path, *path.parents):

        if item.is_symlink():
            raise BuildError(f"Symlink path is not allowed: {item}")


def validate_regular_file(path: Path, *, limit: int = MAX_ARTIFACT) -> None:
    reject_symlinks(path)
    metadata = path.stat()

    if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > limit:
        raise BuildError(f"Not a regular file or exceeds size limit: {path}")


def calculate_sha256(path: Path) -> str:
    validate_regular_file(path)

    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def stage_resources(payload: Path, resources: Mapping[str, bytes], *, resource_kind: str) -> dict[str, str]:
    """Validate every resource destination before writing any payload file."""

    for name in resources:
        target = payload / parse_relative_path(name)
        reject_symlinks(target)
        validate_directory(target.parent)

        if target.exists():
            raise BuildError(f"Duplicate {resource_kind} payload file: {name}")

    for name, data in resources.items():
        target = payload / parse_relative_path(name)
        target.parent.mkdir(parents=True, exist_ok=True)

        with target.open("xb") as stream:
            stream.write(data)

        target.chmod(0o644)

    return {name: hashlib.sha256(data).hexdigest() for name, data in resources.items()}


def sync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def read_cached_json(path: Path) -> Any:
    """Missing or incomplete cache state is a miss; unsafe paths are still errors."""
    reject_symlinks(path)

    if not path.exists():
        return None

    validate_regular_file(path, limit=64 * 1024)
    try:
        return json.loads(path.read_text(encoding="utf-8"))

    except (ValueError, UnicodeError, RecursionError):
        return None


def write_json(path: Path, value: Any, *, owner: tuple[int, int] | None = None) -> None:
    # Callers own the staging/report directory or have invalidated the previous cache state.

    with path.open("x", encoding="utf-8") as stream:

        if owner is not None:
            os.fchown(stream.fileno(), *owner)

        json.dump(value, stream, indent=2, ensure_ascii=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())

    sync_directory(path.parent)


@contextmanager
def acquire_directory_lock(path: Path) -> Iterator[None]:
    """Lock a stable existing directory inode, without creating a lock file."""
    reject_symlinks(path)
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:

        if not stat.S_ISDIR(os.fstat(fd).st_mode):
            raise BuildError(f"Lock target is not a directory: {path}")

        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

        except BlockingIOError as exc:
            raise BuildError(f"Another iroll operation holds the lock: {path}") from exc

        yield
    finally:
        os.close(fd)
    # A locked destination or build-data root is never replaced while its lock is held.


def read_elf_architecture(path: Path) -> str:
    """Identify supported ELF architecture without executing the binary."""
    validate_regular_file(path)

    with path.open("rb") as stream:
        header = stream.read(20)

    if len(header) != 20 or header[:4] != b"\x7fELF" or header[5:7] != b"\x01\x01":
        raise BuildError(f"Not a supported little-endian ELF: {path}")

    architecture = {(2, 62): "x86_64", (1, 40): "arm"}.get((header[4], int.from_bytes(header[18:20], "little")))

    if architecture is None:
        raise BuildError(f"Unsupported ELF architecture: {path}")

    return architecture


def fingerprint_paths(roots: dict[Path, bool]) -> str:
    """Hash toolchain contents; bool selects recursive directories.

    SDK/header symlinks are followed, once per directory, unlike the stricter
    no-symlink rules for build outputs. No metadata or content copies are saved.
    """
    digest = hashlib.sha256()
    pending = [(path.absolute(), recursive) for path, recursive in sorted(roots.items())]
    seen = set()

    while pending:
        path, recursive = pending.pop()
        resolved = path.resolve()
        key = (resolved, recursive)

        if key in seen:
            continue

        seen.add(key)
        try:
            metadata = path.stat()

        except FileNotFoundError:
            record = [f"{path}", f"{resolved}", None]

        else:
            record = [f"{path}", f"{resolved}", metadata.st_mode]

            if stat.S_ISDIR(metadata.st_mode):
                children = sorted(path.iterdir())
                record.append([child.name for child in children])
                pending.extend((child, recursive) for child in children if recursive or not child.is_dir())

            elif stat.S_ISREG(metadata.st_mode):

                with path.open("rb") as stream:
                    record.append(hashlib.file_digest(stream, "sha256").hexdigest())

        digest.update(json.dumps(record, ensure_ascii=True).encode())

    return digest.hexdigest()


def _format_filename_stamp(timestamp: int) -> str:
    return datetime.fromtimestamp(timestamp, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def create_report_directory(parent: Path) -> Path:
    stamp = _format_filename_stamp(int(time()))
    collision = 0

    while True:
        suffix = f"_{collision:02d}" if collision else ""
        path = parent / f"{stamp}{suffix}"
        try:
            path.mkdir(mode=0o700)
            return path

        except FileExistsError:
            collision += 1


def validate_directory(path: Path) -> None:
    reject_symlinks(path)

    for item in (path, *path.parents):

        if item.exists() and not item.is_dir():
            raise BuildError(f"File/directory conflict: {item}")


def ensure_directory(path: Path) -> None:
    validate_directory(path)

    if not path.exists():
        ensure_directory(path.parent)
        path.mkdir()
        sync_directory(path.parent)
