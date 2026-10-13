"""Synthetic build-only trampoline artifacts; no Cargo, SD or database operations."""

import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from iroll.common.errors import BuildError
from iroll.components.registry import select
from iroll.rollout.build import BuildOptions, Builder


@pytest.fixture
def trampoline_options(options: BuildOptions, toolchains: dict[str, Any]) -> BuildOptions:
    source = options.source_root / "itrampoline"
    (source / "src/itrampoline/resources/runtime").mkdir(parents=True)
    (source / "pyproject.toml").write_text('[project]\nname = "itrampoline"\n')
    (source / "src/itrampoline/build.py").write_text("# Synthetic build boundary\n")
    (source / "src/itrampoline/resources/runtime/Cargo.lock").write_text("version = 4\n")
    return replace(
        options, components=select("rk3032", ["itrampoline"]), toolchain=toolchains["rk3032"],
    )


@pytest.mark.parametrize("failure", [None, "architecture", "compiler", "cancel"])
def test_trampoline_only_build_does_not_install_core_or_touch_database(
    trampoline_options: BuildOptions, runner: Any, monkeypatch: pytest.MonkeyPatch, failure: str | None,
) -> None:
    options = trampoline_options
    database = options.dest / "game/games.db"
    database.parent.mkdir(parents=True)
    database.write_bytes(b"Synthetic database; must not be opened")
    run_process = runner.run

    def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:

        if argv[0] != sys.executable:
            return run_process(argv, **kwargs)

        runner.calls.append((argv, kwargs["cwd"], {}))

        if failure == "compiler":
            raise BuildError("Synthetic compiler failure")

        if failure == "cancel":
            raise KeyboardInterrupt

        assert argv[1:3] == ["-I", "-c"]
        assert argv[4] == f"{options.source_root / 'itrampoline/src'}"
        elf = bytearray(64)
        elf[:7] = b"\x7fELF" + bytes((2 if failure == "architecture" else 1, 1, 1))
        elf[18:20] = (62 if failure == "architecture" else 40).to_bytes(2, "little")

        for filename in argv[-2:]:
            Path(filename).write_bytes(elf)

        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(runner, "run", run)

    if failure:
        error_type = KeyboardInterrupt if failure == "cancel" else BuildError
        message = {"cancel": "^$", "compiler": "Synthetic compiler failure", "architecture": "not ARM"}[failure]

        with pytest.raises(error_type, match=message):
            Builder(options, runner=runner).run()

        assert not (options.dest / "system/itrampoline").exists()

    else:
        reports = Builder(options, runner=runner).run()
        record = json.loads((reports / "result.json").read_text())["components"]["itrampoline"]
        assert record["binary"] == "itrampoline"
        assert Path(record["artifacts"]["core"]["path"]).is_file()
        assert (options.dest / "system/itrampoline").stat().st_mode & 0o777 == 0o755

    assert database.read_bytes() == b"Synthetic database; must not be opened"
    assert not (options.dest / "retro_lib").exists()
    assert not (options.dest / "system/retroarch/cores").exists()
    assert not (options.build_dir / "out_stage").exists()


def test_trampoline_source_metadata_fails_before_compilation(
    trampoline_options: BuildOptions, runner: Any,
) -> None:
    options = trampoline_options
    (options.source_root / "itrampoline/pyproject.toml").write_text('[project]\nname = "wrong"\n')

    with pytest.raises(BuildError, match="Wrong Python package"):
        Builder(options, runner=runner).run()

    assert not runner.calls
    assert not (options.dest / "system").exists()
