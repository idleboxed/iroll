"""Card orchestration with a synthetic backend; never opens a real device.

SPDX-License-Identifier: BSD-3-Clause
"""

import json
import os
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from iroll.card import service as card
from iroll.card.service import CardInitializer, read_instructions
from iroll.card.journal import CardJournal
from iroll.card.layout import CardError, CardPlan


def test_plan_has_one_full_size_partition_without_side_effects(backend: Any, tmp_path: Path) -> None:
    plan = CardInitializer(backend).create_plan("/dev/sdz")

    assert plan.partition_sectors == plan.target.size // 512 - 34 - 2048 + 1
    assert plan.build_document()["files"] == ["INSTRUCTIONS.md"]
    assert plan.build_document()["directories"] == ["games/states", "games/saves"]
    assert plan.build_document()["all_existing_data_will_be_lost"]
    assert plan.build_document()["partition"]["label"] == "idlebox"
    assert "name=\"idlebox\"" in plan.build_sfdisk_input()
    assert backend.calls == ["inspect"]
    assert not list(tmp_path.iterdir())


def test_initialization_synchronizes_then_reports_complete(backend: Any, tmp_path: Path) -> None:
    service = CardInitializer(backend)
    plan = service.create_plan("/dev/sdz")
    journal = tmp_path / "operation.jsonl"

    result = service.initialize(plan, journal_path=journal, confirm=lambda proposed: proposed == plan)

    assert result["status"] == "CARD_INITIALIZED"
    assert result["safe_to_remove"] and result["synchronized"] and result["reconnect_required"]
    assert result["automatically_unmounted"] == []
    assert not result["full_card_readback_verified"] and not result["hardware_boot_verified"]
    assert backend.content == read_instructions()
    assert backend.calls == [
        "inspect", "preflight", "inspect", "mounted_paths", "unmount",
        "open", "volume", "partition", "format", "copy",
        "release_volume", "finish", "close",
    ]
    events = [json.loads(line) for line in journal.read_text().splitlines()]
    assert events[0]["plan"] == plan.build_document()
    assert [event["phase"] for event in events[1:3]] == ["unmount_intent", "unmount_complete"]
    assert events[-1]["phase"] == "complete"
    assert journal.stat().st_mode & 0o777 == 0o600
    assert list(tmp_path.iterdir()) == [journal]


@pytest.mark.parametrize("accepted,failure,sequence,message", [
    pytest.param(False, None, 42, "declined", id="declined"),
    pytest.param(True, ("preflight", CardError("unavailable")), 42, "unavailable", id="preflight"),
    pytest.param(True, None, 43, "changed", id="changed"),
])
def test_refusal_never_opens_writable_device(
    backend: Any, tmp_path: Path, accepted: bool, failure: tuple[str, BaseException] | None,
    sequence: int, message: str,
) -> None:
    service = CardInitializer(backend)
    plan = service.create_plan("/dev/sdz")
    backend.failure = failure
    backend.target = replace(backend.target, diskseq=sequence)

    with pytest.raises(CardError, match=message):
        service.initialize(plan, journal_path=tmp_path / "operation.jsonl", confirm=lambda proposed: accepted)

    assert "open" not in backend.calls
    assert not list(tmp_path.iterdir())


def test_unmount_failure_is_journalled_and_stops_before_open(backend: Any, tmp_path: Path) -> None:
    backend.target = replace(backend.target, mountpoints=("/media/card",))
    backend.failure = ("unmount", CardError("synthetic unmount failure"))
    service = CardInitializer(backend)
    plan = service.create_plan("/dev/sdz")
    journal = tmp_path / "operation.jsonl"

    with pytest.raises(CardError, match="unmount failure"):
        service.initialize(plan, journal_path=journal, confirm=lambda proposed: True)

    events = [json.loads(line) for line in journal.read_text().splitlines()]
    assert events[-1]["phase"] == "failed"
    assert events[-1]["write_may_have_started"] is False
    assert "open" not in backend.calls
    assert "partition" not in backend.calls


