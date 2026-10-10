"""Pinned archive acquisition and extraction boundaries. SPDX-License-Identifier: BSD-3-Clause."""

import tarfile
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pytest

from iroll.components import sources
from iroll.common.errors import BuildError
from iroll.components.registry import Source
from iroll.components.sources import SourceDownloads, extract, prepare_source


def test_offline_local_archive_is_verified_without_copy_or_download(
    tmp_path: Path, archive: Callable[..., tuple[Path, Source]],
) -> None:
    path, source = archive()
    downloads = SourceDownloads(tmp_path / "downloads", offline=True, archives=tmp_path)

    result = downloads.obtain(source)
    extract(result, tmp_path / "extracted", source)

    assert result == path
    assert not downloads.directory.exists()
    assert (tmp_path / "extracted/Makefile").read_bytes() == b"synthetic"


def test_download_is_published_only_after_sha_verification(
    tmp_path: Path, archive: Callable[..., tuple[Path, Source]],
) -> None:
    path, source = archive()
    calls = []

    def fetch(url: str, target: Path) -> None:
        calls.append(url)
        target.write_bytes(path.read_bytes())

    downloads = SourceDownloads(tmp_path / "downloads", fetch=fetch)

    first = downloads.obtain(source)
    second = downloads.obtain(source)

    assert calls == [source.url]
    assert first == second == downloads.directory / source.archive
    assert first.read_bytes() == path.read_bytes()
    assert list(downloads.directory.iterdir()) == [first]


@pytest.mark.parametrize("failure", ["checksum", "io", "cancel"])
def test_failed_download_leaves_no_partial_archive(
    tmp_path: Path, archive: Callable[..., tuple[Path, Source]], failure: str,
) -> None:
    _, source = archive()

    def fetch(url: str, target: Path) -> None:
        target.write_bytes(b"partial")

        if failure == "io":
            raise OSError("Synthetic download failure")

        if failure == "cancel":
            raise KeyboardInterrupt

    downloads = SourceDownloads(tmp_path / "downloads", fetch=fetch)
    error = {"checksum": BuildError, "io": OSError, "cancel": KeyboardInterrupt}[failure]

    with pytest.raises(error, match="SHA-256 mismatch|Synthetic download failure|^$"):
        downloads.obtain(source)

    assert not list(downloads.directory.iterdir())


def test_wrong_local_sha_does_not_fall_back_to_network_or_overwrite(
    tmp_path: Path, archive: Callable[..., tuple[Path, Source]],
) -> None:
    path, source = archive()
    downloads = SourceDownloads(tmp_path / "downloads", archives=tmp_path)

    with pytest.raises(BuildError, match="SHA-256 mismatch"):
        downloads.obtain(replace(source, sha256="0" * 64))

    assert path.exists()
    assert not downloads.directory.exists()


def test_offline_missing_source_does_not_create_downloads(
    tmp_path: Path, archive: Callable[..., tuple[Path, Source]],
) -> None:
    _, source = archive()
    downloads = SourceDownloads(tmp_path / "missing", offline=True)

    with pytest.raises(BuildError, match="Offline source archive missing"):
        downloads.obtain(source)

    assert not downloads.directory.exists()


@pytest.mark.parametrize("name", ["../escape", "/absolute", "nested/source.tar.gz", "bad\\name.tar.gz"])
def test_archive_name_is_validated_before_creating_or_downloading(
    tmp_path: Path, archive: Callable[..., tuple[Path, Source]], name: str,
) -> None:
    _, source = archive()
    calls = []
    downloads = SourceDownloads(tmp_path / "downloads", fetch=lambda url, target: calls.append(url))

    with pytest.raises(BuildError, match="relative path|single filename"):
        downloads.obtain(replace(source, archive=name))

    assert not calls
    assert not downloads.directory.exists()


