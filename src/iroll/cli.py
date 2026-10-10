"""Arguments, logging, presentation and exit codes only.

SPDX-License-Identifier: BSD-3-Clause
"""

import argparse
import json
import logging
import os
import shutil
import signal
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path

from . import __version__
from .host import dependencies
from .common import paths
from .rollout.build import BuildOptions, Builder
from .card.service import CardInitializer
from .card.layout import VOLUME_LABEL, CardPlan
from .common.errors import BuildError
from .common.files import create_report, write_json
from .host.platforms import DEFAULT_PLATFORM, PROFILES
from .sdk.discovery import discover_sdk
from .common.process import Runner
from .components.registry import COMPONENTS, select
from .sdk.prepare import SdkOptions, SdkPreparer

logger = logging.getLogger(__name__)

COMMAND_DIRS = {
    ("sdk", "prepare"): "sdk-prepare",
    ("card", "init"): "card-init",
    ("card", "deps"): "card-deps",
    ("rollout", None): "rollout",
    ("deps", None): "deps",
    ("list", None): "list",
}


def read_sudo_owner() -> tuple[int, int] | None:

    if os.geteuid() != 0:
        return None

    uid = os.environ.get("SUDO_UID")
    gid = os.environ.get("SUDO_GID")

    if uid is None and gid is None:
        return None

    if uid is None or gid is None or not uid.isdecimal() or not gid.isdecimal():
        raise BuildError("Invalid original user identity from sudo")

    return int(uid), int(gid)


def resolve_command_executable() -> str:
    invoked = sys.argv[0]
    path = f"{Path(invoked).absolute()}" if "/" in invoked else shutil.which(invoked)

    if path is None:
        path = shutil.which("iroll")

    if path is None:
        raise BuildError("Cannot find the installed iroll command for automatic sudo")

    return path


def elevate_card_init(args: argparse.Namespace, argv: list[str]) -> None:

    if args.command != "card" or args.card_command != "init" or args.dry_run or os.geteuid() == 0:
        return

    if not sys.stdin.isatty():
        raise BuildError("Card initialization requires an interactive terminal for sudo and confirmation")

    sudo = shutil.which("sudo")

    if sudo is None:
        raise BuildError("Missing sudo; install it or run the command as root")

    os.execv(sudo, [sudo, "--", resolve_command_executable(), *argv])
    raise AssertionError("os.execv returned unexpectedly")


def add_logs_dir(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--logs_dir",
        type=Path,
        default=paths.DEFAULT_LOGS_DIR,
        help="Root for per-command logs (default: %(default)s in the current directory)",
    )


def get_command_log_root(args: argparse.Namespace) -> Path:
    subcommand = getattr(args, f"{args.command}_command", None)
    return args.logs_dir.absolute() / COMMAND_DIRS[(args.command, subcommand)]


@contextmanager
def configure_cli_logging(verbose: bool) -> Iterator[None]:
    package = logging.getLogger("iroll")
    handler = None
    old_level = package.level
    configured = any(not isinstance(handler_, logging.NullHandler) for handler_ in package.handlers)
    configured |= package.propagate and logging.getLogger().hasHandlers()

    if not configured:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
        package.addHandler(handler)
        package.setLevel(logging.DEBUG if verbose else logging.INFO)

    try:
        yield
    finally:

        if handler:
            package.removeHandler(handler)
            handler.close()
            package.setLevel(old_level)


