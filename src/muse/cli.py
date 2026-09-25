"""Muse command-line interface."""

from __future__ import annotations

import argparse
import os
import platform
import shutil
import sys
from collections.abc import Sequence
from functools import partial
from pathlib import Path
from typing import Any

from rich_argparse import RichHelpFormatter

from muse import __version__
from muse.config import MANAGED_AREAS, MASTER_SHELVES, resolve_root, resolve_target
from muse.reporting import emit_json, human_bytes, make_console, print_table, status_text
from muse.repository import PathStats, scan_path, scan_root_by_area


class MuseHelpFormatter(RichHelpFormatter):
    """Rich help with prominent command and option names."""

    styles = {
        **RichHelpFormatter.styles,
        "argparse.args": "bold cyan",
        "argparse.groups": "bold",
        "argparse.metavar": "cyan",
    }

    def _rich_format_action(self, action: argparse.Action):
        """Avoid repeating the subparser metavar under the Commands heading."""
        if isinstance(action, argparse._SubParsersAction):  # noqa: SLF001
            for subaction in self._iter_indented_subactions(action):
                yield from self._rich_format_action(subaction)
            return
        yield from super()._rich_format_action(action)


PLANNED_COMMANDS = {
    "backlog": ("add", "scan", "status", "overlap", "unique", "browse", "verify", "compact"),
    "integrity": ("scan", "status", "verify", "inspect", "accept"),
    "stopgap": ("add", "status", "publish"),
    "apple": ("status", "diff", "sync", "duplicates", "report", "query"),
    "publish": ("apple",),
}


def _add_json_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")


def build_parser(color: str = "auto") -> argparse.ArgumentParser:
    formatter = partial(MuseHelpFormatter, console=make_console(color))
    parser = argparse.ArgumentParser(
        prog="muse",
        description="Curate and publish a personal music archive.",
        formatter_class=formatter,
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "--root",
        metavar="PATH",
        help="library root (default: ~/music-vault)",
    )
    parser.add_argument(
        "--color",
        choices=("auto", "always", "never"),
        default="auto",
        help="terminal color policy (default: auto)",
    )

    commands = parser.add_subparsers(
        dest="command",
        title="Commands",
        metavar="COMMAND",
        parser_class=partial(argparse.ArgumentParser, formatter_class=formatter),
    )

    doctor = commands.add_parser("doctor", help="inspect repository layout and supporting tools")
    _add_json_argument(doctor)
    doctor.set_defaults(handler=_doctor)

    status = commands.add_parser("status", help="show top-level repository status")
    _add_json_argument(status)
    status.set_defaults(handler=_status)

    stats = commands.add_parser("stats", help="report file counts and disk usage")
    stats.add_argument(
        "target",
        nargs="?",
        help="path to inspect; relative paths are resolved beneath the library root",
    )
    _add_json_argument(stats)
    stats.set_defaults(handler=_stats)

    for name, actions in PLANNED_COMMANDS.items():
        planned = commands.add_parser(name, help=f"planned {name} operations (not implemented)")
        planned.add_argument("action", nargs="?", choices=actions)
        planned.add_argument("arguments", nargs=argparse.REMAINDER)
        planned.set_defaults(handler=_not_implemented)

    return parser


def _check(path: Path, *, expected: str = "directory") -> dict[str, str]:
    if not path.exists():
        return {"path": str(path), "status": "error", "detail": "missing"}
    if expected == "directory" and not path.is_dir():
        return {"path": str(path), "status": "error", "detail": "not a directory"}
    if not os.access(path, os.R_OK):
        return {"path": str(path), "status": "error", "detail": "not readable"}
    return {"path": str(path), "status": "ok", "detail": "readable"}


def _doctor(args: argparse.Namespace, root: Path) -> int:
    checks = [_check(root)]
    checks.extend(_check(root / area) for area in MANAGED_AREAS)
    checks.extend(_check(root / "master" / shelf) for shelf in MASTER_SHELVES)

    tools = []
    for name, required_for in (
        ("beet", "canonical catalogue"),
        ("ffmpeg", "audio validation and conversion"),
        ("ffprobe", "technical inspection"),
    ):
        executable = shutil.which(name)
        tools.append(
            {
                "name": name,
                "status": "available" if executable else "unavailable",
                "path": executable,
                "purpose": required_for,
            }
        )

    result: dict[str, Any] = {
        "root": str(root),
        "platform": platform.platform(),
        "python": platform.python_version(),
        "layout": checks,
        "tools": tools,
        "state_created": False,
    }
    failed = any(check["status"] == "error" for check in checks)

    if args.json:
        emit_json(result)
    else:
        console = make_console(args.color)
        console.print("[bold]Muse doctor[/bold]\n")
        console.print("[bold]Root:[/bold]", str(root), style=None)
        console.print("[bold]Platform:[/bold]", result["platform"], style=None)
        console.print("[bold]Python:[/bold]", result["python"], style=None)
        console.print("\n[bold]Layout[/bold]")
        layout_rows = [
            (status_text(check["status"]), check["path"], check["detail"]) for check in checks
        ]
        print_table(console, ("STATUS", "PATH", "DETAIL"), layout_rows)

        console.print("[bold]Supporting tools[/bold] [dim](optional for read-only commands)[/dim]")
        tool_rows = [
            (
                status_text(tool["status"]),
                tool["name"],
                tool["path"] or "not found",
                tool["purpose"],
            )
            for tool in tools
        ]
        print_table(console, ("STATUS", "TOOL", "PATH", "PURPOSE"), tool_rows)
        console.print("[dim]No Muse state was created or changed.[/dim]")

    return 1 if failed else 0


