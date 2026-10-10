"""Card names and geometry, without device access. SPDX-License-Identifier: BSD-3-Clause."""

from dataclasses import replace

import pytest

from iroll.card.layout import CardError, CardTarget, validate_device_name


@pytest.mark.parametrize("name", ["/dev/sdz", "/dev/sdaa", "/dev/mmcblk0", "/dev/mmcblk10"])
def test_explicit_whole_card_names(name: str) -> None:
    validate_device_name(name)

@pytest.mark.parametrize("name", [
    "/dev/sdz1", "/dev/mmcblk0p1", "/dev/nvme0n1", "/dev/loop0", "/dev/disk/by-id/a", "sdz",
])
def test_rejects_partitions_links_and_unsupported_targets(name: str) -> None:

    with pytest.raises(CardError, match="whole removable disk"):
        validate_device_name(name)

@pytest.mark.parametrize("size", [0, 64 * 1024**2 - 512, 2 * 1024**4 + 512, 64 * 1024**2 + 1])
def test_invalid_capacity(target: CardTarget, size: int) -> None:

    with pytest.raises(CardError, match="no media|size"):
        replace(target, size=size)