def create_parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        prog="iroll",
        description="Prepare SDKs, build system components or initialize an SD data card; never launch apps.",
    )
    result.add_argument("--version", action="version", version=f"iroll {__version__}")
    sub = result.add_subparsers(dest="command", required=True)
    sdk = sub.add_parser("sdk", help="Prepare a host SDK from pinned archives and a local NOR file")
    sdk_sub = sdk.add_subparsers(dest="sdk_command", required=True)
    prepare = sdk_sub.add_parser(
        "prepare", help="Prepare or verify RK3032 SDK; never access hardware or replace an SDK",
    )
    prepare.add_argument("--platform", choices=("rk3032",), default="rk3032")
    prepare.add_argument("--nor", type=Path, required=True, help="Preserved physical NOR file; never a device")
    prepare.add_argument(
        "--sdk", type=Path, default=paths.DEFAULT_SDK_ROOT / "rk3032", help="SDK destination (default: %(default)s)",
    )
    prepare.add_argument(
        "--downloads", type=Path, default=paths.DEFAULT_DOWNLOADS,
        help="Download root; SDK archives use sdk/rk3032/ below it (default: %(default)s)",
    )
    add_logs_dir(prepare)
    prepare.add_argument("--json", action="store_true", help="Write machine-readable result to stdout")
    prepare.add_argument("--verbose", action="store_true")
    card = sub.add_parser("card", help="Explicit card operations, separate from building applications")
    card_sub = card.add_subparsers(dest="card_command", required=True)
    init = card_sub.add_parser("init", help="DESTROY ALL DATA; create one GPT/exFAT partition and INSTRUCTIONS.md")
    init.add_argument("device", help="Whole removable disk, not a partition (for example /dev/sdX)")
    init.add_argument("--dry-run", action="store_true", help="Inspect and show the plan without writing")
    add_logs_dir(init)
    init.add_argument("--json", action="store_true", help="Write machine-readable result to stdout")
    init.add_argument("--verbose", action="store_true")
    card_deps = card_sub.add_parser("deps", help="Check card tool dependencies; no card/root required")
    card_deps.add_argument("--json", action="store_true", help="Write machine-readable result to stdout")
    card_deps.add_argument("--verbose", action="store_true")
    add_logs_dir(card_deps)

    for command, help_text in (
        ("rollout", "Build selected components and publish DEST/system"),
        ("deps", "Check tools/SDK and print installation suggestions; never install"),
        ("list", "List component support for a platform"),
    ):
        child = sub.add_parser(command, help=help_text)
        child.add_argument("--platform", choices=tuple(PROFILES), default=DEFAULT_PLATFORM)
        child.add_argument("--json", action="store_true", help="Write machine-readable data to stdout")
        child.add_argument("--verbose", action="store_true")
        add_logs_dir(child)

        if command == "list":
            continue

        for component in COMPONENTS:

            if not component.cli_selectable:
                continue

            child.add_argument(
                f"--with-{component.name}",
                action="append_const",
                const=component.name,
                dest="components",
                help=f"Select {component.name}",
            )

        child.add_argument("--with-cores", action="store_true", help="Select all supported cores")
        child.add_argument(
            "--sdk",
            type=Path,
            help=f"ARM SDK root (rk3032 default: ./{paths.DEFAULT_SDK_ROOT}/rk3032 in the current directory)",
        )

        if command == "deps":
            continue

        child.add_argument(
            "--source",
            type=Path,
            default=paths.DEFAULT_SOURCE,
            help="Local igui/iman directories or symlinks (default: %(default)s in the current directory)",
        )
        child.add_argument(
            "--bios",
            type=Path,
            default=paths.DEFAULT_BIOS,
            help="Optional user BIOS root with PLATFORM/file entries (default: %(default)s)",
        )
        child.add_argument(
            "--dest",
            type=Path,
            default=paths.DEFAULT_DEST,
            help="Output root, not system itself (default: %(default)s)",
        )
        child.add_argument(
            "--downloads",
            type=Path,
            default=paths.DEFAULT_DOWNLOADS,
            help="Pinned source archives only (default: %(default)s in the current directory)",
        )
        child.add_argument(
            "--build-dir",
            type=Path,
            default=paths.DEFAULT_BUILD_DIR,
            help="PC-only build/ and out_stage/ working root (default: %(default)s in the current directory)",
        )
        child.add_argument("--jobs", type=int, default=4, help="Parallel compiler jobs (1..256; default: 4)")
        child.add_argument(
            "--no-diag", action="store_true",
            help="Build iman without passive diagnostics (RK3032 default: enabled); keep release optimization",
        )
        child.add_argument(
            "--ask", action="store_true",
            help="Ask before replacing each changed existing release file (default: replace)",
        )

    return result


def output_json(value: object) -> None:
    sys.stdout.write(json.dumps(value, indent=2) + "\n")


def output_dependency_report(report: dependencies.DependencyReport, *, json_output: bool) -> int:

    if json_output:
        output_json({**asdict(report), "ready": report.ready})

    else:

        for message in report.checks:
            logger.info("%s", message)

        for message in report.missing:
            logger.error("%s", message)

        for message in report.instructions:
            logger.info("%s", message)

    return 0 if report.ready else 1


def confirm(prompt: str, *, terminal_error: str) -> bool:

    if not sys.stdin.isatty():
        raise BuildError(terminal_error)

    sys.stderr.write(prompt)
    sys.stderr.flush()
    return sys.stdin.readline().rstrip("\r\n") in ("y", "Y")


def confirm_replace(path: Path) -> bool:
    display_path = f"{path}"
    return confirm(
        f"Replace existing file {display_path!r}? [y/N]: ",
        terminal_error="--ask requires an interactive terminal to confirm file replacement",
    )


def confirm_card(plan: CardPlan) -> bool:
    mounted = "".join(f"Mounted path to unmount: {path}\n" for path in plan.target.mountpoints)
    return confirm(
        "\nWARNING: ALL DATA ON THE SELECTED CARD WILL BE LOST.\n"
        "All partitions, games, preferences and saves will be destroyed. No backup is made.\n"
        f"Device: {plan.target.device}; bytes: {plan.target.size}\n"
        f"Model: {plan.target.model!r}; serial: {plan.target.serial!r}\n"
        f"{mounted}"
        "Mounted card filesystems will be unmounted automatically after confirmation.\n"
        "Erase all data and initialize this card? [y/N]: ",
        terminal_error="Card initialization requires an interactive terminal; there is no --yes/--force bypass",
    )


