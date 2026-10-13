"""Build the selected itrampoline checkout without its SD installation CLI."""

import sys
import tempfile
import tomllib
from pathlib import Path
from typing import Any

from ..common.errors import BuildError
from ..common.files import calculate_sha256, read_elf_architecture, validate_regular_file
from ..common.process import Runner

# An isolated interpreter prevents a globally installed itrampoline from replacing
# the checkout selected by --source. Rust flags and ELF validation stay upstream.
BUILD_SCRIPT = """
import logging
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from itrampoline.build import build_runtime
logging.basicConfig(level=logging.INFO)
core, helper = build_runtime(Path(sys.argv[2]))
Path(sys.argv[3]).write_bytes(core)
Path(sys.argv[4]).write_bytes(helper)
"""


def resolve_trampoline_sources(root: Path) -> Path:
    source = (root / "itrampoline").resolve(strict=True)
    manifest = source / "pyproject.toml"
    validate_regular_file(manifest, limit=1024 * 1024)
    validate_regular_file(source / "src/itrampoline/build.py", limit=1024 * 1024)
    validate_regular_file(source / "src/itrampoline/resources/runtime/Cargo.lock", limit=1024 * 1024)
    try:
        document = tomllib.loads(manifest.read_text(encoding="utf-8"))

    except (ValueError, UnicodeError, RecursionError) as exc:
        raise BuildError(f"Invalid itrampoline manifest: {manifest}") from exc

    project = document.get("project")

    if not isinstance(project, dict) or project.get("name") != "itrampoline":
        raise BuildError(f"Wrong Python package for itrampoline: {manifest}")

    return source


def build_trampoline(
    root: Path, payload: Path, reports: Path, build_dir: Path, *, runner: Runner,
) -> dict[str, Any]:
    source = resolve_trampoline_sources(root)
    build_dir.mkdir(parents=True, exist_ok=True)
    helper_path = payload / "itrampoline"
    core_path = reports / "itrampoline_libretro.so"

    with tempfile.TemporaryDirectory(prefix="itrampoline-", dir=build_dir) as temporary:
        runner.run(
            [
                sys.executable, "-I", "-c", BUILD_SCRIPT, f"{source / 'src'}", temporary,
                f"{core_path}", f"{helper_path}",
            ],
            cwd=source, log=reports / "itrampoline.log",
        )

    for artifact in (helper_path, core_path):
        validate_regular_file(artifact)

        if read_elf_architecture(artifact) != "arm":
            raise BuildError(f"Built itrampoline artifact is not ARM: {artifact}")

    helper_path.chmod(0o755)
    core_path.chmod(0o644)
    return {
        "binary": "itrampoline",
        "files": {"itrampoline": calculate_sha256(helper_path)},
        "artifacts": {"core": {"path": f"{core_path}", "sha256": calculate_sha256(core_path)}},
        "game_platforms": [],
        "source": {"kind": "local-python-runtime"},
        "submodules": [],
    }
