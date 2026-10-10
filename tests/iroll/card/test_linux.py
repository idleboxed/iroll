"""Synthetic Linux metadata, file descriptors and command responses only.

SPDX-License-Identifier: BSD-3-Clause
"""

import errno
import hashlib
import json
import os
import shutil
import stat
import struct
import subprocess
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from iroll.card import linux as linux
from iroll.card.layout import CardError, CardPlan, CardTarget

VolumeData = tuple[linux.CardVolume, Path, list[tuple[str, str]], list[list[str]]]
InspectionData = tuple[linux.LinuxCardBackend, dict[str, str], list[Path]]


def test_listing_checks_removable_card_and_partitions(disk_listing: dict[str, Any], target: CardTarget) -> None:
    assert linux.validate_listing(disk_listing, target.device, 42) == target


@pytest.mark.parametrize("field,value,message", [
    ("type", "part", "whole disk"), ("rm", False, "removable"), ("tran", "sata", "readers"),
    ("ro", True, "Read-only"), ("size", "1234", "size"),
    ("maj:min", "broken", "number"),
    pytest.param("tran", [], "readers", id="transport-list"),
    pytest.param("tran", {}, "readers", id="transport-object"),
    pytest.param("size", True, "size", id="boolean-size"),
    pytest.param("maj:min", None, "number", id="null-device-number"),
    pytest.param("maj:min", 240, "number", id="numeric-device-number"),
    pytest.param("children", None, "child device list", id="null-children"),
    pytest.param("children", {}, "child device list", id="children-object"),
    pytest.param("children", [None], "child entry", id="null-child"),
    pytest.param("children", [[]], "child entry", id="child-list"),
    pytest.param("mountpoints", None, "mountpoint list", id="null-mountpoints"),
    pytest.param("mountpoints", "/media/card", "mountpoint list", id="mountpoints-string"),
    pytest.param("mountpoints", [42], "mountpoint list", id="non-string-mountpoint"),
    pytest.param("model", 42, "model or serial", id="numeric-model"),
    pytest.param("serial", [], "model or serial", id="serial-list"),
])
def test_listing_refuses_unsafe_disk(disk_listing: dict[str, Any], field: str, value: Any, message: str) -> None:
    disk_listing[field] = value

    with pytest.raises(CardError, match=message):
        linux.validate_listing(disk_listing, "/dev/sdz", 42)


@pytest.mark.parametrize("field,value,message", [
    ("ro", True, "Read-only"),
    ("path", "/dev/sdy1", "Unexpected"), ("type", "crypt", "Unexpected"),
    ("children", [{"type": "raid"}], "Unexpected"),
    pytest.param("path", None, "Unexpected", id="null-path"),
    pytest.param("path", 1, "Unexpected", id="numeric-path"),
    pytest.param("maj:min", [], "number", id="device-number-list"),
    pytest.param("children", None, "Unexpected", id="null-children"),
    pytest.param("children", {}, "Unexpected", id="children-object"),
])
def test_listing_refuses_unsafe_child(disk_listing: dict[str, Any], field: str, value: Any, message: str) -> None:
    disk_listing["children"][0][field] = value

    with pytest.raises(CardError, match=message):
        linux.validate_listing(disk_listing, "/dev/sdz", 42)


def test_listing_keeps_mounted_paths_without_rejecting_card(
    disk_listing: dict[str, Any], target: CardTarget,
) -> None:
    disk_listing["children"][0]["mountpoints"] = ["/media/card", None]

    result = linux.validate_listing(disk_listing, target.device, 42)

    assert result == target
    assert result.mountpoints == ("/media/card",)


def test_mounted_paths_select_only_target_devices(target: CardTarget, monkeypatch: pytest.MonkeyPatch) -> None:
    text = (
        "1 0 8:1 / / rw - ext4 /dev/sda1 rw\n"
        "2 0 8:241 / /media/card rw - exfat /dev/sdz1 rw\n"
    )
    monkeypatch.setattr(Path, "read_text", lambda self: text)

    assert linux.LinuxCardBackend().list_mounted_paths(target) == ("/media/card",)


