"""Build selected recipes, then publish. No runtime execution.

SPDX-License-Identifier: BSD-3-Clause
"""

import hashlib
import json
import logging
import re
import shutil
import tomllib
from contextlib import ExitStack
from dataclasses import asdict, dataclass
from importlib.resources import files as resource_files
from itertools import combinations
from pathlib import Path
from typing import Any

from ..emulation import bios, shaders
from ..host import dependencies
from . import deploy
from ..common import paths
from ..common.errors import BuildError
from ..common.files import (
    acquire_directory_lock,
    calculate_sha256,
    create_report_directory,
    parse_relative_path,
    read_elf_architecture,
    reject_symlinks,
    validate_regular_file,
    write_json,
)
from ..host.platforms import COMMON_CONFIGURE, PROFILES, Toolchain
from ..common.process import Runner
from ..components.registry import Component
from ..components.sources import SourceDownloads, prepare_source
from .workspace import create_staging_tree, reuse_cached_build
from .trampoline import build_trampoline, resolve_trampoline_sources

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BuildOptions:
    source_root: Path
    dest: Path
    downloads: Path
    build_dir: Path
    logs: Path
    toolchain: Toolchain
    components: tuple[Component, ...]
    jobs: int = 4
    offline: bool = False
    archives: Path | None = None
    bios: Path | None = None
    no_diag: bool = False

    def __post_init__(self) -> None:

        if self.archives is None:
            object.__setattr__(self, "archives", self.downloads)


def resolve_cargo_sources(root: Path, component: Component) -> Path:
    # Local project links are an authoring convenience, not an installation boundary.
    # Resolve them once; output and build-data paths still reject every symlink.
    source = (root / parse_relative_path(component.directory)).resolve(strict=True)
    manifest = source / paths.CARGO_MANIFEST
    validate_regular_file(manifest, limit=1024 * 1024)
    validate_regular_file(source / paths.CARGO_LOCK, limit=1024 * 1024)
    try:
        document = tomllib.loads(manifest.read_text(encoding="utf-8"))

    except (ValueError, UnicodeError, RecursionError) as exc:
        raise BuildError(f"Invalid Cargo manifest: {manifest}") from exc

    package = document.get("package")

    if not isinstance(package, dict) or package.get("name") != component.name:
        raise BuildError(f"Wrong Cargo package for {component.name}: {manifest}")

    # Cargo resolves path dependencies itself; failures never publish a partial tree.
    return source


def build_commands(
    component: Component,
    options: BuildOptions,
    source: Path,
    cargo_target: Path,
    payload: Path,
) -> tuple[list[list[str]], Path]:

    if component.recipe == "cargo":
        argv = [
            "cargo",
            "build",
            "--locked",
            "--release",
            "--target",
            options.toolchain.profile.rust_target,
            "--target-dir",
            f"{cargo_target}",
            "--jobs",
            f"{options.jobs}",
            "--manifest-path",
            f"{source / paths.CARGO_MANIFEST}",
        ]

        if options.offline:
            argv.append("--offline")

        if options.toolchain.profile.cross:

            if component.name == "igui":
                argv.extend(("--no-default-features", "--features", "drm,rgb565"))

            elif component.name == "iman":
                argv.extend(("--features", "drm" if options.no_diag else "drm,diagnostics"))

        artifact = cargo_target / options.toolchain.profile.rust_target / paths.RELEASE_DIR / component.artifact
        return [argv], artifact

    artifact = payload / parse_relative_path(component.destination)
    make = ["make", "-j", f"{options.jobs}", "-f", component.makefile, f"TARGET={artifact}"]

    if component.recipe == "make":
        make += [
            "platform=unix",
            "DEBUG=0",
            f"CC={options.toolchain.cc}",
            f"CXX={options.toolchain.cxx}",
            f"AR={options.toolchain.ar}",
        ]

        if component.source:
            make.append(f"GIT_VERSION={component.source.revision[:7]}")
            make.append(f"GIT_REVISION={component.source.revision[:7]}")

        make.extend(component.make_args)

        if options.toolchain.profile.cross:
            make.extend(component.arm_make_args)

        return [make], artifact

    if component.recipe == "configure":
        return [
            ["sh", "./configure", *COMMON_CONFIGURE, *options.toolchain.profile.configure],
            make,
        ], artifact

    raise BuildError(f"Unknown recipe: {component.recipe}")


