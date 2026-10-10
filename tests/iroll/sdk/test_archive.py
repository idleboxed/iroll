"""Confined SDK extraction; synthetic archives only. SPDX-License-Identifier: BSD-3-Clause."""

import tarfile
from collections.abc import Callable
from pathlib import Path

import pytest

from iroll.common.errors import BuildError
from iroll.components.registry import Source
from iroll.sdk.archive import extract_sdk_archive, validate_sdk_links


def test_sdk_extracts_internal_soft_and_hard_links_without_copying_hardlink(
    archive: Callable[..., tuple[Path, Source]], tmp_path: Path,
) -> None:
    soft = tarfile.TarInfo()
    soft.type = tarfile.SYMTYPE
    soft.linkname = "../data/file"
    hard = tarfile.TarInfo()
    hard.type = tarfile.LNKTYPE
    hard.linkname = "root/data/file"
    path, source = archive({"root/data/file": b"content", "root/bin/soft": soft, "root/hard": hard})
    destination = tmp_path / "sdk"

    extract_sdk_archive(path, destination, source)

    assert (destination / "bin/soft").read_bytes() == b"content"
    assert (destination / "bin/soft").is_symlink()
    assert (destination / "hard").stat().st_ino == (destination / "data/file").stat().st_ino


@pytest.mark.parametrize("link", ["/tmp/outside", "../../outside", "bad\\link"])
@pytest.mark.parametrize("kind", [tarfile.SYMTYPE, tarfile.LNKTYPE])
def test_sdk_archive_rejects_escaping_links_before_extraction(
    archive: Callable[..., tuple[Path, Source]], tmp_path: Path, link: str, kind: bytes,
) -> None:
    member = tarfile.TarInfo()
    member.type = kind
    member.linkname = link
    path, source = archive({"root/link": member})
    destination = tmp_path / "sdk"

    with pytest.raises(BuildError, match="Unsafe|escapes|regular"):
        extract_sdk_archive(path, destination, source)

    assert not destination.exists()


def test_sdk_archive_refuses_file_written_through_internal_symlink(
    archive: Callable[..., tuple[Path, Source]], tmp_path: Path,
) -> None:
    member = tarfile.TarInfo()
    member.type = tarfile.SYMTYPE
    member.linkname = "other"
    path, source = archive({"root/link": member, "root/link/file": b"no"})
    destination = tmp_path / "sdk"

    with pytest.raises(BuildError, match="non-directory"):
        extract_sdk_archive(path, destination, source)

    assert not destination.exists()


def test_sdk_archive_cleans_cyclic_links(archive: Callable[..., tuple[Path, Source]], tmp_path: Path) -> None:
    member = tarfile.TarInfo()
    member.type = tarfile.SYMTYPE
    member.linkname = "link"
    path, source = archive({"root/link": member})
    destination = tmp_path / "sdk"

    with pytest.raises(BuildError, match="Invalid SDK link"):
        extract_sdk_archive(path, destination, source)

    assert not destination.exists()


@pytest.mark.parametrize("kind", [tarfile.FIFOTYPE, tarfile.CHRTYPE, tarfile.BLKTYPE])
def test_sdk_archive_rejects_special_files(
    archive: Callable[..., tuple[Path, Source]], tmp_path: Path, kind: bytes,
) -> None:
    member = tarfile.TarInfo()
    member.type = kind
    path, source = archive({"root/special": member})

    with pytest.raises(BuildError, match="Unsafe SDK archive member"):
        extract_sdk_archive(path, tmp_path / "sdk", source)


def test_sdk_archive_never_replaces_existing_destination(
    archive: Callable[..., tuple[Path, Source]], tmp_path: Path,
) -> None:
    path, source = archive()
    destination = tmp_path / "sdk"
    destination.mkdir()
    (destination / "keep").write_bytes(b"original")

    with pytest.raises(BuildError, match="already exists"):
        extract_sdk_archive(path, destination, source)

    assert (destination / "keep").read_bytes() == b"original"


def test_sdk_archive_rejects_changed_input_before_creating_destination(
    archive: Callable[..., tuple[Path, Source]], tmp_path: Path,
) -> None:
    path, source = archive()
    path.write_bytes(b"changed archive")

    with pytest.raises(BuildError, match="SHA-256 mismatch"):
        extract_sdk_archive(path, tmp_path / "sdk", source)

    assert not (tmp_path / "sdk").exists()


def test_sdk_link_validation_allows_dangling_internal_link_but_not_external(tmp_path: Path) -> None:
    root = tmp_path / "sdk"
    root.mkdir()
    link = root / "link"
    link.symlink_to("missing")

    validate_sdk_links(root)

    link.unlink()
    link.symlink_to("../outside")

    with pytest.raises(BuildError, match="escapes its root"):
        validate_sdk_links(root)


@pytest.mark.parametrize("name", ["/root/file", "root/../file", "root/back\\slash", "root/./file"])
def test_sdk_archive_rejects_unsafe_or_duplicate_paths(
    archive: Callable[..., tuple[Path, Source]], tmp_path: Path, name: str,
) -> None:
    path, source = archive({"root/file": b"original", name: b"conflict"})

    with pytest.raises(BuildError, match="Unsafe|Duplicate"):
        extract_sdk_archive(path, tmp_path / "sdk", source)

    assert not (tmp_path / "sdk").exists()


def test_sdk_archive_rejects_hard_link_to_symlink(
    archive: Callable[..., tuple[Path, Source]], tmp_path: Path,
) -> None:
    soft = tarfile.TarInfo()
    soft.type = tarfile.SYMTYPE
    soft.linkname = "file"
    hard = tarfile.TarInfo()
    hard.type = tarfile.LNKTYPE
    hard.linkname = "root/soft"
    path, source = archive({"root/file": b"data", "root/soft": soft, "root/hard": hard})

    with pytest.raises(BuildError, match="regular archive member"):
        extract_sdk_archive(path, tmp_path / "sdk", source)

    assert not (tmp_path / "sdk").exists()