def test_download_directory_symlink_is_refused_before_creating_a_child(
    tmp_path: Path, archive: Callable[..., tuple[Path, Source]],
) -> None:
    _, source = archive()
    outside = tmp_path / "outside"
    outside.mkdir()
    link = tmp_path / "linked"
    link.symlink_to(outside, target_is_directory=True)
    calls = []
    downloads = SourceDownloads(link / "downloads", fetch=lambda url, target: calls.append(url))

    with pytest.raises(BuildError, match="Symlink path"):
        downloads.obtain(source)

    assert not calls
    assert not list(outside.iterdir())


@pytest.mark.parametrize("name", ["../escape", "root/../../escape", "/absolute", "root\\escape"])
def test_extraction_rejects_escaping_names_before_creating_output(
    tmp_path: Path, archive: Callable[..., tuple[Path, Source]], name: str,
) -> None:
    path, source = archive({name: b"unsafe"})

    with pytest.raises(BuildError, match="Unsafe archive member"):
        extract(path, tmp_path / "output", source)

    assert not (tmp_path / "output").exists()


@pytest.mark.parametrize("kind", [tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.FIFOTYPE, tarfile.CHRTYPE])
def test_extraction_refuses_links_and_special_files(
    tmp_path: Path, archive: Callable[..., tuple[Path, Source]], kind: bytes,
) -> None:
    info = tarfile.TarInfo()
    info.type = kind
    info.linkname = "/outside"
    path, source = archive({"root/link": info})

    with pytest.raises(BuildError, match="Unsafe archive member"):
        extract(path, tmp_path / "output", source)

    assert not (tmp_path / "output").exists()


def test_existing_tree_is_never_reused(tmp_path: Path, archive: Callable[..., tuple[Path, Source]]) -> None:
    path, source = archive()
    output = tmp_path / "output"
    output.mkdir()
    (output / "keep").write_bytes(b"local modifications")

    with pytest.raises(BuildError, match="already exists"):
        extract(path, output, source)

    assert (output / "keep").read_bytes() == b"local modifications"


def test_verified_source_tree_is_read_only_and_reused_without_writes(
    tmp_path: Path, archive: Callable[..., tuple[Path, Source]],
) -> None:
    # A separate archive with an explicit empty directory exercises directory validation.
    directory = tarfile.TarInfo()
    directory.type = tarfile.DIRTYPE
    path, source = archive({"root/sub/Makefile": b"synthetic", "root/empty": directory})
    output = tmp_path / "src" / source.sha256
    prepare_source(path, output, source)
    file = output / "sub/Makefile"
    before = file.stat()

    prepare_source(path, output, source)

    assert file.read_bytes() == b"synthetic"
    assert not file.stat().st_mode & 0o222
    assert (file.stat().st_ino, file.stat().st_mtime_ns) == (before.st_ino, before.st_mtime_ns)
    assert (output / "empty").is_dir()
    assert set(output.parent.iterdir()) == {output, output.with_name(f"{output.name}.json")}


@pytest.mark.parametrize("damage", [
    pytest.param(lambda file, archive: (
        file.chmod(0o644), file.write_bytes(b"different"), file.chmod(0o444),
    ), id="content"),
    pytest.param(lambda file, archive: (
        file.chmod(0o644), file.write_bytes(b"longer content"), file.chmod(0o444),
    ), id="size"),
    pytest.param(lambda file, archive: file.chmod(0o644), id="writable"),
    pytest.param(lambda file, archive: file.chmod(0o555), id="executable"),
    pytest.param(lambda file, archive: (file.parent / "unexpected.o").write_bytes(b"object"), id="extra"),
    pytest.param(lambda file, archive: file.unlink(), id="missing"),
    pytest.param(lambda file, archive: (file.unlink(), file.symlink_to(archive)), id="symlink"),
])
def test_cached_sources_are_verified_before_reuse(
    tmp_path: Path, archive: Callable[..., tuple[Path, Source]], damage: Callable[[Path, Path], object],
) -> None:
    path, source = archive()
    output = tmp_path / "src"
    prepare_source(path, output, source)
    damage(output / "Makefile", path)

    with pytest.raises((BuildError, FileNotFoundError), match="changed|unexpected|Symlink|No such file|size limit"):
        prepare_source(path, output, source)