class Builder:
    def __init__(
        self, options: BuildOptions, *, runner: Runner | None = None, sources: SourceDownloads | None = None,
        confirm_replace: deploy.ConfirmReplace | None = None,
    ) -> None:
        self.options = options
        self.confirm_replace = confirm_replace
        self._toolchain_fingerprint: str | None = None
        self._host_toolchain_fingerprint: str | None = None
        self._bios_files: dict[str, str] = {}
        self.runner = runner if runner is not None else Runner()
        self.sources = (
            sources
            if sources is not None
            else SourceDownloads(options.downloads, offline=options.offline, archives=options.archives)
        )

    def validate_inputs(self) -> None:
        options = self.options

        if not 1 <= options.jobs <= 256:
            raise BuildError("--jobs must be between 1 and 256")

        if not options.components:
            raise BuildError("No components selected")

        data_roots = (options.downloads, options.build_dir, options.logs)

        for path in (options.source_root, options.dest, *data_roots):

            if not path.is_absolute():
                raise BuildError(
                    "Build service requires absolute source, destination, downloads, build and logs paths"
                )

            reject_symlinks(path)

            if path.exists() and not path.is_dir():
                raise BuildError(f"Build path is not a directory: {path}")

        for relative in (
            Path(paths.OUR_BUILD_DIR) / options.toolchain.profile.name,
            Path(paths.THEIR_SOURCE_DIR),
            Path(paths.THEIR_COMPILED_DIR) / options.toolchain.profile.name,
            Path(paths.OUT_STAGE_DIR),
        ):
            reject_symlinks(options.build_dir / relative)

        if (options.build_dir / paths.OUT_STAGE_DIR).exists():
            raise BuildError("Unfinished output staging; inspect and remove out_stage before retrying")

        if (
            any(component.source for component in options.components)
            and not re.fullmatch(r"[A-Za-z0-9_./+-]+", f"{options.build_dir}")
        ):
            raise BuildError(
                "Upstream Make builds require a build path without whitespace or shell/Make metacharacters"
            )

        # Compare normalized paths so '..' cannot hide an overlap; symlinks were rejected above.
        source = options.source_root.resolve()
        dest = options.dest.resolve()
        roots = tuple(path.resolve() for path in data_roots)
        _, build, logs = roots
        overlap_pairs = list(combinations((dest, *roots), 2))
        # Reports may live under the working root, but cannot overlap
        # source/compiler data or the disposable publication stage.

        if logs != build and logs.is_relative_to(build):
            overlap_pairs.remove((build, logs))
            overlap_pairs.extend((logs, build / name) for name in (paths.BUILD_DIR, paths.OUT_STAGE_DIR))

        system = dest / paths.SYSTEM_DIR

        if (
            dest == source
            or dest in source.parents
            or system == source
            or system in source.parents
            or any(root == source or root in source.parents for root in roots)
            or any(
                left == right or left in right.parents or right in left.parents
                for left, right in overlap_pairs
            )
        ):
            raise BuildError("Source, destination, downloads, build and logs paths overlap unsafely")

        if options.bios is not None:

            if not options.bios.is_absolute():
                raise BuildError("Build service requires an absolute BIOS path")

            reject_symlinks(options.bios)
            firmware = options.bios.resolve()

            if any(
                firmware == other or firmware in other.parents or other in firmware.parents
                for other in (source, dest, *roots)
            ):
                raise BuildError("BIOS inputs overlap source, destination or build-data paths")

            self._bios_files = bios.collect(options.bios)

            for name in self._bios_files:
                deploy.read_file_state(system / name)

        destinations = set()

        for component in options.components:

            if options.toolchain.profile.name not in component.platforms:
                raise BuildError(f"Unsupported component {component.name}: {component.unavailable_reason}")

            destination = parse_relative_path(component.destination)

            if destination in destinations:
                raise BuildError(f"Duplicate component destination: {destination}")

            destinations.add(destination)

            if component.recipe == "cargo":
                resolve_cargo_sources(options.source_root, component)

            elif component.recipe == "trampoline":
                resolve_trampoline_sources(options.source_root)

            elif component.source is None:
                raise BuildError(f"Missing pinned source for {component.name}")

        report = dependencies.check(options.components, options.toolchain, self.runner, cwd=options.source_root)

        if not report.ready:
            details = "\n".join((*report.missing, *report.instructions))
            raise BuildError(f"Build dependencies are not ready:\n{details}")

        deploy.validate_destination(options.dest, options.toolchain.profile, options.components)

    def run(self) -> Path:
        options = self.options
        logger.info("Platform: %s", options.toolchain.profile.name)
        self.validate_inputs()
        self._toolchain_fingerprint = None
        self._host_toolchain_fingerprint = None

        with ExitStack() as locks:

            if options.bios is not None and options.bios.exists():
                locks.enter_context(acquire_directory_lock(options.bios))

            for directory in (options.downloads, options.build_dir, options.logs):
                directory.mkdir(parents=True, exist_ok=True)
                locks.enter_context(acquire_directory_lock(directory))

            log_dir = create_report_directory(options.logs)
            logger.info("Build reports: %s", log_dir)
            try:

                with create_staging_tree(options.build_dir / paths.OUT_STAGE_DIR) as payload:
                    records = {}

                    for component in options.components:
                        records[component.name] = self.build_component(component, payload, log_dir)

                    if options.bios is not None:
                        bios.stage(options.bios, payload, self._bios_files)

                    kept = deploy.publish(
                        payload, options.dest, records, options.toolchain.profile,
                        confirm_replace=self.confirm_replace,
                        bios_files=self._bios_files,
                    )

                write_json(
                    log_dir / paths.REPORT_FILE,
                    {
                        "status": "BUILT_AND_PUBLISHED",
                        "platform": options.toolchain.profile.name,
                        "destination": f"{options.dest / paths.SYSTEM_DIR}",
                        "components": records,
                        "bios": self._bios_files,
                        "bundled_shader_source": shaders.read_source_info(),
                        "kept_files": kept,
                        "applications_launched": False,
                        "hardware_verified": False,
                    },
                )

            except (Exception, KeyboardInterrupt) as exc:
                try:
                    write_json(
                        log_dir / paths.FAILURE_FILE,
                        {
                            "error": f"{exc}" or type(exc).__name__,
                            "destination": f"{options.dest}",
                            "automatic_retry": False,
                            "publication_may_be_partial": True,
                        },
                    )

                except OSError:
                    logger.error("Could not save failure report in %s", log_dir)

                raise

        logger.info(
            "Built %d components into %s",
            len(options.components),
            options.dest / paths.SYSTEM_DIR,
        )
        return log_dir

    def build_component(self, component: Component, payload: Path, logs: Path) -> dict[str, Any]:
        options = self.options

        if component.recipe == "trampoline":
            logger.info("Building %s ...", component.name)
            return build_trampoline(
                options.source_root, payload, logs,
                options.build_dir / paths.OUR_BUILD_DIR / options.toolchain.profile.name,
                runner=self.runner,
            )

        if component.source:
            archive = self.sources.obtain(component.source)
            source_root = (
                options.build_dir / paths.THEIR_SOURCE_DIR / parse_relative_path(component.name)
            )
            modules = tuple(
                (module.directory, self.sources.obtain(module.source), module.source)
                for module in component.submodules
            )
            prepare_source(archive, source_root, component.source, modules=modules)
            compiled = (
                options.build_dir / paths.THEIR_COMPILED_DIR / options.toolchain.profile.name
                / parse_relative_path(component.name)
            )
            source = compiled if component.directory == "." else compiled / parse_relative_path(component.directory)

        else:
            source_root = resolve_cargo_sources(options.source_root, component)
            source = source_root

        cargo_target = options.build_dir / paths.OUR_BUILD_DIR / options.toolchain.profile.name
        argv_list, artifact = build_commands(component, options, source, cargo_target, payload)
        staged_binary = payload / parse_relative_path(component.destination)
        staged_binary.parent.mkdir(parents=True, exist_ok=True)
        logger.info("Building %s ...", component.name)
        environment = (
            options.toolchain.build_environment(rust=True) if component.recipe == "cargo"
            else options.toolchain.make_environment()
        )

        with ExitStack() as build_state:

            if component.source:

                if self._toolchain_fingerprint is None:
                    self._toolchain_fingerprint = options.toolchain.fingerprint(
                        self.runner, cwd=options.source_root, env=environment,
                    )

                inputs = {
                    "submodules": [asdict(module) for module in component.submodules],
                    "recipe": {
                        "component": {
                            key: value for key, value in asdict(component).items()
                            if key not in {"source", "submodules", "cli_selectable"}
                        },
                        # Only remove our scheduler option, not arbitrary recipe arguments.
                        "commands": [
                            argv[:1] + argv[3:] if argv[:2] == ["make", "-j"] else argv for argv in argv_list
                        ],
                        "source": f"{source_root}",
                    },
                    "profile": asdict(options.toolchain.profile),
                    "environment": environment,
                }
                state = {
                    "version": 2, "source_sha256": component.source.sha256,
                    # Category hashes explain misses without persisting environment names or values.
                    **{
                        f"{name}_sha256": hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()
                        for name, value in inputs.items()
                    },
                    "toolchain_sha256": self._toolchain_fingerprint,
                }

                if component.host_tools and options.toolchain.profile.cross:

                    if self._host_toolchain_fingerprint is None:
                        native = Toolchain(PROFILES["linux-x86_64"])
                        self._host_toolchain_fingerprint = native.fingerprint(
                            self.runner, cwd=options.source_root, env=native.make_environment(),
                        )

                    state["host_toolchain_sha256"] = self._host_toolchain_fingerprint

                required = (
                    tuple(source / name for name in ("config.h", "config.mk"))
                    if component.recipe == "configure" else ()
                )
                reuse = build_state.enter_context(reuse_cached_build(source_root, compiled, state, required=required))

                if reuse:
                    argv_list = [argv for argv in argv_list if argv[0] == "make"]

            for argv in argv_list:
                self.runner.run(argv, cwd=source, env=environment, log=logs / f"{component.name}.log")

            files = {}

            def copy_file(source: Path, name: str, mode: int) -> None:
                validate_regular_file(source)
                target = payload / parse_relative_path(name)

                if target.exists():
                    raise BuildError(f"Duplicate payload file: {name}")

                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, target)
                target.chmod(mode)
                files[name] = calculate_sha256(target)

            if read_elf_architecture(artifact) != options.toolchain.profile.architecture:
                raise BuildError(f"Built artifact has a different architecture: {artifact}")

            mode = 0o644 if component.core else 0o755

            if artifact == staged_binary:
                artifact.chmod(mode)
                files[component.destination] = calculate_sha256(artifact)

            else:
                # Cargo owns its reusable target tree; never move its final artifacts out.
                copy_file(artifact, component.destination, mode)

            for resource in component.resources:
                relative = parse_relative_path(resource.directory)
                directory = source_root / relative
                reject_symlinks(directory)
                resources = sorted(directory.glob("*.json"))

                if not resources:
                    raise BuildError(f"No JSON resource files found: {directory}")

                destination = parse_relative_path(component.destination).parent / relative

                for resource_file in resources:
                    copy_file(resource_file, (destination / resource_file.name).as_posix(), 0o644)

            if component.name == "iman" and options.toolchain.profile.cross:
                # Packaged board settings, not a dependency on a historical BOX2
                # payload or a checkout-relative sample directory.
                resources = {
                    "iman/iman.json": resource_files("iroll").joinpath(
                        "resources", "sd", "rk3032", "system", "iman", "iman.json",
                    ).read_bytes(),
                }

                for name, data in resources.items():
                    target = payload / name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(data)
                    files[name] = calculate_sha256(target)

            return {
                "binary": component.destination,
                "files": files,
                "game_platforms": list(component.game_platforms),
                "source": asdict(component.source) if component.source else {"kind": "local-cargo-locked"},
                "submodules": [asdict(module) for module in component.submodules],
            }
