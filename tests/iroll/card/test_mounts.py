"""Synthetic Linux mountinfo, not a query of the host mounts. SPDX-License-Identifier: BSD-3-Clause."""

import pytest

from iroll.card.layout import CardError
from iroll.card.mounts import get_filesystem_types, parse_mount_records


def test_mountinfo_unescapes_spaces_and_backslashes() -> None:
    records = parse_mount_records("1 0 8:241 / /media/a\\040b\\134c rw - exfat /dev/sdz1 rw\n")

    assert records == [("8:241", "/media/a b\\c")]


def test_filesystem_types_include_every_mount_of_the_selected_device() -> None:
    text = (
        "1 0 8:1 / / rw shared:1 - ext4 /dev/sda1 rw\n"
        "2 0 0:3 / /run rw - tmpfs tmpfs rw\n"
        "3 0 8:1 /sub /copy rw - ext4 /dev/sda1 rw\n"
    )

    assert get_filesystem_types(text, "8:1") == ("ext4", "ext4")
    assert get_filesystem_types(text, "0:3") == ("tmpfs",)
    assert get_filesystem_types(text, "8:2") == ()
    assert parse_mount_records("") == []


@pytest.mark.parametrize("text", [
    pytest.param("incomplete", id="short-record"),
    pytest.param("1 0 8:1 / / rw ext4 /dev/sda1 rw", id="missing-separator"),
    pytest.param("1 0 8:1 / / rw - ext4", id="missing-filesystem-fields"),
    pytest.param("1 0 invalid / / rw - ext4 /dev/sda1 rw", id="invalid-device-number"),
    pytest.param("1 0 8:1 / relative rw - ext4 /dev/sda1 rw", id="relative-mountpoint"),
])
def test_malformed_mountinfo_is_an_expected_error_for_both_projections(text: str) -> None:

    with pytest.raises(CardError, match="Invalid Linux mountinfo"):
        parse_mount_records(text)

    with pytest.raises(CardError, match="Invalid Linux mountinfo"):
        get_filesystem_types(text, "8:1")
