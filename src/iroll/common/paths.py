"""Filesystem layout and defaults. No checkout discovery at import time.

SPDX-License-Identifier: BSD-3-Clause
"""

from pathlib import Path

from .files import ensure_directory

DEFAULT_SOURCE = Path("in/components")
DEFAULT_BIOS = Path("in/bios")
DEFAULT_DEST = Path("out")
DEFAULT_DOWNLOADS = Path("in/downloads")
DEFAULT_BUILD_DIR = Path("tmp")
DEFAULT_LOGS_DIR = Path("logs")
DEFAULT_SDK_ROOT = Path("in/sdk")
SYSTEM_DIR = "system"
BIOS_DIR = "bios"
STATES_DIR = "games/states"
SAVES_DIR = "games/saves"
SAVE_DIRECTORIES = (STATES_DIR, SAVES_DIR)
CORES_DIR = "retroarch/cores"
RUNTIME_MANIFEST = "iman/emulation.json"
STAGE_PREFIX = ".iroll-stage-"
BACKUP_PREFIX = ".iroll-backup-"
BUILD_DIR = "build"
OUR_BUILD_DIR = "build/our"
THEIR_SOURCE_DIR = "build/their/src"
THEIR_COMPILED_DIR = "build/their/compiled"
OUT_STAGE_DIR = "out_stage"
REPORT_FILE = "result.json"
FAILURE_FILE = "failure.json"
CARD_JOURNAL_FILE = "journal.jsonl"
CARGO_MANIFEST = "Cargo.toml"
CARGO_LOCK = "Cargo.lock"
RELEASE_DIR = "release"
OS_RELEASE = Path("/etc/os-release")
SYSROOT_PKGCONFIG_DIRS = (
    "usr/lib/arm-linux-gnueabihf/pkgconfig",
    "usr/lib/pkgconfig",
    "usr/share/pkgconfig",
    "lib/arm-linux-gnueabihf/pkgconfig",
    "lib/pkgconfig",
)


def get_sdk_pending_path(sdk: Path) -> Path:
    """Sibling marker prevents consumers from using an interrupted preparation."""
    return sdk.with_name(f".{sdk.name}.iroll-preparing")


def prepare_save_directories(dest: Path) -> None:

    for relative in SAVE_DIRECTORIES:
        ensure_directory(dest / relative)
