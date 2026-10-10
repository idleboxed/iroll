"""User-supplied platform firmware; never download it or include it in the package.

SPDX-License-Identifier: BSD-3-Clause
"""

import os
import re
import shutil
from pathlib import Path

from ..common import paths
from ..common.errors import BuildError
from ..common.files import calculate_sha256, parse_relative_path, reject_symlinks, validate_regular_file


def collect(root: Path) -> dict[str, str]:
    """Snapshot regular firmware files, preserving arbitrary relative subdirectories."""
    reject_symlinks(root)

    if not root.exists():
        return {}

    if not root.is_dir():
        raise BuildError(f"BIOS root is not a directory: {root}")

    result = {}
    device = root.stat().st_dev

    for platform in sorted(root.iterdir()):
        reject_symlinks(platform)

        if not re.fullmatch(r"[A-Z0-9][A-Z0-9_-]{0,63}", platform.name) or not platform.is_dir():
            raise BuildError(f"Expected a BIOS platform directory: {platform}")

        if platform.stat().st_dev != device:
            raise BuildError(f"BIOS directory is on another device: {platform}")

        pending = [platform]

        while pending:
            directory = pending.pop()

            with os.scandir(directory) as entries:

                for entry in entries:
                    source = Path(entry.path)
                    relative = source.relative_to(platform).as_posix()
                    parse_relative_path(relative)
                    try:
                        relative.encode("utf-8")

                    except UnicodeEncodeError as exc:
                        raise BuildError(f"BIOS filename is not UTF-8: {source!s}") from exc

                    if any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in relative):
                        raise BuildError(f"Invalid BIOS filename: {source}")

                    reject_symlinks(source)
                    metadata = source.stat()

                    if metadata.st_dev != device:
                        raise BuildError(f"BIOS path is on another device: {source}")

                    if source.is_dir():
                        pending.append(source)
                        continue

                    validate_regular_file(source)

                    if metadata.st_nlink != 1:
                        raise BuildError(f"BIOS must be a single-link file: {source}")

                    result[f"{paths.BIOS_DIR}/{platform.name}/{relative}"] = calculate_sha256(source)

    return dict(sorted(result.items()))


def stage(root: Path, payload: Path, expected: dict[str, str]) -> None:
    """Copy only the validated snapshot; publication uses the common deploy path."""

    if collect(root) != expected:
        raise BuildError("BIOS inputs changed during the build; publication cancelled")

    for name, digest in expected.items():
        relative = parse_relative_path(name)
        source = root.joinpath(*relative.parts[1:])
        target = payload / relative
        validate_regular_file(source)
        target.parent.mkdir(parents=True, exist_ok=True)

        with target.open("xb") as output, source.open("rb") as input_file:
            shutil.copyfileobj(input_file, output)

        target.chmod(0o644)

        if calculate_sha256(target) != digest or calculate_sha256(source) != digest:
            raise BuildError(f"BIOS input changed while copying: {source}")
