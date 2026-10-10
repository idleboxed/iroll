"""Card identity and the single-partition layout, without hardware access.

SPDX-License-Identifier: BSD-3-Clause
"""

import re
from dataclasses import asdict, dataclass, field
from typing import Any

from ..common.errors import BuildError
from ..common.paths import SAVE_DIRECTORIES

SECTOR_BYTES = 512
PARTITION_START = 2048
BASIC_DATA_TYPE = "EBD0A0A2-B9E5-4433-87C0-68B6B72699C7"
VOLUME_LABEL = "idlebox"
INSTRUCTIONS_NAME = "INSTRUCTIONS.md"


class CardError(BuildError):
    """Refuse an unsafe operation or report an incomplete initialization."""


def require(condition: bool, message: str) -> None:

    if not condition:
        raise CardError(message)


def validate_device_name(device: str) -> None:
    require(
        re.fullmatch(r"/dev/(?:sd[a-z]+|mmcblk[0-9]+)", device) is not None,
        "Specify a whole removable disk: /dev/sdX or /dev/mmcblkN; links and partitions are forbidden",
    )


@dataclass(frozen=True)
class CardTarget:
    device: str
    major_minor: str
    size: int
    diskseq: int
    model: str
    serial: str
    device_numbers: tuple[str, ...]
    mountpoints: tuple[str, ...] = field(default=(), compare=False)

    def __post_init__(self) -> None:
        validate_device_name(self.device)
        require(
            self.size != 0,
            "Card reader reports no media (0 bytes); unmount partitions without ejecting or powering off the reader",
        )
        require(64 * 1024**2 <= self.size <= 2 * 1024**4, "Supported card size is 64 MiB through 2 TiB")
        require(self.size % SECTOR_BYTES == 0, "Card size is not a multiple of 512 bytes")
        require(self.diskseq > 0, "A positive kernel diskseq is required")
        require(self.major_minor in self.device_numbers, "Missing whole-disk device number")


@dataclass(frozen=True)
class CardPlan:
    target: CardTarget

    @property
    def last_sector(self) -> int:
        return self.target.size // SECTOR_BYTES - 34

    @property
    def partition_sectors(self) -> int:
        return self.last_sector - PARTITION_START + 1

    def build_sfdisk_input(self) -> str:
        return (
            "label: gpt\nunit: sectors\n"
            f"first-lba: {PARTITION_START}\nlast-lba: {self.last_sector}\n\n"
            f"start={PARTITION_START}, size={self.partition_sectors}, "
            f"type={BASIC_DATA_TYPE}, name=\"{VOLUME_LABEL}\"\n"
        )

    def build_document(self) -> dict[str, Any]:
        return {
            "target": {
                **asdict(self.target),
                "device_numbers": list(self.target.device_numbers),
                "mountpoints": list(self.target.mountpoints),
            },
            "partition_table": "gpt",
            "partition": {
                "number": 1,
                "start_sector": PARTITION_START,
                "sectors": self.partition_sectors,
                "sector_bytes": SECTOR_BYTES,
                "filesystem": "exfat",
                "label": VOLUME_LABEL,
            },
            "files": [INSTRUCTIONS_NAME],
            "directories": list(SAVE_DIRECTORIES),
            "all_existing_data_will_be_lost": True,
            "secure_erase": False,
            "confirmation": "y/Y",
            "confirmation_default": "n",
        }


def validate_same_card(expected: CardTarget, actual: CardTarget) -> None:
    require(expected == actual, "Card identity, diskseq, size or device topology changed; inspect again")
