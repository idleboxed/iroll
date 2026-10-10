"""Isolated Make build trees and a disposable publication stage.

SPDX-License-Identifier: BSD-3-Clause
"""

import logging
import os
import shutil
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from ..common import paths
from ..common.errors import BuildError
from ..common.files import read_cached_json, reject_symlinks, sync_directory, validate_regular_file, write_json


logger = logging.getLogger(__name__)
BUILD_STATE = ".iroll-build.json"


def mirror_sources(source: Path, compiled: Path) -> None:
    """Expose unchanged source files through links, with real writable directories.

    Upstream in-tree Makefiles still see their expected relative paths, but
    generated headers, object files and configure output stay in compiled.
    The caller holds the working-root lock throughout preparation and building.
    """
    reject_symlinks(source)
    reject_symlinks(compiled)

    if not source.is_absolute() or not compiled.is_absolute():
        raise BuildError("Source and compiled paths must be absolute")

    source = source.resolve()
    compiled = compiled.resolve()

    if source == compiled or source in compiled.parents or compiled in source.parents:
        raise BuildError("Source and compiled paths overlap unsafely")

    if not source.is_dir():
        raise BuildError(f"Source tree is not a directory: {source}")

    if compiled.exists():

        if not compiled.is_dir():
            raise BuildError(f"Compiled tree is not a directory: {compiled}")

        shutil.rmtree(compiled)

    compiled.mkdir(parents=True)

    for parent, directories, files in os.walk(source, followlinks=False):
        directory = Path(parent)

        for name in directories:
            reject_symlinks(directory / name)

        target = compiled / directory.relative_to(source)
        target.mkdir(exist_ok=True)

        for name in files:
            original = directory / name
            validate_regular_file(original)
            (target / name).symlink_to(original)


@contextmanager
def reuse_cached_build(
    source: Path, compiled: Path, state: dict[str, object], *, required: tuple[Path, ...] = (),
) -> Iterator[bool]:
    """Reuse only a completed matching build, under the caller's working-root lock."""
    marker = compiled / BUILD_STATE
    previous = read_cached_json(marker)

    for path in required:
        reject_symlinks(path)

    missing_configuration = any(not path.is_file() for path in required)
    reuse = previous == state and not missing_configuration

    if reuse:
        # A failed or interrupted compiler must never leave a success marker.
        marker.unlink()
        sync_directory(compiled)
        logger.info("Reusing compiled files: %s", compiled)

    else:
        reasons = []

        if not isinstance(previous, dict):
            reasons.append("missing or invalid success marker (new or interrupted build)")

        elif previous.get("version") != state.get("version"):
            reasons.append("cache format changed")

        elif previous != state:

            for fields, reason in (
                (("source_sha256", "submodules_sha256"), "sources changed"),
                (("recipe_sha256",), "recipe changed"),
                (("profile_sha256",), "profile changed"),
                (("environment_sha256",), "environment changed"),
                (("toolchain_sha256",), "toolchain changed"),
                (("host_toolchain_sha256",), "host toolchain changed"),
            ):

                if any(previous.get(field) != state.get(field) for field in fields):
                    reasons.append(reason)

            if not reasons:
                reasons.append("build state changed")

        if missing_configuration:
            reasons.append("required configuration missing")

        logger.info("Build cache miss: %s (%s)", compiled, "; ".join(reasons))
        mirror_sources(source, compiled)

    yield reuse
    write_json(marker, state)


@contextmanager
def create_staging_tree(directory: Path) -> Iterator[Path]:
    """Create a fresh SD-shaped staging tree; never reuse an interrupted stage."""
    reject_symlinks(directory)
    try:
        directory.mkdir(mode=0o700)

    except FileExistsError as exc:
        raise BuildError(f"Unfinished output staging: {directory}; inspect and remove it before retrying") from exc

    try:
        payload = directory / paths.SYSTEM_DIR
        payload.mkdir()
        yield payload
    finally:
        shutil.rmtree(directory)