def test_cached_sources_still_require_the_pinned_archive_sha(
    tmp_path: Path, archive: Callable[..., tuple[Path, Source]],
) -> None:
    path, source = archive()
    output = tmp_path / "src"
    prepare_source(path, output, source)

    with pytest.raises(BuildError, match="SHA-256 mismatch"):
        prepare_source(path, output, replace(source, sha256="0" * 64))


@pytest.mark.parametrize("error,message", [
    pytest.param(OSError("Synthetic extraction failure"), "Synthetic extraction failure", id="failure"),
    pytest.param(KeyboardInterrupt(), "^$", id="cancellation"),
])
def test_source_preparation_never_publishes_partial_extraction(
    tmp_path: Path, archive: Callable[..., tuple[Path, Source]], monkeypatch: pytest.MonkeyPatch,
    error: BaseException, message: str,
) -> None:
    path, source = archive()
    output = tmp_path / "src" / source.sha256

    def interrupted(archive_path: Path, destination: Path, source: Source) -> None:
        destination.mkdir()
        (destination / "partial").write_bytes(b"partial")

        raise error

    monkeypatch.setattr(sources, "extract", interrupted)

    with pytest.raises(type(error), match=message):
        prepare_source(path, output, source)

    assert not output.exists()
    assert not list(output.parent.iterdir())


def test_new_archive_replaces_source_tree_instead_of_keeping_old_versions(
    tmp_path: Path, archive: Callable[..., tuple[Path, Source]],
) -> None:
    first, first_pin = archive({"root/old.c": b"old"})
    second, second_pin = archive({"root/new.c": b"new"})
    output = tmp_path / "src/component"
    prepare_source(first, output, first_pin)

    prepare_source(second, output, second_pin)

    assert {path.name for path in output.iterdir()} == {"new.c"}
    assert (output / "new.c").read_bytes() == b"new"
    assert {path.name for path in output.parent.iterdir()} == {"component", "component.json"}


@pytest.mark.parametrize("damage", [
    pytest.param(Path.unlink, id="missing"),
    pytest.param(lambda path: path.write_text("partial JSON"), id="broken"),
])
def test_incomplete_source_state_causes_fresh_extraction(
    tmp_path: Path, archive: Callable[..., tuple[Path, Source]], damage: Callable[[Path], object],
) -> None:
    path, pin = archive()
    output = tmp_path / "src/component"
    prepare_source(path, output, pin)
    marker = output.with_name("component.json")
    (output / "partial").write_bytes(b"incomplete")

    damage(marker)

    prepare_source(path, output, pin)

    assert not (output / "partial").exists()
    assert (output / "Makefile").read_bytes() == b"synthetic"


def test_source_marker_symlink_is_rejected_without_touching_external_file(
    tmp_path: Path, archive: Callable[..., tuple[Path, Source]],
) -> None:
    path, pin = archive()
    output = tmp_path / "component"
    outside = tmp_path / "outside"
    outside.write_bytes(b"untouched")
    output.with_name("component.json").symlink_to(outside)

    with pytest.raises(BuildError, match="Symlink"):
        prepare_source(path, output, pin)

    assert outside.read_bytes() == b"untouched"
    assert not output.exists()


def test_pinned_submodules_are_read_only_verified_and_reused(
    tmp_path: Path, archive: Callable[..., tuple[Path, Source]],
) -> None:
    slot = tarfile.TarInfo()
    slot.type = tarfile.DIRTYPE
    path, source = archive({"root/Makefile": b"synthetic", "root/deps/core": slot})
    module, pin = archive({"module/source.c": b"synthetic dependency"})
    output = tmp_path / "src"
    modules = (("deps/core", module, pin),)

    prepare_source(path, output, source, modules=modules)
    dependency = output / "deps/core/source.c"
    original = dependency.stat()
    prepare_source(path, output, source, modules=modules)

    assert dependency.read_bytes() == b"synthetic dependency"
    assert not dependency.stat().st_mode & 0o222
    assert dependency.stat().st_ino == original.st_ino
    assert dependency.stat().st_mtime_ns == original.st_mtime_ns


