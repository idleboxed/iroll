"""Overlay release files without copying or deleting unrelated user data.

Files are staged and verified before publication, then replaced individually. This
is not a whole-release or power-loss transaction; a failed commit may be partial.
SPDX-License-Identifier: BSD-3-Clause
"""

import json
import logging
import os
import shutil
import stat
import tempfile
from collections.abc import Callable
from importlib.resources import files
from pathlib import Path
from typing import Any

from ..emulation import joypads, shaders
from ..common import paths
from ..common.errors import BuildError
from ..common.files import (
    acquire_directory_lock,
    ensure_directory,
    validate_directory,
    calculate_sha256,
    parse_relative_path,
    read_elf_architecture,
    reject_symlinks,
    sync_directory,
    validate_regular_file,
    write_json,
)
from ..host.platforms import Profile
from ..emulation.manifest import build_runtime_manifest
from ..common.paths import prepare_save_directories
from ..components.registry import COMPONENTS, Component

logger = logging.getLogger(__name__)
ConfirmReplace = Callable[[Path], bool]


def load_json(path: Path) -> dict[str, Any]:
    """Read a bounded checked manifest file and require a JSON object."""
    validate_regular_file(path, limit=1024 * 1024)
    try:
        value = json.loads(path.read_bytes())

    except (ValueError, UnicodeError, RecursionError) as exc:
        raise BuildError(f"Invalid manifest: {path}") from exc

    if not isinstance(value, dict):
        raise BuildError(f"Manifest must be an object: {path}")

    return value


def read_file_state(path: Path) -> tuple[int, ...] | None:
    """Observe only a release destination, not every user file on the card."""
    validate_directory(path.parent)
    reject_symlinks(path)
    try:
        metadata = path.stat()

    except FileNotFoundError:
        return None

    if not stat.S_ISREG(metadata.st_mode):
        raise BuildError(f"File/directory conflict or non-regular destination: {path}")

    return (metadata.st_dev, metadata.st_ino, metadata.st_size, metadata.st_mtime_ns, metadata.st_ctime_ns)


def collect_existing_components(system: Path, profile: Profile, replacing: frozenset[str]) -> dict[str, Any]:
    """Read components that a partial update must retain from the existing runtime manifest."""
    state = system / paths.RUNTIME_MANIFEST
    result: dict[str, Any] = {}
    cores: dict[str, Any] = {}
    runtime_names = {
        component.name for component in COMPONENTS
        if profile.name in component.platforms and (component.core or component.name == "retro")
    }
    # A complete runtime replacement owns the manifest, including an edited or
    # broken previous copy. Partial updates still validate everything they reuse.

    if state.exists() and not runtime_names <= replacing:
        document = load_json(state)

        if document.get("architecture") != profile.architecture:
            raise BuildError("Existing emulation manifest has a different architecture or is missing architecture")

        cores = document.get("cores", {})

        if not isinstance(cores, dict):
            raise BuildError("Invalid existing core manifest")

        entries = [("retro", document.get("retroarch"), [])]
        entries.extend((f"installed-core-{platform}", value, [platform]) for platform, value in cores.items())
        replaced_platforms = {
            platform for component in COMPONENTS
            if component.name in replacing for platform in component.game_platforms
        }

        for name, entry, game_platforms in entries:

            if name in replacing or any(platform in replaced_platforms for platform in game_platforms):
                continue

            if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
                raise BuildError("Invalid existing runtime component")

            relative = parse_relative_path(entry["path"])
            current_digest = calculate_sha256(system / relative)

            if read_elf_architecture(system / relative) != profile.architecture:
                raise BuildError(f"Installed component has a different architecture: {relative}")

            name = next(
                (component.name for component in COMPONENTS if component.destination == relative.as_posix()), name,
            )
            platform_options = {platform: {
                key: entry[key]
                for key in ("library_name", "remap", "shader_preset", "system_directory", "core_options")
                if key in entry
            } for platform in game_platforms}

            if name in result:
                old = result[name]

                if old["binary"] != relative.as_posix() or old["files"][relative.as_posix()] != current_digest:
                    raise BuildError("Conflicting emulation components")

                old["game_platforms"].extend(game_platforms)
                old["platform_options"].update(platform_options)
                continue

            result[name] = {
                "binary": relative.as_posix(),
                "files": {relative.as_posix(): current_digest},
                "game_platforms": game_platforms,
                "platform_options": platform_options,
                "source": None,
            }

    # New aliases of a known installed core also become available in partial
    # rollouts. Preserve an explicit platform assignment to a different core.

    for component in COMPONENTS:

        if component.name in result:
            result[component.name]["game_platforms"].extend(
                platform for platform in component.game_platforms if platform not in cores
            )

    for component in COMPONENTS:

        if component.name in replacing:
            continue

        path = system / component.destination

        if path.exists() and read_elf_architecture(path) != profile.architecture:
            raise BuildError(f"Installed component has a different architecture: {path}")

        if path.is_file() and component.name not in result:
            result[component.name] = {
                "binary": component.destination,
                "files": {component.destination: calculate_sha256(path)},
                "game_platforms": [platform for platform in component.game_platforms if platform not in cores],
                "source": None,
            }

    return result


