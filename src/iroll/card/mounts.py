"""Validated Linux mountinfo shared by card guards and journal checks.

SPDX-License-Identifier: BSD-3-Clause
"""

import re

from .layout import require


def _parse_mountinfo(text: str) -> list[tuple[str, str, str]]:
    records = []

    for line in text.splitlines():
        before, separator, after = line.partition(" - ")
        fields = before.split()
        filesystem = after.split()
        require(
            bool(separator) and len(fields) >= 6 and len(filesystem) >= 3,
            "Invalid Linux mountinfo record",
        )
        number = fields[2]
        mountpoint = re.sub(r"\\([0-7]{3})", lambda match: chr(int(match[1], 8)), fields[4])
        require(
            re.fullmatch(r"[0-9]+:[0-9]+", number) is not None and mountpoint.startswith("/"),
            "Invalid Linux mountinfo device or mountpoint",
        )
        records.append((number, mountpoint, filesystem[0]))

    return records


def parse_mount_records(text: str) -> list[tuple[str, str]]:
    return [(number, mountpoint) for number, mountpoint, _ in _parse_mountinfo(text)]


def get_filesystem_types(text: str, device_number: str) -> tuple[str, ...]:
    return tuple(filesystem for number, _, filesystem in _parse_mountinfo(text) if number == device_number)
