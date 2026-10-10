"""Internal source projections and disposable output staging. SPDX-License-Identifier: BSD-3-Clause."""

import logging
from collections.abc import Callable
from contextlib import AbstractContextManager, nullcontext
from pathlib import Path

import pytest

from iroll.common.errors import BuildError
from iroll.rollout.workspace import BUILD_STATE, reuse_cached_build, mirror_sources, create_staging_tree


def test_source_projection_uses_file_links_and_writable_real_directories(tmp_path: Path) -> None:
    source = tmp_path / "src"
    (source / "sub").mkdir(parents=True)
    (source / "sub/file.c").write_bytes(b"source")
    compiled = tmp_path / "compiled"

    mirror_sources(source, compiled)
    (compiled / "sub/file.o").write_bytes(b"object")

    assert not (compiled / "sub").is_symlink()
    assert (compiled / "sub/file.c").is_symlink()
    assert (compiled / "sub/file.c").resolve() == source / "sub/file.c"
    assert (compiled / "sub/file.c").read_bytes() == b"source"
    assert not (source / "sub/file.o").exists()


def test_recreating_projection_discards_objects_without_following_old_links(tmp_path: Path) -> None:
    source = tmp_path / "src"
    source.mkdir()
    (source / "file.c").write_bytes(b"source")
    compiled = tmp_path / "compiled"
    mirror_sources(source, compiled)
    (compiled / "file.o").write_bytes(b"old object")
    (compiled / "directory-link").symlink_to(source, target_is_directory=True)

    mirror_sources(source, compiled)

    assert {path.name for path in compiled.iterdir()} == {"file.c"}
    assert (source / "file.c").read_bytes() == b"source"


@pytest.mark.parametrize("relation", ["same", "parent", "child"])
def test_projection_rejects_overlapping_roots(tmp_path: Path, relation: str) -> None:
    source = tmp_path / "src"
    source.mkdir()
    (source / "keep").write_bytes(b"untouched")
    compiled = {"same": source, "parent": tmp_path, "child": source / "compiled"}[relation]

    with pytest.raises(BuildError, match="overlap unsafely"):
        mirror_sources(source, compiled)

    assert (source / "keep").read_bytes() == b"untouched"


@pytest.mark.parametrize("create_paths", [
    pytest.param(lambda source, compiled, outside: source.symlink_to(outside, target_is_directory=True),
                 id="source"),
    pytest.param(lambda source, compiled, outside: (
        source.mkdir(), compiled.symlink_to(outside, target_is_directory=True),
    ), id="compiled"),
    pytest.param(lambda source, compiled, outside: (
        source.mkdir(), (source / "link").symlink_to(outside / "keep"),
    ), id="source-file"),
    pytest.param(lambda source, compiled, outside: (
        source.mkdir(), (source / "link").symlink_to(outside, target_is_directory=True),
    ), id="source-directory"),
])
def test_projection_rejects_unexpected_symlinks(
    tmp_path: Path, create_paths: Callable[[Path, Path, Path], object],
) -> None:
    source = tmp_path / "src"
    compiled = tmp_path / "compiled"
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep").write_bytes(b"untouched")
    create_paths(source, compiled, outside)

    with pytest.raises(BuildError, match="Symlink"):
        mirror_sources(source, compiled)

    assert (outside / "keep").read_bytes() == b"untouched"


def test_projection_requires_absolute_paths(tmp_path: Path) -> None:
    source = tmp_path / "src"
    source.mkdir()

    with pytest.raises(BuildError, match="absolute"):
        mirror_sources(source, Path("relative"))