@pytest.mark.parametrize("phase", ["volume", "partition", "format", "copy", "release_volume", "finish"])
@pytest.mark.parametrize("exception,message", [
    pytest.param(CardError("synthetic failure"), "synthetic failure", id="failure"),
    pytest.param(KeyboardInterrupt(), "^$", id="cancellation"),
])
def test_failure_or_cancellation_preserves_journal_without_retry(
    backend: Any, tmp_path: Path, phase: str, exception: BaseException, message: str,
) -> None:
    service = CardInitializer(backend)
    plan = service.create_plan("/dev/sdz")
    backend.failure = (phase, exception)
    path = tmp_path / "operation.jsonl"

    with pytest.raises(type(exception), match=message):
        service.initialize(plan, journal_path=path, confirm=lambda proposed: True)

    events = [json.loads(line) for line in path.read_text().splitlines()]
    assert events[-1]["phase"] == "failed"
    assert events[-1]["write_may_have_started"] == (phase != "volume")
    assert not events[-1]["safe_to_remove"]
    assert not any(event["phase"] == "complete" for event in events)
    assert backend.calls.count(phase) == 1
    assert backend.calls[-1] == "close"


def test_journal_sync_failure_before_intent_prevents_partition(
    backend: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = CardJournal.record_event

    def fail(self: CardJournal, phase: str, **fields: object) -> None:

        if phase == "partition_intent":
            raise OSError("synthetic journal full")

        original(self, phase, **fields)

    monkeypatch.setattr(CardJournal, "record_event", fail)
    service = CardInitializer(backend)
    plan = service.create_plan("/dev/sdz")

    with pytest.raises(OSError, match="journal full"):
        service.initialize(plan, journal_path=tmp_path / "journal", confirm=lambda proposed: True)

    assert "partition" not in backend.calls
    assert backend.calls[-2:] == ["release_volume", "close"]


def test_existing_journal_is_not_overwritten(backend: Any, tmp_path: Path) -> None:
    path = tmp_path / "journal"
    path.write_bytes(b"previous operation\n")
    service = CardInitializer(backend)
    plan = service.create_plan("/dev/sdz")

    with pytest.raises(FileExistsError, match="File exists"):
        service.initialize(plan, journal_path=path, confirm=lambda proposed: True)

    assert "partition" not in backend.calls
    assert "open" not in backend.calls
    assert path.read_bytes() == b"previous operation\n"


def test_packaged_guide_describes_current_layout_and_limits() -> None:
    content = read_instructions().decode("utf-8")

    for value in ("system/", "iman.json", "games/", "catalogue.json", "images/", "thumbs/", "prefs.json", "states/"):
        assert value in content

    assert "empty `games/states/` and `games/saves/`" in content
    assert "labelled `idlebox`" in content
    assert "not a secure erase" in content
    assert "Read-only mounting" in content
    assert content.endswith("\n")


def test_missing_packaged_guide_stops_before_inspection(backend: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    def missing() -> bytes:
        raise CardError("Missing packaged guide")

    monkeypatch.setattr(card, "read_instructions", missing)

    with pytest.raises(CardError, match="Missing"):
        CardInitializer(backend).create_plan("/dev/sdz")

    assert not backend.calls


@pytest.mark.parametrize("storage", ["card", "tmpfs", "ramfs"])
def test_run_refuses_unsafe_report_storage_before_mkdir_or_writable_open(
    backend: Any, tmp_path: Path, storage: str,
) -> None:
    device = tmp_path.stat().st_dev
    number = f"{os.major(device)}:{os.minor(device)}"
    filesystem = "ext4" if storage == "card" else storage
    backend.mountinfo = f"1 0 {number} / / rw - {filesystem} synthetic rw\n"
    root = tmp_path / "not-created/logs"

    if storage == "card":
        backend.target = replace(backend.target, device_numbers=(*backend.target.device_numbers, number))

    message = "outside the card" if storage == "card" else "durable PC storage"

    with pytest.raises(CardError, match=message):
        CardInitializer(backend).run("/dev/sdz", logs_root=root, dry_run=False, confirm=lambda proposed: True)

    assert backend.calls == ["inspect"]
    assert not list(tmp_path.iterdir())


def test_run_dry_run_reports_original_owner_without_confirmation_or_mutation(
    backend: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory_owners = []
    file_owners = []
    monkeypatch.setattr(
        os, "chown", lambda path, uid, gid: directory_owners.append((Path(path), uid, gid)),
    )
    monkeypatch.setattr(os, "fchown", lambda descriptor, uid, gid: file_owners.append((uid, gid)))

    def forbidden(proposed: CardPlan) -> bool:
        pytest.fail("Dry-run must not ask for destructive confirmation")

    result = CardInitializer(backend).run(
        "/dev/sdz", logs_root=tmp_path / "logs/card-init", dry_run=True, confirm=forbidden, owner=(1234, 5678),
    )

    report = Path(result["reports"])
    assert result["status"] == "DRY_RUN"
    assert json.loads((report / "result.json").read_text()) == result
    assert list(report.iterdir()) == [report / "result.json"]
    assert backend.calls == ["inspect"]
    assert directory_owners == [
        (tmp_path / "logs", 1234, 5678),
        (tmp_path / "logs/card-init", 1234, 5678),
        (report, 1234, 5678),
    ]
    assert file_owners == [(1234, 5678)]


def test_run_success_records_complete_only_after_volume_fd_and_sync_finish(backend: Any, tmp_path: Path) -> None:
    result = CardInitializer(backend).run(
        "/dev/sdz", logs_root=tmp_path / "logs", dry_run=False, confirm=lambda proposed: True,
    )

    events = [json.loads(line) for line in Path(result["journal"]).read_text().splitlines()]
    assert result["safe_to_remove"] is True
    assert backend.calls[-3:] == ["release_volume", "finish", "close"]
    assert events[-1]["phase"] == "complete"
    assert events[-1]["synchronized"] is True


@pytest.mark.parametrize("exception", [CardError("synthetic preflight failure"), KeyboardInterrupt()])
def test_run_early_failure_keeps_original_exception_and_owned_failure_report(
    backend: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, exception: BaseException,
) -> None:
    backend.failure = ("preflight", exception)
    file_owners = []
    monkeypatch.setattr(os, "chown", lambda path, uid, gid: None)
    monkeypatch.setattr(os, "fchown", lambda descriptor, uid, gid: file_owners.append((uid, gid)))

    with pytest.raises(type(exception), match="synthetic preflight failure|^$") as caught:
        CardInitializer(backend).run(
            "/dev/sdz", logs_root=tmp_path / "logs", dry_run=False,
            confirm=lambda proposed: True, owner=(1234, 5678),
        )

    assert caught.value is exception
    failure, = (tmp_path / "logs").glob("*/failure.json")
    assert json.loads(failure.read_text()) == {
        "error": f"{exception}" or type(exception).__name__, "card_data_changed": False,
    }
    assert list(failure.parent.iterdir()) == [failure]
    assert backend.calls == ["inspect", "preflight"]
    assert file_owners == [(1234, 5678)]


@pytest.mark.parametrize("exception", [CardError("synthetic inspection failure"), KeyboardInterrupt()])
def test_run_failure_before_report_validation_creates_no_files(
    backend: Any, tmp_path: Path, exception: BaseException,
) -> None:
    backend.failure = ("inspect", exception)

    with pytest.raises(type(exception), match="synthetic inspection failure|^$") as caught:
        CardInitializer(backend).run(
            "/dev/sdz", logs_root=tmp_path / "logs", dry_run=False, confirm=lambda proposed: True,
        )

    assert caught.value is exception
    assert not list(tmp_path.iterdir())
    assert backend.calls == ["inspect"]


@pytest.mark.parametrize("exception", [CardError("synthetic write failure"), KeyboardInterrupt()])
def test_run_failure_after_journal_preserves_failed_state_without_early_report_or_retry(
    backend: Any, tmp_path: Path, exception: BaseException,
) -> None:
    backend.failure = ("partition", exception)

    with pytest.raises(type(exception), match="synthetic write failure|^$") as caught:
        CardInitializer(backend).run(
            "/dev/sdz", logs_root=tmp_path / "logs", dry_run=False, confirm=lambda proposed: True,
        )

    assert caught.value is exception
    journal, = (tmp_path / "logs").glob("*/journal.jsonl")
    events = [json.loads(line) for line in journal.read_text().splitlines()]
    assert events[-1]["phase"] == "failed"
    assert events[-1]["write_may_have_started"] is True
    assert events[-1]["safe_to_remove"] is False
    assert not any(event["phase"] == "complete" for event in events)
    assert list(journal.parent.iterdir()) == [journal]
    assert backend.calls.count("partition") == 1
    assert backend.calls[-2:] == ["release_volume", "close"]


def test_run_failure_report_error_does_not_mask_original_exception(
    backend: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    original = CardError("synthetic preflight failure")
    backend.failure = ("preflight", original)

    def unavailable(path: Path, value: object, *, owner: tuple[int, int] | None = None) -> None:
        raise OSError("synthetic report storage full")

    monkeypatch.setattr(card, "write_json", unavailable)

    with pytest.raises(CardError, match="synthetic preflight failure") as caught:
        CardInitializer(backend).run(
            "/dev/sdz", logs_root=tmp_path / "logs", dry_run=False, confirm=lambda proposed: True,
        )

    assert caught.value is original
    assert "Could not save card initialization failure report" in caplog.text
    assert backend.calls == ["inspect", "preflight"]
