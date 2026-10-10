"""SDK preparation with synthetic pinned archives, NOR and host tools; no hardware.

SPDX-License-Identifier: BSD-3-Clause
"""

import logging
import os
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any, NoReturn

import pytest

from iroll.common import paths
from iroll.common.errors import BuildError
from iroll.common.files import acquire_directory_lock
from iroll.host.platforms import PROFILES
from iroll.sdk.discovery import discover_sdk
from iroll.sdk.prepare import SdkOptions, SdkPreparer
from iroll.components.sources import SourceDownloads

PreparationData = tuple[SdkOptions, dict[str, Any], Any]


def test_prepare_and_reuse_sdk_without_writes_or_commands(
    preparation: PreparationData, caplog: pytest.LogCaptureFixture,
) -> None:
    options, recipe, runner = preparation
    preparer = SdkPreparer(options, recipe=recipe, runner=runner)

    with caplog.at_level(logging.INFO, logger="iroll.sdk.prepare"):
        result = preparer.run()

    messages = [record.getMessage() for record in caplog.records if record.name == "iroll.sdk.prepare"]
    assert messages[-1] == f"SDK prepared: {options.sdk}"

    configure = next(argv for argv in runner.calls if "./configure" in argv)
    assert "--build=x86_64-pc-linux-gnu" in configure
    assert f"--host={recipe["triplet"]}" in configure
    assert result["status"] == "SDK_PREPARED"
    assert not result["hardware_verified"]
    assert not result["target_programs_executed"]
    assert (Path(result["reports"]) / "result.json").is_file()
    assert not paths.get_sdk_pending_path(options.sdk).exists()
    assert not list(options.sdk.parent.glob(".iroll-sdk-*"))
    assert (options.sdk / "share/buildroot/sdk-location").read_text().strip() == f"{options.sdk}"
    tracked = [path for base in (options.sdk.parent, options.logs, options.downloads) for path in base.rglob("*")]
    before = {path: (path.lstat().st_ino, path.lstat().st_mtime_ns) for path in tracked}
    runner.calls.clear()

    reused = preparer.run()

    assert reused["status"] == "SDK_REUSED"
    assert not runner.calls
    assert {path: (path.lstat().st_ino, path.lstat().st_mtime_ns) for path in tracked} == before
    assert len(list(options.logs.iterdir())) == 2
    assert (Path(reused["reports"]) / "result.json").is_file()


def test_constructor_does_not_read_files_or_run_tools(preparation: PreparationData) -> None:
    options, _, runner = preparation

    SdkPreparer(options, runner=runner)

    assert not options.sdk.parent.exists()
    assert not options.logs.exists()
    assert not runner.calls


@pytest.mark.parametrize("damage", [
    pytest.param(lambda path: path.write_bytes(b"x" * path.stat().st_size), id="content"),
    pytest.param(lambda path: path.write_bytes(b"short"), id="size"),
    pytest.param(
        lambda path: (path.rename(path.with_name("real.bin")), path.symlink_to(path.with_name("real.bin"))),
        id="symlink",
    ),
    pytest.param(lambda path: (path.unlink(), path.mkdir()), id="directory"),
])
def test_invalid_nor_fails_before_any_write_or_command(
    preparation: PreparationData, damage: Callable[[Path], object],
) -> None:
    options, recipe, runner = preparation
    damage(options.nor)

    with pytest.raises((BuildError, OSError), match="NOR|Symlink|directory"):
        SdkPreparer(options, recipe=recipe, runner=runner).run()

    assert not options.sdk.parent.exists()
    assert not options.logs.exists()
    assert not runner.calls


def test_existing_unmanaged_sdk_is_never_replaced(preparation: PreparationData) -> None:
    options, recipe, runner = preparation
    options.sdk.mkdir(parents=True)
    keep = options.sdk / "keep"
    keep.write_bytes(b"original SDK")

    with pytest.raises(BuildError, match="no matching preparation record"):
        SdkPreparer(options, recipe=recipe, runner=runner).run()

    assert keep.read_bytes() == b"original SDK"
    assert options.logs.is_dir()
    assert not runner.calls