@contextmanager
def handle_cancellation_signals() -> Iterator[None]:
    def cancel(signum: int, frame: object) -> None:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        raise KeyboardInterrupt

    previous = signal.signal(signal.SIGTERM, cancel)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)


def initialize_card(args: argparse.Namespace) -> int:
    initializer = CardInitializer()
    owner = read_sudo_owner()

    with handle_cancellation_signals():
        result = initializer.run(
            args.device, logs_root=get_command_log_root(args), dry_run=args.dry_run, confirm=confirm_card, owner=owner,
        )

    if args.dry_run:
        logger.warning("ALL EXISTING DATA WILL BE LOST on initialization; dry-run made no changes")

        if not args.json:
            target = result["target"]
            logger.info("Target: %s; bytes=%d", target["device"], target["size"])
            logger.info("Model=%r; serial=%r", target["model"], target["serial"])
            logger.info(
                "One GPT/exFAT partition labelled %s; INSTRUCTIONS.md and empty save directories", VOLUME_LABEL,
            )

    else:
        logger.info("Card synchronized and unmounted. Disconnect and reconnect the reader before further use.")

    if args.json:
        output_json(result)

    return 0


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    args = create_parser().parse_args(arguments)

    with configure_cli_logging(args.verbose):
        try:
            elevate_card_init(args, arguments)

            if args.command == "sdk":
                options = SdkOptions(
                    nor=args.nor.absolute(), sdk=args.sdk.absolute(),
                    downloads=args.downloads.absolute(), logs=get_command_log_root(args),
                )

                with handle_cancellation_signals():
                    result = SdkPreparer(options).run()

                if args.json:
                    output_json(result)

                return 0

            if args.command == "card":

                if args.card_command == "deps":
                    report = dependencies.check_card()
                    run = create_report(get_command_log_root(args))
                    write_json(run / paths.REPORT_FILE, {**asdict(report), "ready": report.ready})
                    return output_dependency_report(report, json_output=args.json)

                return initialize_card(args)

            profile = PROFILES[args.platform]

            if args.command == "list":
                rows = [
                    dict(
                        name=component.name,
                        supported=profile.name in component.platforms,
                        flag=f"--with-{component.name}" if component.cli_selectable else None,
                        game_platforms=component.game_platforms,
                        reason=component.unavailable_reason if profile.name not in component.platforms else "",
                    )
                    for component in COMPONENTS
                ]
                document = {"platform": profile.name, "components": rows, "hardware_verified": False}
                run = create_report(get_command_log_root(args))
                write_json(run / paths.REPORT_FILE, document)

                if args.json:
                    output_json(document)

                else:

                    for row in rows:
                        logger.info(
                            "%s: %s%s",
                            row["flag"] or f"--with-cores ({row["name"]})",
                            "available" if row["supported"] else "unavailable",
                            f"; {row["reason"]}" if row["reason"] else "",
                        )

                return 0

            components = select(profile.name, args.components or [], cores=args.with_cores)

            if args.command == "rollout" and args.ask and not sys.stdin.isatty():
                raise BuildError("--ask requires an interactive terminal")

            runner = Runner()
            toolchain = discover_sdk(profile, args.sdk, runner, cwd=Path.cwd())

            if args.command == "deps":
                report = dependencies.check(components, toolchain, runner, cwd=Path.cwd())
                run = create_report(get_command_log_root(args))
                write_json(run / paths.REPORT_FILE, {**asdict(report), "ready": report.ready})
                return output_dependency_report(report, json_output=args.json)

            options = BuildOptions(
                source_root=args.source.absolute(),
                bios=args.bios.absolute(),
                dest=args.dest.absolute(),
                downloads=args.downloads.absolute(),
                build_dir=args.build_dir.absolute(),
                logs=get_command_log_root(args),
                toolchain=toolchain,
                components=components,
                jobs=args.jobs,
                no_diag=args.no_diag,
            )
            logs = Builder(options, runner=runner, confirm_replace=confirm_replace if args.ask else None).run()

            if args.json:
                output_json(
                    {
                        "status": "BUILT_AND_PUBLISHED",
                        "system": f"{options.dest / paths.SYSTEM_DIR}",
                        "reports": f"{logs}",
                    }
                )

            return 0

        except KeyboardInterrupt:

            if args.command == "sdk":
                logger.error("SDK preparation cancelled; inspect its reports. No automatic retry.")

            elif args.command == "card":
                logger.error("Cancelled; inspect the card journal and any remaining mount. No automatic retry.")

            else:
                logger.error("Cancelled; publication may be partial. Inspect build reports. No automatic retry.")

            return 130

        except (BuildError, OSError) as exc:
            logger.error("%s", exc)
            return 1
