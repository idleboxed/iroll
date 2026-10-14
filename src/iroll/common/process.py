"""Subprocess ownership and build logs; never execute installed applications.

SPDX-License-Identifier: BSD-3-Clause
"""

import logging
import os
import shlex
import signal
import subprocess
from collections.abc import Mapping, Sequence
from contextlib import nullcontext
from pathlib import Path

from .errors import BuildError

logger = logging.getLogger(__name__)


class Runner:
    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str] | None = None,
        log: Path | None = None,
        check: bool = True,
        input_data: str | None = None,
        pass_fds: tuple[int, ...] = (),
    ) -> subprocess.CompletedProcess[str]:
        """Run a command with controlled streams and clean up its process group on cancellation."""
        logger.debug("Command: %s (cwd=%s)", shlex.join(argv), cwd)

        with log.open("a", encoding="utf-8") if log else nullcontext(None) as output:

            if output:
                output.write(f"\n$ {shlex.join(argv)}\n")
                output.flush()

            with subprocess.Popen(
                argv,
                cwd=cwd,
                env=env,
                stdin=subprocess.PIPE if input_data is not None else subprocess.DEVNULL,
                stdout=output or subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                errors="replace",
                start_new_session=True,
                pass_fds=pass_fds,
            ) as child:
                try:
                    stdout, _ = child.communicate(input_data)

                except BaseException:
                    # A compiler may own more children; cancellation must reap the whole group.
                    try:
                        os.killpg(child.pid, signal.SIGTERM)

                    except ProcessLookupError:
                        pass

                    try:
                        child.wait(timeout=3)

                    except subprocess.TimeoutExpired:
                        os.killpg(child.pid, signal.SIGKILL)
                        child.wait()

                    raise

            result = subprocess.CompletedProcess(list(argv), child.returncode, stdout or "", "")

        if check and result.returncode:
            detail = f"; log: {log}" if log else f": {result.stdout[-2000:].strip()}"
            raise BuildError(f"{argv[0]} exited with status {result.returncode}{detail}")

        return result
