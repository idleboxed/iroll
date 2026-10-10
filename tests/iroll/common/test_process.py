"""Compiler process ownership with harmless Python subprocesses. SPDX-License-Identifier: BSD-3-Clause."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from iroll.common.errors import BuildError
from iroll.common.process import Runner


def test_runner_captures_output_and_preserves_argument_boundaries(tmp_path: Path) -> None:
    result = Runner().run([sys.executable, "-c", "import sys; print(sys.argv[1])", "one two"], cwd=tmp_path)

    assert result.stdout == "one two\n"
    assert result.returncode == 0


def test_runner_appends_compiler_output_to_log(tmp_path: Path) -> None:
    log = tmp_path / "compiler.log"

    for message in ("first", "second"):
        Runner().run([sys.executable, "-c", f"print({message!r})"], cwd=tmp_path, log=log)

    assert "first" in log.read_text() and "second" in log.read_text()


def test_runner_reports_failure_with_log_location(tmp_path: Path) -> None:
    log = tmp_path / "compiler.log"

    with pytest.raises(BuildError, match="exited with status 7; log:"):
        Runner().run([sys.executable, "-c", "raise SystemExit(7)"], cwd=tmp_path, log=log)


def test_runner_allows_nonzero_dependency_probe(tmp_path: Path) -> None:
    result = Runner().run([sys.executable, "-c", "raise SystemExit(3)"], cwd=tmp_path, check=False)

    assert result.returncode == 3


def test_runner_passes_input_and_only_requested_descriptor(tmp_path: Path) -> None:
    path = tmp_path / "synthetic-device"
    path.write_bytes(b"synthetic bytes")

    with path.open("rb") as stream:
        code = "import os, sys; print(sys.stdin.read()); print(os.read(int(sys.argv[1]), 100).decode())"

        result = Runner().run(
            [sys.executable, "-c", code, f"{stream.fileno()}"], cwd=tmp_path,
            input_data="synthetic table", pass_fds=(stream.fileno(),),
        )

    assert result.stdout == "synthetic table\nsynthetic bytes\n"


def test_runner_cancellation_stops_and_reaps_child(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    children = []

    def cancel(child: subprocess.Popen[str], *args: object, **kwargs: object) -> tuple[str, str]:
        children.append(child)
        raise KeyboardInterrupt

    monkeypatch.setattr(subprocess.Popen, "communicate", cancel)

    with pytest.raises(KeyboardInterrupt, match="^$"):
        Runner().run([sys.executable, "-c", "import time; time.sleep(60)"], cwd=tmp_path)

    assert children[0].returncode is not None

    with pytest.raises(ProcessLookupError, match="No such process"):
        os.kill(children[0].pid, 0)