@pytest.mark.parametrize("damage", [
    pytest.param(lambda options, recipe: (options.sdk / "share/buildroot/sdk-location").write_text("modified"),
                 id="content"),
    pytest.param(lambda options, recipe: (options.sdk / "outside").symlink_to(options.nor), id="external-link"),
    pytest.param(lambda options, recipe: (
        (options.sdk / recipe["triplet"] / "sysroot/usr/lib/vendor-alias.so").unlink(),
        (options.sdk / recipe["triplet"] / "sysroot/usr/lib/vendor-alias.so").symlink_to("./vendor.so"),
    ), id="internal-link"),
    pytest.param(lambda options, recipe: (options.sdk / "bin" / f"{recipe["triplet"]}-gcc").chmod(0o644), id="mode"),
    pytest.param(lambda options, recipe: recipe.update(id="new-recipe"), id="recipe"),
])
def test_modified_sdk_or_recipe_is_not_silently_reused(
    preparation: PreparationData, damage: Callable[[SdkOptions, dict[str, Any]], object],
) -> None:
    options, recipe, runner = preparation
    preparer = SdkPreparer(options, recipe=recipe, runner=runner)
    preparer.run()
    runner.calls.clear()
    damage(options, recipe)

    with pytest.raises(BuildError, match="changed|escapes|no matching"):
        preparer.run()

    assert options.sdk.exists()
    assert not runner.calls


@pytest.mark.parametrize("tool", ["relocate", "unsquashfs", "make", "readelf"])
@pytest.mark.parametrize(
    ("failure_field", "error_type", "message"),
    [
        pytest.param("fail", BuildError, "Synthetic SDK", id="tool-failure"),
        pytest.param("cancel", KeyboardInterrupt, "^$", id="cancellation"),
    ],
)
def test_tool_failure_and_cancellation_leave_no_sdk_or_staging(
    preparation: PreparationData, tool: str, failure_field: str, error_type: type[BaseException], message: str,
) -> None:
    options, recipe, runner = preparation
    setattr(runner, failure_field, tool)

    with pytest.raises(error_type, match=message):
        SdkPreparer(options, recipe=recipe, runner=runner).run()

    assert not options.sdk.exists()
    assert not paths.get_sdk_pending_path(options.sdk).exists()
    assert not list(options.sdk.parent.glob(".iroll-sdk-*"))
    assert len(list(options.logs.glob("*/failure.json"))) == 1


@pytest.mark.parametrize("issue", ["corrupt_library", "new_glibc", "bad_arch", "corrupt_header", "corrupt_pkgconfig"])
def test_payload_or_probe_failure_refuses_publication(preparation: PreparationData, issue: str) -> None:
    options, recipe, runner = preparation
    setattr(runner, issue, True)

    with pytest.raises(
        BuildError, match="library mismatch|incompatible loader|ELF|header mismatch|pkg-config mismatch",
    ):
        SdkPreparer(options, recipe=recipe, runner=runner).run()

    assert not options.sdk.exists()
    assert len(list(options.logs.glob("*/failure.json"))) == 1


