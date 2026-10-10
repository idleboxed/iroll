"""Confined SDK archives, including Bootlin's internal soft and hard links.

SPDX-License-Identifier: BSD-3-Clause
"""

import hashlib
import json
import os
import posixpath
import shutil
import stat
import tarfile
from pathlib import Path, PurePosixPath

from ..common.errors import BuildError
from ..common.files import fingerprint_paths, reject_symlinks
from ..components.registry import Source
from ..components.sources import MAX_EXTRACTED, MAX_MEMBERS, verify


def extract_sdk_archive(archive_path: Path, destination: Path, source: Source) -> None:
    """Strip one archive root; never write through a link or outside destination."""
    verify(archive_path, source)
    reject_symlinks(destination)

    if destination.exists():
        raise BuildError(f"SDK extraction directory already exists: {destination}")

    created = False
    try:

        with tarfile.open(archive_path) as archive:
            members = archive.getmembers()

            if len(members) > MAX_MEMBERS or sum(member.size for member in members) > MAX_EXTRACTED:
                raise BuildError("SDK archive exceeds extraction limits")

            entries = {}
            roots = set()

            for member in members:
                parts = PurePosixPath(member.name).parts

                if (
                    not parts or member.name.startswith("/") or ".." in parts or "\\" in member.name
                    or any(ord(char) < 32 for char in member.name)
                    or not (member.isdir() or member.isfile() or member.issym() or member.islnk())
                    or member.mode & 0o7000 or member.size < 0
                ):
                    raise BuildError(f"Unsafe SDK archive member: {member.name!r}")

                roots.add(parts[0])
                name = PurePosixPath(*parts[1:]).as_posix()

                if name in entries:
                    raise BuildError(f"Duplicate SDK archive member: {member.name!r}")

                if name == "." and not member.isdir():
                    raise BuildError("SDK archive root must be a directory")

                entries[name] = member

            if len(roots) != 1:
                raise BuildError("SDK archive must have exactly one top-level directory")

            top = roots.pop()
            targets = {}

            for name, member in entries.items():

                for parent in PurePosixPath(name).parents:
                    ancestor = entries.get(parent.as_posix())

                    if ancestor is not None and not ancestor.isdir():
                        raise BuildError(f"SDK archive writes through a non-directory: {name}")

                if not (member.issym() or member.islnk()):
                    continue

                link = member.linkname

                if not link or link.startswith("/") or "\\" in link or any(ord(char) < 32 for char in link):
                    raise BuildError(f"Unsafe SDK archive link: {name}")

                if member.issym():
                    target = posixpath.normpath(posixpath.join(posixpath.dirname(name), link))

                else:
                    full = posixpath.normpath(link)

                    if not full.startswith(f"{top}/"):
                        raise BuildError(f"SDK hard link escapes archive root: {name}")

                    target = full[len(top) + 1:]

                    if target not in entries or not entries[target].isfile():
                        raise BuildError(f"SDK hard link must target a regular archive member: {name}")

                if target == ".." or target.startswith("../"):
                    raise BuildError(f"SDK link escapes archive root: {name}")

                targets[name] = target

            destination.mkdir(parents=True)
            created = True

            for name, member in entries.items():
                path = destination / name

                if member.isdir():
                    path.mkdir(parents=True, exist_ok=True)

                elif member.isfile():
                    path.parent.mkdir(parents=True, exist_ok=True)

                    with archive.extractfile(member) as source_file, path.open("xb") as destination_file:
                        shutil.copyfileobj(source_file, destination_file)

                    path.chmod(member.mode & 0o777)

            # Files precede hard links; soft links are installed only after all writes.

            for name, target in targets.items():
                member = entries[name]
                path = destination / name
                path.parent.mkdir(parents=True, exist_ok=True)

                if member.islnk():
                    os.link(destination / target, path)

                else:
                    path.symlink_to(member.linkname)

            validate_sdk_links(destination)

    except BaseException as exc:

        if created:
            shutil.rmtree(destination)

        if isinstance(exc, (tarfile.TarError, EOFError)):
            raise BuildError(f"Cannot extract SDK archive: {archive_path}: {exc}") from exc

        raise


def validate_sdk_links(root: Path) -> None:
    """Permit dangling internal links, never external links or special files."""
    root = root.absolute()

    for directory, directories, files in os.walk(root, followlinks=False):

        for name in (*directories, *files):
            path = Path(directory) / name
            metadata = path.lstat()

            if stat.S_ISLNK(metadata.st_mode):
                try:
                    resolved = path.resolve()

                except (RuntimeError, OSError) as exc:
                    raise BuildError(f"Invalid SDK link: {path}") from exc

                if path.readlink().is_absolute() or not resolved.is_relative_to(root):
                    raise BuildError(f"SDK link escapes its root: {path}")

            elif not (stat.S_ISDIR(metadata.st_mode) or stat.S_ISREG(metadata.st_mode)):
                raise BuildError(f"Special file in SDK: {path}")


def fingerprint_sdk(root: Path) -> str:
    """Include every alias as well as the shared toolchain content fingerprint."""
    validate_sdk_links(root)
    links = []

    for directory, directories, files in os.walk(root, followlinks=False):

        for name in (*directories, *files):
            path = Path(directory) / name

            if path.is_symlink():
                links.append((f"{path.relative_to(root)}", os.readlink(path)))

    # fingerprint_paths deduplicates resolved paths, so aliases need their own record.
    payload = [fingerprint_paths({root: True}), sorted(links)]
    return hashlib.sha256(json.dumps(payload).encode()).hexdigest()