@pytest.mark.parametrize("failure,expected", [
    pytest.param(None, nullcontext(), id="success"),
    pytest.param(OSError("Synthetic build failure"), pytest.raises(OSError, match="Synthetic build failure"),
                 id="failure"),
    pytest.param(KeyboardInterrupt(), pytest.raises(KeyboardInterrupt, match="^$"), id="cancellation"),
])
def test_stage_has_final_layout_and_is_removed_on_completion(
    tmp_path: Path, failure: BaseException | None, expected: AbstractContextManager[object],
) -> None:
    stage = tmp_path / "out_stage"

    with expected, create_staging_tree(stage) as payload:
        assert payload == stage / "system"
        (payload / "retroarch").mkdir()
        (payload / "retroarch/retroarch").write_bytes(b"binary")

        if failure is not None:
            raise failure

    assert not stage.exists()


def test_stage_refuses_leftovers_without_removing_them(tmp_path: Path) -> None:
    stage = tmp_path / "out_stage"
    stage.mkdir()
    (stage / "keep").write_bytes(b"interrupted")

    with pytest.raises(BuildError, match="Unfinished output staging"), create_staging_tree(stage):
        pytest.fail("Existing stage must not be entered")

    assert (stage / "keep").read_bytes() == b"interrupted"


def test_stage_refuses_symlink_to_another_directory(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    stage = tmp_path / "out_stage"
    stage.symlink_to(outside, target_is_directory=True)

    with pytest.raises(BuildError, match="Symlink"), create_staging_tree(stage):
        pytest.fail("Symlink stage must not be entered")

    assert stage.is_symlink()
    assert outside.is_dir()


def test_cached_build_records_success_only_after_context_exit(tmp_path: Path) -> None:
    source = tmp_path / "src"
    source.mkdir()
    (source / "file.c").write_bytes(b"source")
    compiled = tmp_path / "compiled"
    state = {"source_sha256": "pinned", "recipe_sha256": "recipe"}

    with reuse_cached_build(source, compiled, state) as reuse:
        assert reuse is False
        assert not (compiled / BUILD_STATE).exists()
        (compiled / "file.o").write_bytes(b"object")

    with reuse_cached_build(source, compiled, state) as reuse:
        assert reuse is True
        assert not (compiled / BUILD_STATE).exists()
        assert (compiled / "file.o").read_bytes() == b"object"

    assert (compiled / BUILD_STATE).is_file()


def test_cache_marker_symlink_is_rejected_without_deleting_target(tmp_path: Path) -> None:
    source = tmp_path / "src"
    source.mkdir()
    compiled = tmp_path / "compiled"
    compiled.mkdir()
    outside = tmp_path / "outside.json"
    outside.write_bytes(b"untouched")
    (compiled / BUILD_STATE).symlink_to(outside)

    with pytest.raises(BuildError, match="Symlink"), reuse_cached_build(source, compiled, {}):
        pytest.fail("Symlink state must not be accepted")

    assert outside.read_bytes() == b"untouched"


@pytest.mark.parametrize(
    "field, reason",
    [
        ("source_sha256", "sources changed"), ("submodules_sha256", "sources changed"),
        ("recipe_sha256", "recipe changed"), ("profile_sha256", "profile changed"),
        ("environment_sha256", "environment changed"), ("toolchain_sha256", "toolchain changed"),
        ("host_toolchain_sha256", "host toolchain changed"), ("unexpected-field", "build state changed"),
    ],
)
def test_cache_miss_logs_only_changed_categories(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, field: str, reason: str,
) -> None:
    source = tmp_path / "src"
    source.mkdir()
    compiled = tmp_path / "compiled"
    previous = {"version": 2, field: "private-previous-value"}

    with reuse_cached_build(source, compiled, previous):
        (compiled / "old.o").write_bytes(b"old object")

    with (
        caplog.at_level(logging.INFO),
        reuse_cached_build(source, compiled, {**previous, field: "private-new-value"}) as reuse,
    ):
        assert reuse is False
        assert not (compiled / "old.o").exists()

    assert caplog.messages == [f"Build cache miss: {compiled} ({reason})"]
    assert "private-" not in caplog.text
    assert field not in caplog.text