@pytest.mark.parametrize("damage", [
    pytest.param(lambda file, archive: (
        file.chmod(0o644), file.write_bytes(b"modified"), file.chmod(0o444),
    ), id="content"),
    pytest.param(lambda file, archive: (file.parent / "extra.c").write_bytes(b"extra"), id="extra"),
    pytest.param(lambda file, archive: (file.unlink(), file.symlink_to(archive)), id="symlink"),
])
def test_modified_submodule_is_rejected_before_reuse(
    tmp_path: Path, archive: Callable[..., tuple[Path, Source]], damage: Callable[[Path, Path], object],
) -> None:
    path, source = archive()
    module, pin = archive({"module/source.c": b"original"})
    output = tmp_path / "src"
    modules = (("deps/core", module, pin),)
    prepare_source(path, output, source, modules=modules)
    damage(output / "deps/core/source.c", module)

    with pytest.raises(BuildError, match="changed|unexpected|Symlink"):
        prepare_source(path, output, source, modules=modules)


def test_changed_submodule_pin_replaces_combined_tree(
    tmp_path: Path, archive: Callable[..., tuple[Path, Source]],
) -> None:
    path, source = archive()
    first, first_pin = archive({"module/old.c": b"old"})
    second, second_pin = archive({"module/new.c": b"new"})
    output = tmp_path / "src"
    prepare_source(path, output, source, modules=(("dep", first, first_pin),))

    prepare_source(path, output, source, modules=(("dep", second, second_pin),))

    assert not (output / "dep/old.c").exists()
    assert (output / "dep/new.c").read_bytes() == b"new"
    prepare_source(path, output, source, modules=(("dep", second, second_pin),))


def test_bad_submodule_sha_preserves_previous_tree(
    tmp_path: Path, archive: Callable[..., tuple[Path, Source]],
) -> None:
    path, source = archive()
    module, pin = archive()
    output = tmp_path / "src"
    prepare_source(path, output, source)
    original = (output / "Makefile").stat().st_ino

    with pytest.raises(BuildError, match="SHA-256 mismatch"):
        prepare_source(path, output, source, modules=(("dep", module, replace(pin, sha256="0" * 64)),))

    assert (output / "Makefile").stat().st_ino == original


@pytest.mark.parametrize("directory", ["..", "../outside", "/absolute", ".", "dep/../outside"])
def test_submodule_paths_cannot_escape_or_replace_root(
    tmp_path: Path, archive: Callable[..., tuple[Path, Source]], directory: str,
) -> None:
    path, source = archive()
    module, pin = archive()
    output = tmp_path / "src"

    with pytest.raises(BuildError, match="path|submodule"):
        prepare_source(path, output, source, modules=((directory, module, pin),))

    assert not output.exists()


@pytest.mark.parametrize("second_directory", ["dep", "dep/nested"])
def test_submodule_paths_cannot_overlap(
    tmp_path: Path, archive: Callable[..., tuple[Path, Source]], second_directory: str,
) -> None:
    path, source = archive()
    module, pin = archive()

    with pytest.raises(BuildError, match="Overlapping source submodule"):
        prepare_source(
            path, tmp_path / "src", source,
            modules=(("dep", module, pin), (second_directory, module, pin)),
        )


@pytest.mark.parametrize("member", ["root/dep", "root/dep/original.c"])
def test_submodule_cannot_overwrite_upstream_file_or_nonempty_directory(
    tmp_path: Path, archive: Callable[..., tuple[Path, Source]], member: str,
) -> None:
    path, source = archive({member: b"upstream"})
    module, pin = archive()
    output = tmp_path / "src"

    with pytest.raises(BuildError, match="overwrite upstream files"):
        prepare_source(path, output, source, modules=(("dep", module, pin),))

    assert not output.exists()
