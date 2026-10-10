"""Durable off-card journals using synthetic metadata. SPDX-License-Identifier: BSD-3-Clause."""

import json
import os
from contextlib import AbstractContextManager, nullcontext
from dataclasses import replace
from pathlib import Path

import pytest

from iroll.card.journal import CardJournal, validate_journal_parent, validate_journal_root
from iroll.card.layout import CardError, CardTarget


def test_card_journal_assigns_sudo_owner_before_writing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    owners = []
    monkeypatch.setattr("iroll.card.journal.os.fchown", lambda fd, uid, gid: owners.append((uid, gid)))
    journal = tmp_path / "operation.jsonl"

    with CardJournal(journal, owner=(1234, 5678)) as stream:
        stream.record_event("synthetic")

    assert owners == [(1234, 5678)]
    assert json.loads(journal.read_text())["phase"] == "synthetic"

@pytest.mark.parametrize("filesystem,expected", [
    pytest.param("tmpfs", pytest.raises(CardError, match="durable"), id="tmpfs"),
    pytest.param("ramfs", pytest.raises(CardError, match="durable"), id="ramfs"),
    pytest.param("ext4", nullcontext(), id="ext4"),
])
def test_journal_requires_durable_off_card_parent(
    target: CardTarget, tmp_path: Path, filesystem: str, expected: AbstractContextManager[object],
) -> None:
    number = f"{os.major(tmp_path.stat().st_dev)}:{os.minor(tmp_path.stat().st_dev)}"
    mounts = f"1 0 {number} / / rw - {filesystem} synthetic rw\n"

    with expected:
        validate_journal_parent(tmp_path / "journal", target, mounts)

def test_journal_root_checks_nearest_existing_parent_before_creation(target: CardTarget, tmp_path: Path) -> None:
    root = tmp_path / "not-created/logs/card-init"
    number = f"{os.major(tmp_path.stat().st_dev)}:{os.minor(tmp_path.stat().st_dev)}"
    mounts = f"1 0 {number} / / rw - ext4 synthetic rw\n"

    validate_journal_root(root, target, mounts)

    assert not root.exists()

    with pytest.raises(CardError, match="outside the card"):
        validate_journal_root(root, replace(target, device_numbers=(*target.device_numbers, number)), mounts)

def test_journal_rejects_target_filesystem_and_relative_path(target: CardTarget, tmp_path: Path) -> None:
    number = f"{os.major(tmp_path.stat().st_dev)}:{os.minor(tmp_path.stat().st_dev)}"
    target = replace(target, device_numbers=(*target.device_numbers, number))

    with pytest.raises(CardError, match="outside the card"):
        validate_journal_parent(tmp_path / "journal", target, "")

    with pytest.raises(CardError, match="absolute"):
        validate_journal_parent(Path("journal"), target, "")


def test_journal_rejects_malformed_mountinfo_before_creating_a_file(target: CardTarget, tmp_path: Path) -> None:
    journal = tmp_path / "operation.jsonl"

    with pytest.raises(CardError, match="Invalid Linux mountinfo"):
        validate_journal_parent(journal, target, "incomplete")

    assert not journal.exists()