def test_unmounts_deepest_paths_and_rechecks_identity(
    target: CardTarget, monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = linux.LinuxCardBackend()
    mounted = ["/media/card", "/media/card/nested"]
    inspections, commands = [], []

    def inspect(device: str) -> CardTarget:
        inspections.append(device)

        return target

    def run_command(argv: list[str], **kwargs: object) -> str:
        commands.append(argv)
        mounted.remove(argv[-1])

        return ""

    monkeypatch.setattr(backend, "inspect", inspect)
    monkeypatch.setattr(backend, "list_mounted_paths", lambda selected: tuple(mounted))
    monkeypatch.setattr(backend, "run_command", run_command)

    result = backend.unmount(target)

    assert result == ("/media/card/nested", "/media/card")
    assert commands == [
        ["umount", "--", "/media/card/nested"],
        ["umount", "--", "/media/card"],
    ]
    assert len(inspections) == 4


def test_unmount_failure_stops_without_retry(target: CardTarget, monkeypatch: pytest.MonkeyPatch) -> None:
    backend = linux.LinuxCardBackend()
    monkeypatch.setattr(backend, "inspect", lambda device: target)
    monkeypatch.setattr(backend, "list_mounted_paths", lambda selected: ("/media/card",))
    calls = []

    def run_command(argv: list[str], **kwargs: object) -> str:
        calls.append(argv)
        raise CardError("synthetic unmount failure")

    monkeypatch.setattr(backend, "run_command", run_command)

    with pytest.raises(CardError, match="unmount failure"):
        backend.unmount(target)

    assert calls == [["umount", "--", "/media/card"]]


def test_gpt_readback_accepts_only_planned_geometry(table: dict[str, Any], target: CardTarget) -> None:
    linux.validate_table(table, CardPlan(target))


@pytest.mark.parametrize("document,message", [
    pytest.param(None, "partition table document", id="null-document"),
    pytest.param([], "partition table document", id="document-list"),
    pytest.param({"partitiontable": None}, "GPT layout", id="null-table"),
    pytest.param({"partitiontable": []}, "GPT layout", id="table-list"),
])
def test_gpt_readback_rejects_non_object_documents(target: CardTarget, document: Any, message: str) -> None:

    with pytest.raises(CardError, match=message):
        linux.validate_table(document, CardPlan(target))


@pytest.mark.parametrize("field,value", [
    ("label", "dos"), ("lastlba", 42), ("sectorsize", 4096), ("partitions", []),
    pytest.param("partitions", None, id="null-partitions"),
    pytest.param("partitions", {}, id="partitions-object"),
    pytest.param("partitions", "partition", id="partitions-string"),
    pytest.param("partitions", 1, id="partitions-number"),
])
def test_gpt_readback_rejects_wrong_table(
    table: dict[str, Any], target: CardTarget, field: str, value: Any,
) -> None:
    table["partitiontable"][field] = value

    with pytest.raises(CardError, match="GPT layout"):
        linux.validate_table(table, CardPlan(target))


@pytest.mark.parametrize("part", [
    pytest.param(None, id="null-partition"),
    pytest.param([], id="partition-list"),
    pytest.param("partition", id="partition-string"),
])
def test_gpt_readback_rejects_non_object_partition(table: dict[str, Any], target: CardTarget, part: Any) -> None:
    table["partitiontable"]["partitions"] = [part]

    with pytest.raises(CardError, match="partition does not match"):
        linux.validate_table(table, CardPlan(target))


@pytest.mark.parametrize("field,value", [
    ("start", 63), ("size", 4096), ("type", "wrong"), ("name", "wrong"),
    pytest.param("type", None, id="null-partition-type"),
    pytest.param("type", [], id="partition-type-list"),
])
def test_gpt_readback_rejects_wrong_partition(
    table: dict[str, Any], target: CardTarget, field: str, value: Any,
) -> None:
    table["partitiontable"]["partitions"][0][field] = value

    with pytest.raises(CardError, match="partition does not match"):
        linux.validate_table(table, CardPlan(target))


def test_commands_use_trusted_path_and_inherit_only_pinned_fd(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    class Runner:
        def run(self, argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            calls.append((argv, kwargs))

            return subprocess.CompletedProcess(argv, 0, "synthetic\n", "")

    monkeypatch.setattr(linux.shutil, "which", lambda name, path: f"/usr/sbin/{name}")

    result = linux.LinuxCardBackend(Runner()).run_command(["sfdisk", "--json", "/proc/self/fd/72"], fd=72)

    assert result == "synthetic\n"
    argv, kwargs = calls[0]
    assert argv[0] == "/usr/sbin/sfdisk"
    assert kwargs["pass_fds"] == (72,)
    assert kwargs["env"] == {"PATH": linux.TOOL_PATH, "LC_ALL": "C"}
    assert kwargs["cwd"] == Path("/")


def test_opened_fd_guard_matches_all_identity_fields(target: CardTarget, guard_io: dict[int, int]) -> None:
    linux.LinuxCardBackend().guard(100, target)


@pytest.mark.parametrize("output,message", [
    pytest.param("{", "Invalid sfdisk JSON output", id="malformed-json"),
    pytest.param("null", "object in sfdisk JSON output", id="null-document"),
    pytest.param("[]", "object in sfdisk JSON output", id="document-list"),
])
def test_table_verification_reports_invalid_command_json_without_retry(
    target: CardTarget, guard_io: dict[int, int], monkeypatch: pytest.MonkeyPatch, output: str, message: str,
) -> None:
    backend = linux.LinuxCardBackend()
    calls = []

    def run_command(argv: list[str], **kwargs: object) -> str:
        calls.append((argv, kwargs))
        return output

    monkeypatch.setattr(backend, "run_command", run_command)

    with pytest.raises(CardError, match=message):
        backend.verify_table(100, CardPlan(target))

    assert calls == [
        (["sfdisk", "--verify", "/proc/self/fd/100"], {"fd": 100}),
        (["sfdisk", "--json", "/proc/self/fd/100"], {"fd": 100}),
    ]


@pytest.mark.parametrize("field,value", [
    (linux.BLKGETSIZE64, 0), (linux.BLKGETDISKSEQ, 43), (linux.BLKSSZGET, 4096), (linux.BLKROGET, 1),
])
def test_opened_fd_guard_rejects_identity_changes(
    target: CardTarget, guard_io: dict[int, int], field: int, value: int,
) -> None:
    guard_io[field] = value

    with pytest.raises(CardError, match="Opened card"):
        linux.LinuxCardBackend().guard(100, target)


def test_exclusive_claim_closes_fd_after_guard_failure(
    target: CardTarget, monkeypatch: pytest.MonkeyPatch,
) -> None:
    opened, closed = [], []

    def open_fd(path: str, flags: int) -> int:
        opened.append((path, flags))

        return 100

    backend = linux.LinuxCardBackend()
    monkeypatch.setattr(linux.os, "open", open_fd)
    monkeypatch.setattr(linux.os, "close", closed.append)
    monkeypatch.setattr(linux.fcntl, "flock", lambda *args: None)
    monkeypatch.setattr(backend, "guard", lambda *args: (_ for _ in ()).throw(CardError("changed")))

    with pytest.raises(CardError, match="changed"), backend.open(target):
        pytest.fail("Must refuse before yielding")

    assert opened[0][1] & os.O_EXCL and opened[0][1] & os.O_NOFOLLOW
    assert closed == [100]


def test_partition_uses_pinned_fd_no_reread_and_clears_only_boot_gap(
    target: CardTarget, monkeypatch: pytest.MonkeyPatch, table: dict[str, Any],
) -> None:
    backend = linux.LinuxCardBackend()
    calls, writes, synced = [], [], []
    monkeypatch.setattr(backend, "guard", lambda *args: None)

    def run_command(argv: list[str], **kwargs: object) -> str:
        calls.append((argv, kwargs))

        return json.dumps(table) if "--json" in argv else ""

    def write(fd: int, data: bytes, offset: int) -> int:
        writes.append((fd, len(data), offset, any(data)))

        return len(data)

    monkeypatch.setattr(backend, "run_command", run_command)
    monkeypatch.setattr(linux.os, "pwrite", write)
    monkeypatch.setattr(linux.os, "fsync", synced.append)

    backend.partition(100, CardPlan(target))

    assert "--no-reread" in calls[0][0] and "--no-tell-kernel" in calls[0][0]
    assert "--force" not in calls[0][0]
    assert calls[0][0][-1] == "/proc/self/fd/100"
    assert calls[0][1]["input_data"] == CardPlan(target).build_sfdisk_input()
    assert writes == [(100, (2048 - 34) * 512, 34 * 512, False)]
    assert synced == [100]


def test_short_boot_gap_write_stops_without_retry(
    target: CardTarget, monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = linux.LinuxCardBackend()
    calls = []
    monkeypatch.setattr(backend, "guard", lambda *args: None)
    monkeypatch.setattr(backend, "run_command", lambda *args, **kwargs: "")

    def short(fd: int, data: bytes, offset: int) -> int:
        calls.append(offset)

        return len(data) - 1

    monkeypatch.setattr(linux.os, "pwrite", short)

    with pytest.raises(CardError, match="Short write"):
        backend.partition(100, CardPlan(target))

    assert len(calls) == 1


def test_loop_uapi_layout_and_backing_fd_are_pinned(
    target: CardTarget, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    backend = linux.LinuxCardBackend()
    events, closed = [], []
    monkeypatch.setattr(linux.os, "open", lambda path, flags: 10 if f"{path}" == "/dev/loop-control" else 11)
    monkeypatch.setattr(linux.os, "close", closed.append)
    monkeypatch.setattr(linux.os, "fstat", lambda fd: SimpleNamespace(st_mode=stat.S_IFBLK, st_rdev=os.makedev(7, 9)))
    monkeypatch.setattr(linux.fcntl, "flock", lambda *args: None)
    monkeypatch.setattr(linux.CardVolume, "guard", lambda self: None)
    monkeypatch.setattr(Path, "read_text", lambda self: "")

    def ioctl(fd: int, request: int, arg: bytes | None = None) -> int:
        events.append((fd, request, arg))

        return 9 if request == linux.LOOP_CTL_GET_FREE else 0

    monkeypatch.setattr(linux.fcntl, "ioctl", ioctl)
    journal = SimpleNamespace(record_event=lambda *args, **kwargs: None)

    with backend.open_volume(100, CardPlan(target), journal) as volume:
        assert volume.path == Path("/dev/loop9")

    configuration = events[1][2]
    assert len(configuration) == 304
    assert struct.unpack_from("=II", configuration) == (100, 512)
    info = linux.LOOP_INFO.unpack(configuration[8:240])
    assert info[3:5] == (2048 * 512, CardPlan(target).partition_sectors * 512)
    assert info[8] == linux.LO_FLAGS_AUTOCLEAR
    assert [event[1] for event in events] == [linux.LOOP_CTL_GET_FREE, linux.LOOP_CONFIGURE, linux.LOOP_CLR_FD]
    assert closed == [10, 11]


def test_copy_instruction_bytes_and_cleanup_owned_mount(volume: VolumeData) -> None:
    value, mountpoint, records, calls = volume
    content = b"# Synthetic guide\n"

    digest = value.copy_instructions(content, SimpleNamespace(record_event=lambda *args, **kwargs: None))

    assert digest == hashlib.sha256(content).hexdigest()
    assert not records and not mountpoint.exists()
    assert [call[0] for call in calls] == ["mount", "umount"]
    assert "nosuid,nodev,noexec" in calls[0]


def test_cancelled_mount_that_succeeded_is_unmounted(volume: VolumeData, monkeypatch: pytest.MonkeyPatch) -> None:
    value, mountpoint, records, calls = volume
    original = value.backend.run_command

    def run_command(argv: list[str], **kwargs: Any) -> str:
        result = original(argv, **kwargs)

        if argv[0] == "mount":
            raise KeyboardInterrupt

        return result

    monkeypatch.setattr(value.backend, "run_command", run_command)

    with pytest.raises(KeyboardInterrupt, match="^$"):
        value.copy_instructions(b"synthetic\n", SimpleNamespace(record_event=lambda *args, **kwargs: None))

    assert not records and not mountpoint.exists()
    assert [call[0] for call in calls] == ["mount", "umount"]


def test_unmount_failure_keeps_recovery_location(volume: VolumeData, monkeypatch: pytest.MonkeyPatch) -> None:
    value, mountpoint, records, calls = volume
    original = value.backend.run_command

    def run_command(argv: list[str], **kwargs: Any) -> str:

        if argv[0] == "umount":
            raise CardError("synthetic unmount failure")

        return original(argv, **kwargs)

    monkeypatch.setattr(value.backend, "run_command", run_command)

    with pytest.raises(CardError, match="unmount failure"):
        value.copy_instructions(b"synthetic\n", SimpleNamespace(record_event=lambda *args, **kwargs: None))

    assert records and mountpoint.is_dir()
    assert (mountpoint / "INSTRUCTIONS.md").read_bytes() == b"synthetic\n"


def test_inspect_checks_sysfs_mountinfo_and_swap(inspection: InspectionData, target: CardTarget) -> None:
    backend, texts, holders = inspection

    assert backend.inspect("/dev/sdz") == target


@pytest.mark.parametrize("path,value,message", [
    pytest.param("/sys/class/block/sdz/diskseq", "invalid", "Invalid sysfs integer", id="invalid-diskseq"),
    pytest.param("/sys/class/block/sdz/diskseq", "", "Invalid sysfs integer", id="empty-diskseq"),
    pytest.param("/sys/class/block/sdz/queue/logical_block_size", "512.0", "Invalid sysfs integer",
                 id="invalid-sector-size"),
    pytest.param("/proc/swaps", "Filename Type Size Used Priority\nincomplete", "Invalid Linux swap record",
                 id="malformed-swaps"),
])
def test_inspect_rejects_malformed_system_metadata_before_writing(
    inspection: InspectionData, target: CardTarget, path: str, value: str, message: str,
) -> None:
    backend, texts, _ = inspection
    texts[path] = value

    with pytest.raises(CardError, match=message):
        backend.inspect(target.device)


def test_mounted_paths_reject_malformed_mountinfo(inspection: InspectionData, target: CardTarget) -> None:
    backend, texts, _ = inspection
    texts["/proc/self/mountinfo"] = "incomplete"

    with pytest.raises(CardError, match="Invalid Linux mountinfo"):
        backend.list_mounted_paths(target)


@pytest.mark.parametrize("output,message", [
    pytest.param("{", "Invalid lsblk JSON output", id="malformed-json"),
    pytest.param("1" * 5000, "lsblk JSON output", id="large-integer"),
    pytest.param("[" * 2000 + "]" * 2000, "lsblk JSON output", id="deep-document"),
    pytest.param("null", "object in lsblk JSON output", id="null-document"),
    pytest.param("[]", "object in lsblk JSON output", id="document-list"),
    pytest.param("{}", "Ambiguous lsblk result", id="missing-devices"),
    pytest.param(json.dumps({"blockdevices": None}), "Ambiguous lsblk result", id="null-devices"),
    pytest.param(json.dumps({"blockdevices": {}}), "Ambiguous lsblk result", id="devices-object"),
    pytest.param(json.dumps({"blockdevices": "disk"}), "Ambiguous lsblk result", id="devices-string"),
    pytest.param(json.dumps({"blockdevices": [None]}), "lsblk disk entry", id="null-disk"),
    pytest.param(json.dumps({"blockdevices": [[]]}), "lsblk disk entry", id="disk-list"),
    pytest.param(json.dumps({"blockdevices": [{}, {}]}), "Ambiguous lsblk result", id="multiple-disks"),
])
def test_inspect_rejects_invalid_listing_without_retry(
    inspection: InspectionData, monkeypatch: pytest.MonkeyPatch, output: str, message: str,
) -> None:
    backend, texts, holders = inspection
    calls = []

    def run_command(argv: list[str], **kwargs: object) -> str:
        calls.append(argv)
        return output

    monkeypatch.setattr(backend, "run_command", run_command)

    with pytest.raises(CardError, match=message):
        backend.inspect("/dev/sdz")

    assert len(calls) == 1
    assert calls[0][0] == "lsblk" and calls[0][-1] == "/dev/sdz"


def test_inspect_allows_mounted_partitions_for_later_automatic_unmount(
    inspection: InspectionData, disk_listing: dict[str, Any], target: CardTarget,
) -> None:
    backend, texts, holders = inspection
    disk_listing["children"][0]["mountpoints"] = ["/media/card"]
    texts["/proc/self/mountinfo"] = "1 0 8:241 / /media/card rw - exfat /dev/sdz1 rw\n"

    result = backend.inspect("/dev/sdz")

    assert result == target
    assert result.mountpoints == ("/media/card",)


@pytest.mark.parametrize("path,value,message", [
    ("/sys/class/block/sdz/dev", "8:16", "number mismatch"),
    ("/sys/class/block/sdz/queue/logical_block_size", "4096", "512-byte"),
])
def test_inspect_rejects_sysfs_and_mount_conflicts(
    inspection: InspectionData, path: str, value: str, message: str,
) -> None:
    backend, texts, holders = inspection
    texts[path] = value

    with pytest.raises(CardError, match=message):
        backend.inspect("/dev/sdz")


def test_inspect_rejects_holders(inspection: InspectionData) -> None:
    backend, texts, holders = inspection
    holders.append(Path("/synthetic/dm-0"))

    with pytest.raises(CardError, match="holders"):
        backend.inspect("/dev/sdz")


def test_inspect_catches_replug_during_listing(inspection: InspectionData) -> None:
    backend, texts, holders = inspection
    original = backend.run_command

    def run_command(*args: Any, **kwargs: Any) -> str:
        texts["/sys/class/block/sdz/diskseq"] = "43\n"

        return original(*args, **kwargs)

    backend.run_command = run_command

    with pytest.raises(CardError, match="changed during inspection"):
        backend.inspect("/dev/sdz")


def test_preflight_refuses_nonroot_before_tools_or_devices(
    target: CardTarget, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(linux.os, "geteuid", lambda: 1000)

    with pytest.raises(CardError, match="requires root"):
        linux.LinuxCardBackend().validate_initialization(target, tmp_path / "journal")


def test_preflight_missing_formatter_names_its_system_package_before_writing(
    target: CardTarget, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(linux.os, "geteuid", lambda: 0)
    original_stat = linux.os.stat

    def metadata(path: str | Path, *args: Any, **kwargs: Any) -> os.stat_result | SimpleNamespace:

        if path in ("/proc/self/ns/mnt", "/proc/1/ns/mnt"):
            return SimpleNamespace(st_ino=123)

        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(linux.os, "stat", metadata)
    monkeypatch.setattr(linux.shutil, "which", lambda tool, **kwargs: None if tool == "mkfs.exfat" else tool)

    with pytest.raises(CardError, match=r"mkfs.exfat.*exfatprogs.*iroll card deps"):
        linux.LinuxCardBackend().validate_initialization(target, tmp_path / "journal")

    assert not list(tmp_path.iterdir())


def test_format_never_uses_full_erase_or_physical_device(
    volume: VolumeData, monkeypatch: pytest.MonkeyPatch,
) -> None:
    value, mountpoint, records, calls = volume
    synced = []
    monkeypatch.setattr(linux.os, "fsync", synced.append)

    value.format()

    assert calls == [["mkfs.exfat", "-L", "idlebox", "/proc/self/fd/101"]]
    assert synced == [101]


def test_format_rejects_unexpected_mount(volume: VolumeData) -> None:
    value, mountpoint, records, calls = volume
    records.append(("7:9", "/media/foreign"))

    with pytest.raises(CardError, match="unexpectedly mounted"):
        value.format()

    assert not calls


@pytest.mark.skipif(
    shutil.which("sfdisk", path=linux.TOOL_PATH) is None, reason="Optional host sfdisk is not installed",
)
def test_sfdisk_recipe_roundtrip_on_sparse_regular_file_without_root(
    target: CardTarget, tmp_path: Path,
) -> None:
    plan = CardPlan(replace(target, size=64 * 1024**2))
    backend = linux.LinuxCardBackend()
    image = tmp_path / "synthetic-gpt.bin"

    with image.open("xb+") as stream:
        stream.truncate(plan.target.size)
        fd = stream.fileno()
        reference = f"/proc/self/fd/{fd}"

        backend.run_command(
            [
                "sfdisk", "--no-reread", "--no-tell-kernel", "--wipe", "always",
                "--wipe-partitions", "always", reference,
            ],
            fd=fd, input_data=plan.build_sfdisk_input(),
        )
        backend.run_command(["sfdisk", "--verify", reference], fd=fd)
        table = json.loads(backend.run_command(["sfdisk", "--json", reference], fd=fd))

        linux.validate_table(table, plan)
        assert os.pread(fd, 8, 512) == b"EFI PART"
        assert os.pread(fd, 8, plan.target.size - 512) == b"EFI PART"


def test_failed_loop_allocation_does_not_detach_someone_elses_volume(
    target: CardTarget, monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls, closed = [], []
    backend = linux.LinuxCardBackend()
    monkeypatch.setattr(linux.os, "open", lambda path, flags: 10 if f"{path}" == "/dev/loop-control" else 11)
    monkeypatch.setattr(linux.os, "close", closed.append)
    monkeypatch.setattr(linux.os, "fstat", lambda fd: SimpleNamespace(st_mode=stat.S_IFBLK))
    monkeypatch.setattr(linux.fcntl, "flock", lambda *args: None)

    def ioctl(fd: int, request: int, arg: bytes | None = None) -> int:
        calls.append(request)

        if request == linux.LOOP_CTL_GET_FREE:
            return 9

        raise OSError(errno.EBUSY, "synthetic racing allocation")

    monkeypatch.setattr(linux.fcntl, "ioctl", ioctl)
    journal = SimpleNamespace(record_event=lambda *args, **kwargs: None)

    with pytest.raises(OSError, match="racing allocation"), backend.open_volume(100, CardPlan(target), journal):
        pytest.fail("No volume should be yielded")

    assert calls == [linux.LOOP_CTL_GET_FREE, linux.LOOP_CONFIGURE]
    assert closed == [10, 11]
