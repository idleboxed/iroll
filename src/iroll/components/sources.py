"""SHA-pinned downloads, confined extraction and verified reusable source trees.

SPDX-License-Identifier: BSD-3-Clause
"""

import hashlib
import logging
import os
import shutil
import tarfile
import tempfile
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path, PurePosixPath

from ..common.errors import BuildError
from ..common.files import (
    parse_relative_path,
    read_cached_json,
    reject_symlinks,
    sync_directory,
    validate_regular_file,
    write_json,
)
from .registry import Source

logger = logging.getLogger(__name__)
MAX_ARCHIVE = 256 * 1024 * 1024
MAX_EXTRACTED = 2 * 1024 * 1024 * 1024
MAX_MEMBERS = 100_000


def verify(path: Path, source: Source) -> None:
    """Check a bounded source archive against its pinned SHA-256."""
    validate_regular_file(path, limit=MAX_ARCHIVE)

    with path.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()

    if digest != source.sha256:
        raise BuildError(f"Source SHA-256 mismatch: {path}; not overwritten or retried")


def download(url: str, target: Path) -> None:
    """Download an HTTPS source into a new file without replacing an existing archive."""

    if not url.startswith("https://"):
        raise BuildError("Source downloads require HTTPS")

    request = urllib.request.Request(url, headers={"User-Agent": "iroll/0.1"})
    try:

        with urllib.request.urlopen(request, timeout=30) as response, target.open("xb") as output:

            if not response.url.startswith("https://"):
                raise BuildError("Source download redirected outside HTTPS")

            total = 0

            while chunk := response.read(1024 * 1024):
                total += len(chunk)

                if total > MAX_ARCHIVE:
                    raise BuildError("Source archive exceeds size limit")

                output.write(chunk)

            output.flush()
            os.fsync(output.fileno())

    except urllib.error.URLError as exc:
        raise BuildError(f"Source download failed: {exc.reason}") from exc