@pytest.mark.parametrize(
    ("error", "message"),
    [
        pytest.param(BuildError("Final relocation failure"), "Final relocation failure", id="relocation-failure"),
        pytest.param(KeyboardInterrupt(), "^$", id="cancellation"),
    ],
)
def test_failure_after_rename_removes_only_new_sdk(
    preparation: PreparationData, error: BaseException, message: str,
) -> None:
    options, recipe, runner = preparation
    original = runner.run

    def fail_final(argv: Sequence[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:

        if argv[0] == "sh" and argv[1] == f"{options.sdk / "relocate-sdk.sh"}":

            raise error

        return original(argv, **kwargs)

    runner.run = fail_final

    with pytest.raises(type(error), match=message):
        SdkPreparer(options, recipe=recipe, runner=runner).run()

    assert not options.sdk.exists()
    assert not paths.get_sdk_pending_path(options.sdk).exists()
    assert not SdkPreparer(options).state_path.exists()


def test_pending_preparation_blocks_both_preparer_and_discovery(preparation: PreparationData) -> None:
    options, recipe, runner = preparation
    options.sdk.mkdir(parents=True)
    paths.get_sdk_pending_path(options.sdk).write_text("interrupted")

    with pytest.raises(BuildError, match="incomplete"):
        SdkPreparer(options, recipe=recipe, runner=runner).run()

    with pytest.raises(BuildError, match="incomplete"):
        discover_sdk(PROFILES["rk3032"], options.sdk, runner, cwd=options.sdk.parent)

    assert not runner.calls


@pytest.mark.parametrize("select_directory", [
    pytest.param(lambda options: options.downloads, id="downloads"),
    pytest.param(lambda options: options.logs, id="logs"),
    pytest.param(lambda options: options.sdk.parent, id="sdk-parent"),
])
def test_preparation_obeys_directory_locks(
    preparation: PreparationData, select_directory: Callable[[SdkOptions], Path],
) -> None:
    options, recipe, runner = preparation
    directory = select_directory(options)
    directory.mkdir(parents=True, exist_ok=True)

    with acquire_directory_lock(directory), pytest.raises(BuildError, match="holds the lock"):
        SdkPreparer(options, recipe=recipe, runner=runner).run()

    assert not options.sdk.exists()
    assert not runner.calls


@pytest.mark.parametrize("change_options", [
    pytest.param(lambda options: replace(options, logs=options.sdk / "logs"), id="overlap"),
    pytest.param(lambda options: replace(options, sdk=options.nor.parent), id="nor-in-sdk"),
    pytest.param(lambda options: (
        options.logs.parent.mkdir(parents=True), options.logs.symlink_to(options.downloads), options,
    )[-1], id="logs-link"),
    pytest.param(lambda options: replace(options, sdk=options.sdk.with_name("sdk with spaces")), id="unsafe-path"),
    pytest.param(lambda options: replace(options, sdk=Path("relative")), id="relative"),
])
def test_unsafe_output_paths_fail_before_tools(
    preparation: PreparationData, change_options: Callable[[SdkOptions], SdkOptions],
) -> None:
    options, recipe, runner = preparation
    options = change_options(options)

    with pytest.raises(BuildError, match="overlap|Symlink|metacharacters|absolute"):
        SdkPreparer(options, recipe=recipe, runner=runner).run()

    assert not runner.calls


def test_missing_tool_fails_before_extraction_or_reports(
    preparation: PreparationData, monkeypatch: pytest.MonkeyPatch,
) -> None:
    options, recipe, runner = preparation
    monkeypatch.setattr(
        "iroll.sdk.prepare.shutil.which", lambda name: None if name == "unsquashfs" else f"/tool/{name}",
    )

    with pytest.raises(BuildError, match="Missing SDK preparation tools: unsquashfs"):
        SdkPreparer(options, recipe=recipe, runner=runner).run()

    assert not options.sdk.exists()
    assert options.logs.is_dir()
    assert not runner.calls


def test_changed_archive_is_not_replaced_or_used(preparation: PreparationData) -> None:
    options, recipe, runner = preparation
    archive = options.downloads / "sdk/rk3032" / recipe["sources"]["bootlin"]["archive"]
    archive.write_bytes(b"changed")

    with pytest.raises(BuildError, match="SHA-256 mismatch"):
        SdkPreparer(options, recipe=recipe, runner=runner).run()

    assert archive.read_bytes() == b"changed"
    assert not options.sdk.exists()
    assert not runner.calls


def test_missing_archive_is_downloaded_to_sdk_subdirectory(preparation: PreparationData) -> None:
    options, recipe, runner = preparation
    source = recipe["sources"]["bootlin"]
    directory = options.downloads / "sdk/rk3032"
    path = directory / source["archive"]
    content = path.read_bytes()
    path.unlink()
    requested = []

    def fetch(url: str, destination: Path) -> None:
        requested.append(url)
        destination.write_bytes(content)

    sources = SourceDownloads(directory, fetch=fetch)

    result = SdkPreparer(options, recipe=recipe, runner=runner, sources=sources).run()

    assert result["status"] == "SDK_PREPARED"
    assert requested == [source["url"]]
    assert path.read_bytes() == content


def test_failure_releases_all_directory_locks(preparation: PreparationData) -> None:
    options, recipe, runner = preparation
    runner.fail = "unsquashfs"

    with pytest.raises(BuildError, match="Synthetic SDK"):
        SdkPreparer(options, recipe=recipe, runner=runner).run()

    runner.fail = None

    result = SdkPreparer(options, recipe=recipe, runner=runner).run()

    assert result["status"] == "SDK_PREPARED"


def test_sdk_normalizes_parent_segments_only_after_rejecting_symlinks(preparation: PreparationData) -> None:
    options, recipe, runner = preparation
    options.sdk.parent.mkdir(parents=True)
    options = replace(options, sdk=options.sdk.parent / "../sdk/rk3032")

    result = SdkPreparer(options, recipe=recipe, runner=runner).run()

    assert result["sdk"] == f"{options.sdk.resolve()}"
    assert (options.sdk / "share/buildroot/sdk-location").read_text().strip() == result["sdk"]


def test_special_nor_input_is_rejected_without_opening_it(
    preparation: PreparationData, monkeypatch: pytest.MonkeyPatch,
) -> None:
    options, recipe, runner = preparation
    options.nor.unlink()
    os.mkfifo(options.nor)

    def forbidden(*args: object, **kwargs: object) -> NoReturn:
        raise AssertionError("Special NOR input must never be opened")

    monkeypatch.setattr("iroll.sdk.prepare.os.open", forbidden)

    with pytest.raises(BuildError, match="NOR input must be a regular file"):
        SdkPreparer(options, recipe=recipe, runner=runner).run()

    assert not options.sdk.parent.exists()
    assert not runner.calls
