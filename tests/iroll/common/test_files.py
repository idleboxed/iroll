"""Public report creation and ownership; synthetic paths only.

SPDX-License-Identifier: BSD-3-Clause
"""

import hashlib
import json
import os
import stat
from collections.abc import Callable
from pathlib import Path

import pytest

from iroll.common import files
from iroll.common.errors import BuildError
from iroll.common.files import fingerprint_paths


@pytest.mark.parametrize("content,expected", [
    pytest.param(b"{\"version\": 1}", {"version": 1}, id="valid-cache"),
    pytest.param(b"{", None, id="incomplete-cache"),
    pytest.param(b"\xff", None, id="invalid-encoding"),
    pytest.param(b"[" * 20_000 + b"]" * 20_000, None, id="excessive-nesting"),
])
def test_cache_json_treats_malformed_documents_as_a_miss(
    tmp_path: Path, content: bytes, expected: object,
) -> None:
    cache = tmp_path / "cache.json"
    cache.write_bytes(content)

    assert files.read_cached_json(cache) == expected


def test_report_directory_and_json_keep_original_owner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    directory_owners = []
    file_owners = []
    monkeypatch.setattr(files.os, "chown", lambda path, uid, gid: directory_owners.append((Path(path), uid, gid)))
    monkeypatch.setattr(files.os, "fchown", lambda descriptor, uid, gid: file_owners.append((uid, gid)))

    report = files.create_report(tmp_path / "logs/card-init", owner=(1234, 5678))
    files.write_json(report / "result.json", {"status": "synthetic"}, owner=(1234, 5678))

    assert directory_owners == [
        (tmp_path / "logs", 1234, 5678),
        (tmp_path / "logs/card-init", 1234, 5678),
        (report, 1234, 5678),
    ]
    assert file_owners == [(1234, 5678)]
    assert json.loads((report / "result.json").read_text()) == {"status": "synthetic"}


def test_report_preserves_existing_parent_owner_and_uses_collision_suffix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    owners = []
    monkeypatch.setattr(files.os, "chown", lambda path, uid, gid: owners.append(Path(path)))
    monkeypatch.setattr(files, "time", lambda: 1791086400)

    first = files.create_report(tmp_path, owner=(1234, 5678))
    second = files.create_report(tmp_path, owner=(1234, 5678))

    assert first.name == "2026-10-04T04:00:00Z"
    assert second.name == "2026-10-04T04:00:00Z_01"
    assert owners == [first, second]
    assert first.stat().st_mode & 0o777 == second.stat().st_mode & 0o777 == 0o700


def test_report_refuses_symlink_ancestor_before_creating_or_changing_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / "logs"
    root.symlink_to(outside, target_is_directory=True)
    owners = []
    monkeypatch.setattr(files.os, "chown", lambda path, uid, gid: owners.append(path))

    with pytest.raises(BuildError, match="Symlink path is not allowed"):
        files.create_report(root / "card-init", owner=(1234, 5678))

    assert not list(outside.iterdir())
    assert not owners


def test_resource_staging_writes_exact_bytes_modes_and_checksums(tmp_path: Path) -> None:
    resources = {"profiles/pad.cfg": b"synthetic pad\n", "shader.glsl": b"synthetic shader\n"}

    staged = files.stage_resources(tmp_path, resources, resource_kind="synthetic")

    assert staged == {name: hashlib.sha256(data).hexdigest() for name, data in resources.items()}

    for name, data in resources.items():
        path = tmp_path / name
        assert path.read_bytes() == data
        assert stat.S_IMODE(path.stat().st_mode) == 0o644


def test_empty_resource_staging_creates_nothing(tmp_path: Path) -> None:
    staged = files.stage_resources(tmp_path, {}, resource_kind="synthetic")

    assert staged == {}
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("resource_kind", ["joypad profile", "shader"])
def test_resource_staging_checks_all_conflicts_before_any_write(tmp_path: Path, resource_kind: str) -> None:
    existing = tmp_path / "existing.cfg"
    existing.write_bytes(b"unchanged")
    resources = {"new/nested.cfg": b"new", "existing.cfg": b"replacement"}

    with pytest.raises(BuildError, match=f"Duplicate {resource_kind} payload file: existing.cfg"):
        files.stage_resources(tmp_path, resources, resource_kind=resource_kind)

    assert existing.read_bytes() == b"unchanged"
    assert not (tmp_path / "new").exists()


def test_resource_staging_rejects_symlink_ancestor_before_any_write(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "linked").symlink_to(outside, target_is_directory=True)
    resources = {"new/nested.cfg": b"new", "linked/unsafe.cfg": b"unsafe"}

    with pytest.raises(BuildError, match="Symlink path is not allowed"):
        files.stage_resources(tmp_path, resources, resource_kind="synthetic")

    assert not (tmp_path / "new").exists()
    assert not list(outside.iterdir())


@pytest.mark.parametrize("name", ["../unsafe.cfg", "/absolute.cfg", "nested//unsafe.cfg"])
def test_resource_staging_rejects_unsafe_relative_paths_before_any_write(tmp_path: Path, name: str) -> None:
    resources = {"new/nested.cfg": b"new", name: b"unsafe"}

    with pytest.raises(BuildError, match="Unsafe relative path"):
        files.stage_resources(tmp_path, resources, resource_kind="synthetic")

    assert not list(tmp_path.iterdir())


def test_resource_staging_rejects_file_parent_before_writing_any_resource(tmp_path: Path) -> None:
    blocked = tmp_path / "blocked"
    blocked.write_bytes(b"unchanged")
    resources = {"new/profile.cfg": b"new", "blocked/profile.cfg": b"unsafe"}

    with pytest.raises(BuildError, match="File/directory conflict"):
        files.stage_resources(tmp_path, resources, resource_kind="synthetic")

    assert blocked.read_bytes() == b"unchanged"
    assert not (tmp_path / "new").exists()


def test_fingerprint_is_stable_after_reads_and_independent_of_root_order(tmp_path: Path) -> None:
    first, second = tmp_path / "first", tmp_path / "second"
    first.write_bytes(b"compiler")
    second.write_bytes(b"header")
    before = fingerprint_paths({first: False, second: False})

    first.read_bytes()
    second.read_bytes()

    assert fingerprint_paths({second: False, first: False}) == before

@pytest.mark.parametrize("damage", [
    pytest.param(lambda sdk, header, metadata: (
        header.write_bytes(b"new"), os.utime(header, ns=(metadata.st_atime_ns, metadata.st_mtime_ns)),
    ), id="rewrite"),
    pytest.param(lambda sdk, header, metadata: (header.parent / "new.h").write_bytes(b"new"), id="add"),
    pytest.param(lambda sdk, header, metadata: header.unlink(), id="delete"),
    pytest.param(lambda sdk, header, metadata: (
        (sdk / "include").unlink(), (sdk / "include").symlink_to(sdk.parent / "missing", target_is_directory=True),
    ), id="symlink-target"),
])
def test_fingerprint_detects_changes_including_preserved_mtime(
    tmp_path: Path, damage: Callable[[Path, Path, os.stat_result], object],
) -> None:
    sdk = tmp_path / "sdk"
    sdk.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    header = outside / "header.h"
    header.write_bytes(b"old")
    (sdk / "include").symlink_to(outside, target_is_directory=True)
    # A directory cycle must not recurse forever.
    (outside / "cycle").symlink_to(sdk, target_is_directory=True)
    metadata = header.stat()
    before = fingerprint_paths({sdk: True})

    damage(sdk, header, metadata)

    assert fingerprint_paths({sdk: True}) != before
