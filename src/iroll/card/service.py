"""Explicit destructive initialization, separate from builds and installation.

SPDX-License-Identifier: BSD-3-Clause
"""

import logging
from collections.abc import Callable
from importlib.resources import files
from pathlib import Path
from typing import Any

from ..common import paths
from .journal import CardJournal, validate_journal_root
from .layout import INSTRUCTIONS_NAME, CardPlan, require, validate_same_card
from .linux import LinuxCardBackend
from ..common.files import create_report, write_json

logger = logging.getLogger(__name__)


def read_instructions() -> bytes:
    content = files("iroll").joinpath("resources", "sd", "common", INSTRUCTIONS_NAME).read_bytes()
    require(0 < len(content) <= 64 * 1024, "Missing or oversized packaged INSTRUCTIONS.md")
    content.decode("utf-8")
    require(content.endswith(b"\n"), "Packaged INSTRUCTIONS.md must end with a newline")
    return content


class CardInitializer:
    def __init__(self, backend: LinuxCardBackend | None = None) -> None:
        self.backend = backend or LinuxCardBackend()

    def create_plan(self, device: str) -> CardPlan:
        read_instructions()
        return CardPlan(self.backend.inspect(device))

    def run(
        self, device: str, *, logs_root: Path, dry_run: bool,
        confirm: Callable[[CardPlan], bool], owner: tuple[int, int] | None = None,
    ) -> dict[str, Any]:
        plan = self.create_plan(device)
        validate_journal_root(logs_root, plan.target, self.backend.read_mountinfo())
        report = create_report(logs_root, owner=owner)

        if dry_run:
            result = {"status": "DRY_RUN", "reports": f"{report}", **plan.build_document()}
            write_json(report / paths.REPORT_FILE, result, owner=owner)
            return result

        journal = report / paths.CARD_JOURNAL_FILE
        try:
            return self.initialize(plan, journal_path=journal, journal_owner=owner, confirm=confirm)

        except BaseException as exc:

            if not journal.exists():
                try:
                    write_json(
                        report / paths.FAILURE_FILE,
                        {"error": f"{exc}" or type(exc).__name__, "card_data_changed": False},
                        owner=owner,
                    )

                except OSError:
                    logger.error("Could not save card initialization failure report in %s", report)

            raise

    def initialize(
        self, plan: CardPlan, *, journal_path: Path, confirm: Callable[[CardPlan], bool],
        journal_owner: tuple[int, int] | None = None,
    ) -> dict[str, Any]:
        content = read_instructions()
        self.backend.validate_initialization(plan.target, journal_path)
        require(confirm(plan), "Confirmation declined; no card data was changed")
        validate_same_card(plan.target, self.backend.inspect(plan.target.device))

        with CardJournal(journal_path, owner=journal_owner) as journal:
            started = False
            try:
                journal.record_event("begin", plan=plan.build_document())
                mounted = self.backend.list_mounted_paths(plan.target)
                journal.record_event("unmount_intent", mountpoints=list(mounted), write_may_have_started=False)
                unmounted = self.backend.unmount(plan.target)
                journal.record_event("unmount_complete", mountpoints=list(unmounted))

                with self.backend.open(plan.target) as fd:

                    with self.backend.open_volume(fd, plan, journal) as volume:
                        journal.record_event("partition_intent", write_may_have_started=True)
                        started = True
                        self.backend.partition(fd, plan)
                        journal.record_event("partition_complete")
                        journal.record_event("format_intent")
                        volume.format()
                        journal.record_event("format_complete")
                        digest = volume.copy_instructions(content, journal)
                        journal.record_event("instructions_complete", sha256=digest, bytes=len(content))

                    self.backend.finish(fd, plan)

                result = {
                    "status": "CARD_INITIALIZED", "device": plan.target.device, "diskseq": plan.target.diskseq,
                    "synchronized": True, "unmounted": True, "safe_to_remove": True,
                    "automatically_unmounted": list(unmounted),
                    "reconnect_required": True, "instructions_sha256": digest,
                    "full_card_readback_verified": False, "hardware_boot_verified": False,
                    "journal": f"{journal_path}",
                }
                journal.record_event("complete", **result)
                return result

            except BaseException as exc:
                try:
                    journal.record_event(
                        "failed", error=type(exc).__name__, detail=f"{exc}", write_may_have_started=started,
                        safe_to_remove=False,
                    )

                except OSError:
                    logger.exception(
                        "Could not persist failure; journal without complete means an incomplete operation"
                    )

                logger.error("Card initialization incomplete. Keep %s; no automatic retry or rollback.", journal_path)
                raise