def _status(args: argparse.Namespace, root: Path) -> int:
    root_state = _check(root)
    areas = []
    for name in MANAGED_AREAS:
        path = root / name
        check = _check(path)
        areas.append({"name": name, **check})

    result = {
        "root": str(root),
        "root_status": root_state["status"],
        "areas": areas,
        "state_created": False,
    }
    failed = root_state["status"] == "error" or any(area["status"] == "error" for area in areas)

    if args.json:
        emit_json(result)
    else:
        console = make_console(args.color)
        console.print("[bold]Muse repository[/bold]")
        console.print("[dim]Root[/dim]", str(root), "\n")
        rows = [
            (area["name"], status_text(area["status"]), area["detail"], area["path"])
            for area in areas
        ]
        print_table(console, ("AREA", "STATUS", "DETAIL", "PATH"), rows)
        console.print("[dim]Read-only inspection; no Muse state was created or changed.[/dim]")

    return 1 if failed else 0


def _stats_row(name: str, stats: PathStats) -> tuple[str, ...]:
    allocated = stats.allocated_bytes if stats.allocated_bytes_available else None
    return (
        name,
        str(stats.files),
        str(stats.audio_files),
        str(stats.non_audio_files),
        str(stats.directories),
        str(stats.symlinks),
        human_bytes(stats.logical_bytes),
        human_bytes(allocated),
        str(len(stats.errors)),
    )


def _stats(args: argparse.Namespace, root: Path) -> int:
    target = resolve_target(root, args.target)
    explicit_target = args.target is not None

    if explicit_target:
        total = scan_path(target)
        areas: dict[str, PathStats] = {}
    elif not root.exists() or not root.is_dir():
        total = scan_path(root)
        areas = {}
    else:
        total, areas = scan_root_by_area(root)

    result = {
        "root": str(root),
        "target": str(target),
        "total": total.to_dict(),
        "areas": {name: value.to_dict() for name, value in sorted(areas.items())},
        "state_created": False,
    }

    if args.json:
        emit_json(result)
    else:
        console = make_console(args.color)
        console.print("[bold]Muse statistics[/bold]")
        console.print("[dim]Target[/dim]", str(target), "\n")
        rows = [_stats_row(name, value) for name, value in sorted(areas.items())]
        rows.append(_stats_row("TOTAL", total))
        print_table(
            console,
            (
                "AREA",
                "FILES",
                "AUDIO",
                "OTHER",
                "DIRS",
                "LINKS",
                "LOGICAL",
                "ALLOCATED",
                "ERRORS",
            ),
            rows,
            right_aligned=frozenset(
                {"FILES", "AUDIO", "OTHER", "DIRS", "LINKS", "LOGICAL", "ALLOCATED", "ERRORS"}
            ),
            bold_rows=frozenset({"TOTAL"}),
        )

        if total.extensions:
            console.print("[bold]Extensions[/bold]")
            extension_rows = [
                (extension, str(count))
                for extension, count in sorted(
                    total.extensions.items(), key=lambda item: (-item[1], item[0])
                )
            ]
            print_table(
                console,
                ("EXTENSION", "FILES"),
                extension_rows,
                right_aligned=frozenset({"FILES"}),
            )

        if total.errors:
            error_console = make_console(args.color, stderr=True)
            error_console.print("[bold red]Scan errors[/bold red]")
            for error in total.errors:
                error_console.print(f"  [red]{error.path}:[/red] {error.message}")

        console.print("[dim]Read-only scan; no Muse state was created or changed.[/dim]")

    return 1 if total.errors else 0


def _not_implemented(args: argparse.Namespace, root: Path) -> int:
    action = f" {args.action}" if args.action else ""
    console = make_console(args.color, stderr=True)
    console.print(
        f"[bold yellow]Not implemented yet:[/bold yellow] muse {args.command}{action}; "
        "no changes were made"
    )
    return 2


def _help_color(argv: Sequence[str]) -> str:
    """Read the color preference before argparse can handle an early --help."""
    for index, argument in enumerate(argv):
        if argument.startswith("--color="):
            return argument.partition("=")[2]
        if argument == "--color" and index + 1 < len(argv):
            return argv[index + 1]
    return "auto"


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser(_help_color(arguments))
    args = parser.parse_args(arguments)
    if args.command is None:
        parser.print_help()
        return 0
    root = resolve_root(args.root)
    return args.handler(args, root)
