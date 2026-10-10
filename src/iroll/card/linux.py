"""Linux card operations through pinned descriptors and an owned loop device.

The physical disk stays exclusively claimed until the private volume is unmounted.
No BLKRRPART/partition uevent is requested: reconnect the reader after completion.
SPDX-License-Identifier: BSD-3-Clause
"""

import fcntl
import hashlib
import json
import os
import re
import shutil
import stat
import struct
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .journal import CardJournal, validate_journal_parent
from .layout import (
    BASIC_DATA_TYPE,
    INSTRUCTIONS_NAME,
    PARTITION_START,
    SECTOR_BYTES,
    VOLUME_LABEL,
    CardError,
    CardPlan,
    CardTarget,
    require,
    validate_same_card,
    validate_device_name,
)
from ..host.dependencies import CARD_PACKAGES, CARD_TOOL_PATH
from ..common.paths import prepare_save_directories
from ..common.files import sync_directory
from ..common.process import Runner
from .mounts import parse_mount_records

# Linux UAPI fs.h and loop.h; restricted to 64-bit Linux with 512-byte sectors.
BLKGETSIZE64 = 0x80081272
BLKGETDISKSEQ = 0x80081280
BLKFLSBUF = 0x1261
BLKROGET = 0x125E
BLKSSZGET = 0x1268
LOOP_CONFIGURE = 0x4C0A
LOOP_GET_STATUS64 = 0x4C05
LOOP_CLR_FD = 0x4C01
LOOP_CTL_GET_FREE = 0x4C82
LO_FLAGS_AUTOCLEAR = 4
LOOP_INFO = struct.Struct("=QQQQQIIII64s64s32sQQ")
TOOL_PATH = CARD_TOOL_PATH


def format_device_number(number: int) -> str:
    return f"{os.major(number)}:{os.minor(number)}"


def read_ioctl_value(fd: int, request: int, fmt: str) -> int:
    data = bytearray(struct.calcsize(fmt))
    fcntl.ioctl(fd, request, data, True)
    return struct.unpack(fmt, data)[0]


def read_sysfs_number(path: Path) -> int:
    try:
        return int(path.read_text())

    except ValueError as exc:
        raise CardError(f"Invalid sysfs integer: {path}") from exc


def parse_command_document(text: str, *, command: str) -> dict[str, Any]:
    try:
        document = json.loads(text)

    except (ValueError, RecursionError) as exc:
        raise CardError(f"Invalid {command} JSON output") from exc

    require(isinstance(document, dict), f"Expected an object in {command} JSON output")
    return document


def validate_listing(disk: dict[str, Any], device: str, diskseq: int) -> CardTarget:
    validate_device_name(device)
    require(isinstance(disk, dict), "Invalid lsblk disk entry")
    require(disk.get("path") == device and disk.get("type") == "disk", "Expected one whole disk")
    require(disk.get("rm") in (True, 1), "Only removable cards are allowed")
    require(disk.get("tran") in ("usb", "mmc"), "Only USB readers and native MMC/SD readers are allowed")
    children = disk.get("children", [])
    require(isinstance(children, list), "Invalid child device list")
    numbers = []
    mountpoints = []

    for item in [disk, *children]:
        require(isinstance(item, dict), "Invalid lsblk child entry")
        require(item.get("ro") in (False, 0), "Read-only target is forbidden")
        listed_mounts = item.get("mountpoints")
        require(
            isinstance(listed_mounts, list)
            and all(value is None or isinstance(value, str) for value in listed_mounts),
            "Invalid mountpoint list",
        )
        mountpoints.extend(value for value in listed_mounts if value)
        number = item.get("maj:min", "")
        require(
            isinstance(number, str) and re.fullmatch(r"[0-9]+:[0-9]+", number) is not None,
            "Invalid device number",
        )
        numbers.append(number)

        if item is not disk:
            separator = "p" if device.startswith("/dev/mmcblk") else ""
            path = item.get("path", "")
            nested = item.get("children", [])
            require(
                isinstance(path, str)
                and re.fullmatch(re.escape(device + separator) + r"[1-9][0-9]*", path) is not None
                and item.get("type") == "part" and isinstance(nested, list) and not nested,
                "Unexpected child device or active device mapper/RAID holder",
            )

    require(len(set(numbers)) == len(numbers), "Duplicate device numbers")
    size = disk.get("size")
    require(isinstance(size, int) and not isinstance(size, bool), "Invalid disk size")
    model, serial = disk.get("model"), disk.get("serial")
    require(all(value is None or isinstance(value, str) for value in (model, serial)), "Invalid disk model or serial")
    return CardTarget(
        device, numbers[0], size, diskseq, model or "", serial or "", tuple(numbers),
        tuple(dict.fromkeys(mountpoints)),
    )