class SourceDownloads:
    def __init__(
        self,
        directory: Path,
        *,
        offline: bool = False,
        archives: Path | None = None,
        fetch: Callable[[str, Path], None] = download,
    ) -> None:
        self.directory = directory
        self.offline = offline
        self.archives = archives
        self.fetch = fetch

    def obtain(self, source: Source) -> Path:
        """Reuse a verified local archive or download and verify the pinned source."""
        archive_name = parse_relative_path(source.archive)

        if len(archive_name.parts) != 1:
            raise BuildError("Source archive name must be a single filename")

        for directory in (self.archives, self.directory):

            if directory is not None:
                candidate = directory / source.archive

                if candidate.exists() or candidate.is_symlink():
                    verify(candidate, source)
                    return candidate

        if self.offline:
            raise BuildError(f"Offline source archive missing: {source.archive}")

        reject_symlinks(self.directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        logger.info("Downloading %s", source.archive)

        with tempfile.TemporaryDirectory(prefix="download-", dir=self.directory) as temporary:
            path = Path(temporary) / source.archive
            self.fetch(source.url, path)
            verify(path, source)
            target = self.directory / source.archive
            # The build service holds the downloads lock throughout the operation.
            os.replace(path, target)
            sync_directory(self.directory)

        return target


def _validate_archive_members(archive: tarfile.TarFile) -> tuple[str, list[tarfile.TarInfo]]:
    members = archive.getmembers()

    if len(members) > MAX_MEMBERS or sum(member.size for member in members) > MAX_EXTRACTED:
        raise BuildError("Source archive exceeds extraction limits")

    top = set()
    seen = set()

    for member in members:
        parts = PurePosixPath(member.name).parts

        if (
            not parts
            or member.name.startswith("/")
            or ".." in parts
            or "\\" in member.name
            or not (member.isdir() or member.isfile())
        ):
            raise BuildError(f"Unsafe archive member: {member.name!r}")

        normalized = f"{PurePosixPath(member.name)}"

        if normalized in seen:
            raise BuildError(f"Duplicate archive member: {member.name!r}")

        seen.add(normalized)
        top.add(parts[0])

    if len(top) != 1:
        raise BuildError("Source archive must have exactly one top-level directory")

    return top.pop(), members


def extract(archive_path: Path, destination: Path, source: Source) -> None:
    """Verify an archive and extract it into a new checked directory, enforcing safe member paths."""
    verify(archive_path, source)
    reject_symlinks(destination)

    if destination.exists():
        raise BuildError(f"Source extraction directory already exists: {destination}")

    try:

        with tarfile.open(archive_path) as archive:
            top, _ = _validate_archive_members(archive)
            destination.mkdir(parents=True)

            with tempfile.TemporaryDirectory(prefix="extract-", dir=destination) as temporary:
                archive.extractall(temporary, filter="data")
                root = Path(temporary) / top

                if not root.is_dir():
                    raise BuildError("Source archive root is not a directory")

                for item in root.iterdir():
                    shutil.move(f"{item}", destination / item.name)

    except (tarfile.TarError, EOFError) as exc:
        raise BuildError(f"Cannot extract source archive: {archive_path}: {exc}") from exc


def prepare_source(
    archive_path: Path, destination: Path, source: Source,
    *, modules: tuple[tuple[str, Path, Source], ...] = (),
) -> None:
    """Reuse only a read-only tree matching every file in the pinned archive.

    One tree per component, with the archive SHA in a sibling JSON file.
    Read-only files prevent compiler writes through links in the build tree.
    """
    inputs = [(Path("."), archive_path, source)]
    module_paths = []

    for directory, path, pin in modules:
        relative = parse_relative_path(directory)

        if relative == Path(".") or any(
            relative == other or relative in other.parents or other in relative.parents for other in module_paths
        ):
            raise BuildError(f"Overlapping source submodule: {directory}")

        module_paths.append(relative)
        inputs.append((relative, path, pin))

    # Missing or corrupt dependencies fail before replacing any previously verified tree.

    for _, path, pin in inputs:
        verify(path, pin)

    reject_symlinks(destination)
    marker = destination.with_name(f"{destination.name}.json")
    expected_state = {"source_sha256": source.sha256}

    if modules:
        expected_state["submodules"] = {directory: pin.sha256 for directory, _, pin in modules}

    previous = read_cached_json(marker)

    if destination.exists() and not destination.is_dir():
        raise BuildError(f"Source tree is not a directory: {destination}")

    if not destination.exists() or previous != expected_state:
        # No archive-version directories: discard an old or incomplete extraction.
        marker.unlink(missing_ok=True)

        if destination.exists():
            shutil.rmtree(destination)

        destination.parent.mkdir(parents=True, exist_ok=True)

        with tempfile.TemporaryDirectory(prefix="source-", dir=destination.parent) as temporary:
            tree = Path(temporary) / "tree"
            extract(archive_path, tree, source)

            for relative, path, pin in inputs[1:]:
                slot = tree / relative
                # Git archives contain empty gitlink directories. Never overlay source files.

                if slot.exists():

                    if not slot.is_dir() or any(slot.iterdir()):
                        raise BuildError(f"Source submodule would overwrite upstream files: {relative}")

                    slot.rmdir()

                slot.parent.mkdir(parents=True, exist_ok=True)
                extract(path, slot, pin)

            for directory, _, files in os.walk(tree):

                for name in files:
                    path = Path(directory) / name
                    path.chmod(path.stat().st_mode & ~0o222)

            tree.rename(destination)

        write_json(marker, expected_state)
        return

    expected: set[Path] = set()
    try:

        for prefix, path, _ in inputs:

            with tarfile.open(path) as archive:
                _, members = _validate_archive_members(archive)

                for member in members:
                    relative = prefix / Path(*PurePosixPath(member.name).parts[1:])

                    if relative == Path("."):
                        continue

                    expected.add(relative)
                    expected.update(parent for parent in relative.parents if parent != Path("."))
                    path = destination / relative
                    reject_symlinks(path)

                    if member.isdir():

                        if not path.is_dir():
                            raise BuildError(f"Cached source directory is missing: {path}")

                        continue

                    validate_regular_file(path, limit=member.size)
                    metadata = path.stat()

                    if (
                        metadata.st_size != member.size or metadata.st_mode & 0o222
                        or metadata.st_mode & 0o111 != member.mode & 0o111
                    ):
                        raise BuildError(f"Cached source file changed: {path}")

                    with archive.extractfile(member) as archived, path.open("rb") as current:
                        expected_hash = hashlib.file_digest(archived, "sha256").digest()

                        if expected_hash != hashlib.file_digest(current, "sha256").digest():
                            raise BuildError(f"Cached source file changed: {path}")

        actual = set()

        for directory, directories, files in os.walk(destination, followlinks=False):

            for name in (*directories, *files):
                path = Path(directory) / name
                reject_symlinks(path)
                actual.add(path.relative_to(destination))

        if actual != expected:
            raise BuildError(f"Cached source tree contains missing or unexpected entries: {destination}")

    except (tarfile.TarError, EOFError) as exc:
        raise BuildError(f"Cannot verify source archive: {archive_path}: {exc}") from exc