def validate_destination(dest: Path, profile: Profile, components: tuple[Component, ...]) -> None:
    """Reject unsafe destination paths and leftovers from an unfinished publication."""
    validate_directory(dest)
    validate_directory(dest / paths.SYSTEM_DIR)

    for relative in paths.SAVE_DIRECTORIES:
        validate_directory(dest / relative)

    if dest.exists() and any(
        path.name.startswith((paths.BACKUP_PREFIX, paths.STAGE_PREFIX)) for path in dest.iterdir()
    ):
        raise BuildError("Unfinished publication found; inspect .iroll-backup-/stage- directories before retrying")

    system = dest / paths.SYSTEM_DIR

    for component in components:
        read_file_state(system / component.destination)

    read_file_state(dest / "INSTRUCTIONS.md")
    read_file_state(system / paths.RUNTIME_MANIFEST)
    collect_existing_components(system, profile, frozenset(component.name for component in components))


def publish(
    payload: Path, dest: Path, records: dict[str, Any], profile: Profile,
    *, confirm_replace: ConfirmReplace | None = None, bios_files: dict[str, str] | None = None,
) -> tuple[str, ...]:
    """Publish selected files with checked replacements; return names of installed components."""
    reject_symlinks(dest)
    dest.mkdir(parents=True, exist_ok=True)

    with acquire_directory_lock(dest):
        selected = tuple(component for component in COMPONENTS if component.name in records)
        validate_destination(dest, profile, selected)
        reused = collect_existing_components(dest / paths.SYSTEM_DIR, profile, frozenset(records))
        components = dict(reused)
        components.update(records)
        release: dict[str, tuple[Path, str]] = {}

        for supplied in [*(record["files"] for record in records.values()), bios_files or {}]:

            for name, expected in supplied.items():
                relative = parse_relative_path(name)
                target = f"{paths.SYSTEM_DIR}/{relative.as_posix()}"

                if target in release:
                    raise BuildError(f"Duplicate release destination: {target}")

                release[target] = (payload / relative, expected)

        guide = payload.parent / "INSTRUCTIONS.md"
        guide.write_bytes(files("iroll").joinpath("resources", "sd", "common", "INSTRUCTIONS.md").read_bytes())
        release["INSTRUCTIONS.md"] = (guide, calculate_sha256(guide))
        manifest_name = f"{paths.SYSTEM_DIR}/{paths.RUNTIME_MANIFEST}"

        if "retro" in components:

            for name, expected in joypads.stage(payload).items():
                target = f"{paths.SYSTEM_DIR}/{name}"

                if target in release:
                    raise BuildError(f"Duplicate release destination: {target}")

                release[target] = (payload / name, expected)

            manifest = payload / paths.RUNTIME_MANIFEST
            manifest.parent.mkdir(parents=True, exist_ok=True)
            document = build_runtime_manifest(components, profile)

            for name, expected in shaders.stage(payload, document["cores"]).items():
                target = f"{paths.SYSTEM_DIR}/{name}"

                if target in release:
                    raise BuildError(f"Duplicate release destination: {target}")

                release[target] = (payload / name, expected)

            write_json(manifest, document)
            release[manifest_name] = (manifest, calculate_sha256(manifest))

        observed = {name: read_file_state(dest / name) for name in release}
        changed = {}

        for name, (source, expected) in release.items():

            if calculate_sha256(source) != expected:
                raise BuildError(f"Release checksum mismatch: {name}")

            previous = observed[name]
            executable = source.stat().st_mode & 0o111

            if (
                previous is not None and previous[2] == source.stat().st_size
                and (not executable or (dest / name).stat().st_mode & 0o111)
                and calculate_sha256(dest / name) == expected
            ):
                logger.debug("Unchanged: %s", name)

            else:
                changed[name] = (source, expected)

        # All decisions precede staging. The same publication path handles both
        # unattended updates and explicitly retained files; user data is not in release.
        kept = []

        for name in sorted(changed):

            if observed[name] is not None and confirm_replace is not None and not confirm_replace(dest / name):
                kept.append(name)
                del changed[name]
                logger.info("Kept existing: %s", name)

        if "retro" in components:
            # --ask may retain the old manifest or a preset. Validate the files
            # actually selected for publication, not a discarded proposed copy.
            effective = load_json(dest / manifest_name) if manifest_name in kept else document
            supplied = {
                name.removeprefix(f"{paths.SYSTEM_DIR}/"): source
                for name, (source, _) in changed.items() if name.startswith(f"{paths.SYSTEM_DIR}/")
            }
            shaders.validate_presets(dest / paths.SYSTEM_DIR, effective["cores"], supplied)

        # The old files coexist with only the changed staged files. Account for
        # allocation units and directory entries, not just sum(st_size).
        stats = os.statvfs(dest)
        block = max(stats.f_frsize, 4096)
        required = sum(((path.stat().st_size + block - 1) // block + 1) * block for path, _ in changed.values())
        required += block * (len(changed) * 4 + 8) if changed else 0

        if required > stats.f_bavail * stats.f_frsize:
            raise BuildError("Not enough free space to stage changed release files")

        if not changed:
            prepare_save_directories(dest)
            logger.info("No release files need publication; kept %d existing files by request", len(kept))
            return tuple(kept)

        with tempfile.TemporaryDirectory(prefix=paths.STAGE_PREFIX, dir=dest) as temporary:
            stage = Path(temporary)

            for name, (source, expected) in changed.items():
                target = stage / name
                target.parent.mkdir(parents=True, exist_ok=True)
                descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, stat.S_IMODE(source.stat().st_mode))
                os.close(descriptor)
                shutil.copyfile(source, target)
                # exFAT exposes mount-wide mode bits. Do not try to clear those
                # bits on data files or copy timestamps/ownership onto the SD.

                if source.stat().st_mode & 0o111 and not target.stat().st_mode & 0o111:
                    target.chmod(0o755)

                if calculate_sha256(target) != expected:
                    raise BuildError(f"Published copy checksum mismatch: {name}")

                with target.open("rb") as stream:
                    os.fsync(stream.fileno())

            for directory_path in sorted(
                (path for path in stage.rglob("*") if path.is_dir()), key=lambda path: len(path.parts), reverse=True,
            ):
                sync_directory(directory_path)

            sync_directory(stage)

            if any(read_file_state(dest / name) != before for name, before in observed.items()):
                raise BuildError("Release destination changed during staging; publication cancelled")

            if collect_existing_components(dest / paths.SYSTEM_DIR, profile, frozenset(records)) != reused:
                raise BuildError("Reused components changed during staging; publication cancelled")

            # Commit the runtime manifest last, after its binaries are durable.
            # Unknown files and existing user directories are never renamed.

            for name in sorted(changed, key=lambda value: (value == manifest_name, value)):
                target = dest / name
                ensure_directory(target.parent)

                if read_file_state(target) != observed[name]:
                    raise BuildError(f"Release destination changed during publication: {name}")

                os.replace(stage / name, target)
                sync_directory(target.parent)
                logger.info("Installed: %s", name)

            prepare_save_directories(dest)

        sync_directory(dest)
        logger.info("Published %d changed files", len(changed))
        return tuple(kept)