def validate_table(document: dict[str, Any], plan: CardPlan) -> None:
    require(isinstance(document, dict), "Invalid partition table document")
    table = document.get("partitiontable", {})
    require(isinstance(table, dict), "Read-back GPT layout does not match the plan")
    partitions = table.get("partitions", [])
    require(
        isinstance(partitions, list) and table.get("label") == "gpt" and table.get("unit") == "sectors"
        and table.get("sectorsize") == SECTOR_BYTES
        and table.get("firstlba") == PARTITION_START and table.get("lastlba") == plan.last_sector
        and len(partitions) == 1,
        "Read-back GPT layout does not match the plan",
    )
    part = partitions[0]
    require(isinstance(part, dict), "Read-back partition does not match the plan")
    partition_type = part.get("type", "")
    require(
        part.get("start") == PARTITION_START and part.get("size") == plan.partition_sectors
        and isinstance(partition_type, str) and partition_type.upper() == BASIC_DATA_TYPE
        and part.get("name") == VOLUME_LABEL,
        "Read-back partition does not match the plan",
    )


class LinuxCardBackend:
    def __init__(self, runner: Runner | None = None) -> None:
        self.runner = runner or Runner()

    def read_mountinfo(self) -> str:
        return Path("/proc/self/mountinfo").read_text()

    def run_command(self, argv: list[str], *, fd: int | None = None, input_data: str | None = None) -> str:
        executable = shutil.which(argv[0], path=TOOL_PATH)
        require(executable is not None, f"Missing system tool: {argv[0]}")
        return self.runner.run(
            [executable, *argv[1:]], cwd=Path("/"),
            env={"PATH": TOOL_PATH, "LC_ALL": "C"}, input_data=input_data,
            pass_fds=() if fd is None else (fd,),
        ).stdout

    def inspect(self, device: str) -> CardTarget:
        require(sys.platform == "linux" and struct.calcsize("P") == 8, "card init requires 64-bit Linux")
        validate_device_name(device)
        before = os.lstat(device)
        require(stat.S_ISBLK(before.st_mode), "Target must be a block device, not a link or regular file")
        base = Path("/sys/class/block") / Path(device).name
        sequence = read_sysfs_number(base / "diskseq")
        listing = parse_command_document(self.run_command([
            "lsblk", "--json", "--tree", "--bytes", "--output",
            "PATH,TYPE,SIZE,RM,RO,TRAN,MAJ:MIN,MODEL,SERIAL,MOUNTPOINTS", device,
        ]), command="lsblk")
        disks = listing.get("blockdevices")
        require(isinstance(disks, list) and len(disks) == 1, "Ambiguous lsblk result")
        disk = disks[0]
        target = validate_listing(disk, device, sequence)
        require(read_sysfs_number(base / "queue/logical_block_size") == SECTOR_BYTES, "Expected 512-byte sectors")

        if device.startswith("/dev/mmcblk"):
            require((base / "device/type").read_text().strip() == "SD", "eMMC is not an SD card")

        for item in [disk, *disk.get("children", [])]:
            node = Path("/sys/class/block") / Path(item["path"]).name
            require(not any((node / "holders").iterdir()), "Card has active holders")
            require((node / "dev").read_text().strip() == item["maj:min"], "sysfs device number mismatch")

            if node != base:
                require(node.resolve().parent == base.resolve(), "Partition does not belong to the card")

        for line in Path("/proc/swaps").read_text().splitlines()[1:]:
            fields = line.split()
            require(len(fields) >= 5, "Invalid Linux swap record")
            path = re.sub(r"\\([0-7]{3})", lambda match: chr(int(match[1], 8)), fields[0])
            info = os.stat(path)
            number = info.st_rdev if stat.S_ISBLK(info.st_mode) else info.st_dev
            require(format_device_number(number) not in target.device_numbers, "Card is used for swap")

        after = os.lstat(device)
        require(
            stat.S_ISBLK(after.st_mode) and before.st_rdev == after.st_rdev
            and format_device_number(after.st_rdev) == target.major_minor
            and read_sysfs_number(base / "diskseq") == sequence,
            "Card changed during inspection",
        )
        return target

    def validate_initialization(self, target: CardTarget, journal: Path) -> None:
        require(os.geteuid() == 0, "Writing requires root; run the explicit command in your own terminal")
        require(
            os.stat("/proc/self/ns/mnt").st_ino == os.stat("/proc/1/ns/mnt").st_ino,
            "Run card init in the host mount namespace, not a container/private mount namespace",
        )

        for tool, package in CARD_PACKAGES.items():
            require(
                shutil.which(tool, path=TOOL_PATH) is not None,
                f"Missing system tool: {tool} (Ubuntu/Debian package: {package}); run iroll card deps",
            )

        require(
            "exfat" in Path("/proc/filesystems").read_text().split(),
            "Load the host exfat module before initialization",
        )
        require(Path("/dev/loop-control").exists(), "The host loop driver is required")
        validate_journal_parent(journal, target, self.read_mountinfo())

    def list_mounted_paths(self, target: CardTarget) -> tuple[str, ...]:
        records = parse_mount_records(self.read_mountinfo())
        return tuple(dict.fromkeys(path for number, path in records if number in target.device_numbers))

    def unmount(self, target: CardTarget) -> tuple[str, ...]:
        validate_same_card(target, self.inspect(target.device))
        mounted = tuple(sorted(
            self.list_mounted_paths(target), key=lambda path: (path.count("/"), path), reverse=True,
        ))

        for path in mounted:
            validate_same_card(target, self.inspect(target.device))
            require(path in self.list_mounted_paths(target), f"Card mount changed before unmount: {path}")
            self.run_command(["umount", "--", path])
            require(path not in self.list_mounted_paths(target), f"Unmount not confirmed: {path}")

        validate_same_card(target, self.inspect(target.device))
        require(not self.list_mounted_paths(target), "Card is still mounted after unmount")
        return mounted

    @contextmanager
    def open(self, target: CardTarget) -> Iterator[int]:
        fd = os.open(target.device, os.O_RDWR | os.O_EXCL | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.guard(fd, target)
            validate_same_card(target, self.inspect(target.device))
            yield fd
        finally:
            os.close(fd)

    def guard(self, fd: int, target: CardTarget) -> None:
        info = os.fstat(fd)
        path = os.lstat(target.device)
        require(
            stat.S_ISBLK(info.st_mode) and stat.S_ISBLK(path.st_mode)
            and path.st_rdev == info.st_rdev and format_device_number(info.st_rdev) == target.major_minor,
            "Pinned device or path changed",
        )
        require(
            (read_ioctl_value(fd, BLKGETSIZE64, "=Q"), read_ioctl_value(fd, BLKGETDISKSEQ, "=Q"),
             read_ioctl_value(fd, BLKSSZGET, "=I"), read_ioctl_value(fd, BLKROGET, "=I"))
            == (target.size, target.diskseq, SECTOR_BYTES, 0),
            "Opened card size, diskseq, sector size or write protection changed",
        )

    def partition(self, fd: int, plan: CardPlan) -> None:
        self.guard(fd, plan.target)
        self.run_command([
            "sfdisk", "--no-reread", "--no-tell-kernel", "--wipe", "always",
            "--wipe-partitions", "always", f"/proc/self/fd/{fd}",
        ], fd=fd, input_data=plan.build_sfdisk_input())
        # Remove stale boot code in the alignment gap, without touching either GPT.
        offset = 34 * SECTOR_BYTES
        zeros = bytes(PARTITION_START * SECTOR_BYTES - offset)
        require(os.pwrite(fd, zeros, offset) == len(zeros), "Short write while clearing the old boot gap")
        os.fsync(fd)
        self.verify_table(fd, plan)

    def verify_table(self, fd: int, plan: CardPlan) -> None:
        self.guard(fd, plan.target)
        self.run_command(["sfdisk", "--verify", f"/proc/self/fd/{fd}"], fd=fd)
        document = parse_command_document(
            self.run_command(["sfdisk", "--json", f"/proc/self/fd/{fd}"], fd=fd), command="sfdisk",
        )
        validate_table(document, plan)

    @contextmanager
    def open_volume(self, disk_fd: int, plan: CardPlan, journal: CardJournal) -> Iterator["CardVolume"]:
        control = os.open("/dev/loop-control", os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW)
        try:
            number = fcntl.ioctl(control, LOOP_CTL_GET_FREE)
        finally:
            os.close(control)
        loop = Path(f"/dev/loop{number}")
        fd = os.open(loop, os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW)
        configured = False
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            require(stat.S_ISBLK(os.fstat(fd).st_mode), "Loop path is not a block device")
            info = LOOP_INFO.pack(
                0, 0, 0, PARTITION_START * SECTOR_BYTES, plan.partition_sectors * SECTOR_BYTES,
                0, 0, 0, LO_FLAGS_AUTOCLEAR, b"iroll-card", b"", b"", 0, 0,
            )
            journal.record_event("loop_intent", loop=f"{loop}")
            # Atomic configure; a racing allocation fails. No automatic retry.
            fcntl.ioctl(fd, LOOP_CONFIGURE, struct.pack("=II", disk_fd, SECTOR_BYTES) + info + bytes(64))
            configured = True
            volume = CardVolume(self, disk_fd, fd, loop, plan)
            volume.guard()
            yield volume
        finally:
            try:

                if configured:
                    # No lazy detach on a failed unmount: the caller must inspect it.
                    mounted = any(
                        number == format_device_number(os.fstat(fd).st_rdev)
                        for number, _ in parse_mount_records(self.read_mountinfo())
                    )
                    require(not mounted, f"Volume is still mounted: {loop}; inspect it before removing the card")
                    fcntl.ioctl(fd, LOOP_CLR_FD)
            finally:
                os.close(fd)

    def finish(self, fd: int, plan: CardPlan) -> None:
        self.guard(fd, plan.target)
        os.fsync(fd)
        fcntl.ioctl(fd, BLKFLSBUF)
        self.verify_table(fd, plan)
        validate_same_card(plan.target, self.inspect(plan.target.device))


class CardVolume:
    def __init__(self, backend: LinuxCardBackend, disk_fd: int, fd: int, path: Path, plan: CardPlan) -> None:
        self.backend = backend
        self.disk_fd = disk_fd
        self.fd = fd
        self.path = path
        self.plan = plan

    def guard(self) -> None:
        self.backend.guard(self.disk_fd, self.plan.target)
        data = bytearray(LOOP_INFO.size)
        fcntl.ioctl(self.fd, LOOP_GET_STATUS64, data, True)
        info = LOOP_INFO.unpack(data)
        disk = os.fstat(self.disk_fd)
        require(
            info[:5] == (disk.st_dev, disk.st_ino, disk.st_rdev,
                         PARTITION_START * SECTOR_BYTES, self.plan.partition_sectors * SECTOR_BYTES)
            and info[6:9] == (0, 0, LO_FLAGS_AUTOCLEAR),
            "Loop backing device, offset, size or flags changed",
        )
        require(os.lstat(self.path).st_rdev == os.fstat(self.fd).st_rdev, "Loop device path changed")

    def format(self) -> None:
        self.guard()
        number = format_device_number(os.fstat(self.fd).st_rdev)
        require(
            not any(device == number for device, _ in parse_mount_records(self.backend.read_mountinfo())),
            "Private loop volume is unexpectedly mounted",
        )
        self.backend.run_command(["mkfs.exfat", "-L", VOLUME_LABEL, f"/proc/self/fd/{self.fd}"], fd=self.fd)
        os.fsync(self.fd)
        self.guard()

    def copy_instructions(self, content: bytes, journal: CardJournal) -> str:
        self.guard()
        mountpoint = Path(tempfile.mkdtemp(prefix="iroll-card-", dir="/run"))
        number = format_device_number(os.fstat(self.fd).st_rdev)
        try:
            journal.record_event("mount_intent", loop=f"{self.path}", mountpoint=f"{mountpoint}")
            self.backend.run_command([
                "mount", "--no-mtab", "--internal-only", "-t", "exfat", "-o", "nosuid,nodev,noexec",
                f"/proc/self/fd/{self.fd}", f"{mountpoint}",
            ], fd=self.fd)
            self.guard()
            records = parse_mount_records(self.backend.read_mountinfo())
            require((number, f"{mountpoint}") in records, "Mount does not belong to the prepared volume")
            require(not any(mountpoint.iterdir()), "New exFAT filesystem is unexpectedly nonempty")
            prepare_save_directories(mountpoint)
            destination = mountpoint / INSTRUCTIONS_NAME

            with destination.open("xb") as stream:
                require(stream.write(content) == len(content), "Short instruction-file write")
                stream.flush()
                os.fsync(stream.fileno())

            sync_directory(mountpoint)
            require(destination.read_bytes() == content, "Instruction-file readback mismatch")
            return hashlib.sha256(content).hexdigest()
        finally:
            records = parse_mount_records(self.backend.read_mountinfo())
            mounted = [(device, path) for device, path in records if path == f"{mountpoint}"]

            if mounted:
                require(mounted == [(number, f"{mountpoint}")], f"Unexpected mount at {mountpoint}; refusing cleanup")
                # Also runs after a cancelled mount command that completed its syscall.
                self.backend.run_command(["umount", "--internal-only", f"{mountpoint}"])

            require(
                not any(path == f"{mountpoint}" for _, path in parse_mount_records(self.backend.read_mountinfo())),
                f"Unmount not confirmed: {mountpoint}",
            )
            mountpoint.rmdir()
