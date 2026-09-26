"""Muse command-line interface."""

from __future__ import annotations

import argparse
import os
import platform
import shutil
import sys
from collections.abc import Sequence
from difflib import SequenceMatcher
from functools import partial
from pathlib import Path
from time import monotonic
from typing import Any

from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
    TimeRemainingColumn,
)
from rich.text import Text
from rich_argparse import RichHelpFormatter

from muse import __version__
from muse.cache import CachePruneReport, prune_missing
from muse.compaction import CompactProgress, apply_plan, load_plan, make_plan, save_plan
from muse.config import MANAGED_AREAS, MASTER_SHELVES, resolve_root, resolve_target
from muse.duplicates import DuplicateGroup, DuplicateReport, ProgressUpdate, find_duplicates
from muse.importing import abort_plan as abort_import_plan
from muse.importing import apply_plan as apply_import_plan
from muse.importing import load_plan as load_import_plan
from muse.importing import make_plan as make_import_plan
from muse.moves import move
from muse.reporting import (
    emit_json,
    human_bytes,
    human_duration,
    human_number,
    make_console,
    middle_truncate,
    print_table,
    status_text,
)
from muse.repository import AUDIO_EXTENSIONS, PathStats, scan_path, scan_root_by_area
from muse.scanning import (
    PullProgress,
    ScanComparison,
    ScannedFile,
    ScanProgress,
    compare_with_vault,
    pull_new_directories,
)
from muse.search import search_vault
from muse.slag import apply as apply_slag
from muse.slag import candidates as slag_candidates
from muse.slag import inventory as slag_inventory
from muse.slag import stats as slag_stats
from muse.tree_diff import TreeDiffReport, compare_trees

_NEGATED_SEARCH_TERM = "muse-internal-negated-search-term:"
_STATS_AREA_ORDER = (
    "master",
    "backlog",
    "stopgap",
    "incoming",
    "slag",
    "trash",
    ".muse",
)
_IMAGE_EXTENSIONS = frozenset(
    {
        ".avif",
        ".bmp",
        ".gif",
        ".heic",
        ".heif",
        ".jpeg",
        ".jpg",
        ".png",
        ".svg",
        ".tif",
        ".tiff",
        ".webp",
    }
)
_PLAYLIST_EXTENSIONS = frozenset({".cue", ".m3u", ".m3u8", ".pls", ".xspf"})
_METADATA_EXTENSIONS = frozenset(
    {".log", ".md5", ".nfo", ".sha1", ".sha256", ".sha512", ".sfv", ".txt"}
)


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


def _add_json_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return parsed


def _add_max_threads_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--max-threads",
        type=_positive_int,
        metavar="N",
        help="limit concurrent hashing and scanning workers (default: up to 16)",
    )


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

    def command(name: str, summary: str) -> argparse.ArgumentParser:
        description = f"{summary[:1].upper()}{summary[1:].removesuffix('.')}."
        return commands.add_parser(name, help=summary, description=description)

    doctor = command("doctor", "inspect repository layout and supporting tools")
    _add_json_argument(doctor)
    doctor.set_defaults(handler=_doctor)

    status = command("status", "show top-level repository status")
    _add_json_argument(status)
    status.set_defaults(handler=_status)

    stats = command("stats", "report file counts and disk usage")
    stats.add_argument(
        "target",
        nargs="?",
        help="path to inspect; relative paths are resolved beneath the library root",
    )
    _add_json_argument(stats)
    stats.set_defaults(handler=_stats)

    scan = command("scan", "check whether an external tree contains files absent from the vault")
    scan.add_argument("target", help="external file or directory to inspect")
    scan.add_argument(
        "--thorough",
        action="store_true",
        help="compare SHA-256 checksums instead of filenames and sizes",
    )
    scan.add_argument(
        "--progress",
        choices=("auto", "always", "never"),
        default="auto",
        help="target scan progress display policy (default: auto)",
    )
    scan.add_argument(
        "--all",
        action="store_true",
        help="show every not-found directory and file",
    )
    scan.add_argument(
        "--pull",
        action="store_true",
        help="interactively copy directories containing not-found audio into backlog",
    )
    _add_max_threads_argument(scan)
    _add_json_argument(scan)
    scan.set_defaults(handler=_scan)

    search = command("search", "find files or directories by name across the vault")
    search.add_argument(
        "terms",
        nargs="+",
        metavar="QUERY",
        help="case-insensitive terms; prefix with - to exclude matching paths",
    )
    _add_json_argument(search)
    search.set_defaults(handler=_search)

    dupes = command(
        "dupes", "find byte-identical files; names and locations need not match"
    )
    dupes.add_argument(
        "targets",
        nargs="*",
        help="paths to inspect; relative paths are resolved beneath the library root",
    )
    dupes.add_argument(
        "--rehash",
        action="store_true",
        help="recompute every hash instead of using unchanged cached entries",
    )
    dupes.add_argument(
        "--trees",
        action="store_true",
        help="report maximal byte-identical directory trees (hashes every file)",
    )
    dupes.add_argument(
        "--progress",
        choices=("auto", "always", "never"),
        default="auto",
        help="progress display policy (default: auto)",
    )
    dupes.add_argument(
        "--all",
        action="store_true",
        help="show every duplicate group instead of the 10 largest",
    )
    _add_max_threads_argument(dupes)
    _add_json_argument(dupes)
    dupes.set_defaults(handler=_dupes)

    prune = command("prune", "remove missing files from the hash cache")
    _add_json_argument(prune)
    prune.set_defaults(handler=_prune)

    tree_diff = command("diff", "compare two directory trees exactly")
    tree_diff.add_argument("left", help="first tree; relative paths are beneath the library root")
    tree_diff.add_argument("right", help="second tree; relative paths are beneath the library root")
    _add_json_argument(tree_diff)
    tree_diff.set_defaults(handler=_diff)

    compact = command("compact", "plan exact duplicate-tree compaction")
    compact.add_argument(
        "target",
        nargs="?",
        help="path to compact; relative paths are resolved beneath the library root",
    )
    compact_actions = compact.add_mutually_exclusive_group()
    compact_actions.add_argument("--show", action="store_true", help="show the pending plan")
    compact_actions.add_argument("--apply", action="store_true", help="apply the pending plan")
    compact.add_argument(
        "--prefer",
        action="append",
        default=[],
        metavar="PATH",
        help="prefer retaining copies beneath PATH; repeat in priority order",
    )
    compact.add_argument(
        "--all",
        action="store_true",
        help="show every planned trash move",
    )
    _add_max_threads_argument(compact)
    compact.set_defaults(handler=_compact)

    command("help", "show help for Muse or one command")

    import_command = command(
        "import", "strictly validate, plan, or apply an album import into master"
    )
    import_command.add_argument("source", help="directory beneath backlog to import")
    import_command.add_argument(
        "destination",
        nargs="?",
        help="destination beneath master; required when creating a plan",
    )
    import_actions = import_command.add_mutually_exclusive_group()
    import_actions.add_argument("--apply", action="store_true", help="apply the ready plan")
    import_actions.add_argument("--abort", action="store_true", help="discard the ready plan")
    _add_json_argument(import_command)
    import_command.set_defaults(handler=_import)

    relocation = command("mv", "move content and preserve cached hashes")
    relocation.add_argument("source", help="existing path beneath the library root")
    relocation.add_argument("destination", help="new path beneath the library root")
    _add_json_argument(relocation)
    relocation.set_defaults(handler=_move)

    slag = command("slag", "inspect or move non-audio backlog artifacts")
    slag.add_argument(
        "sources",
        nargs="*",
        metavar="PATH",
        help="backlog files or directories to move into slag",
    )
    slag.add_argument("--long", action="store_true", help="include size and file type")
    slag.add_argument("--all", action="store_true", help="list every selected file")
    slag.add_argument("--dirs", action="store_true", help="list directories only")
    slag.add_argument(
        "--thorough",
        action="store_true",
        help="also select audio-adjacent artwork, metadata, and checksum files",
    )
    slag.add_argument(
        "--apply", action="store_true", help="move the selected artifacts after confirmation"
    )
    _add_json_argument(slag)
    slag.set_defaults(handler=_slag)

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

        console.print()
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


def _search_path_text(path: str) -> Text:
    """De-emphasize a result's parent path while keeping its basename prominent."""
    parent, separator, name = path.rpartition("/")
    rendered = Text()
    if separator:
        rendered.append(f"{parent}/", style="dim")
    rendered.append(name, style="not dim bold")
    return rendered


def _search(args: argparse.Namespace, root: Path) -> int:
    matches, errors = search_vault(root, args.terms)
    result = {
        "root": str(root),
        "query": args.terms,
        "match_count": len(matches),
        "matches": [match.to_dict() for match in matches],
        "errors": [error.to_dict() for error in errors],
        "state_created": False,
    }

    if args.json:
        emit_json(result)
    else:
        console = make_console(args.color)
        console.print("[bold]Vault search[/bold]")
        console.print("[dim]Query[/dim]", " ".join(args.terms), "\n")
        if matches:
            print_table(
                console,
                ("TYPE", "PATH"),
                [(match.kind, _search_path_text(match.path)) for match in matches],
            )
        else:
            console.print("[yellow]No matches.[/yellow]")
        console.print(f"[dim]{human_number(len(matches))} matching paths; read-only search.[/dim]")

        if errors:
            error_console = make_console(args.color, stderr=True)
            error_console.print("[bold red]Search errors[/bold red]")
            for error in errors:
                error_console.print(f"  [red]{error.path}:[/red] {error.message}")

    return 1 if errors else 0


def _stats_row(name: str, stats: PathStats) -> tuple[str, ...]:
    allocated = stats.allocated_bytes if stats.allocated_bytes_available else None
    return (
        name,
        human_number(stats.files),
        human_number(stats.audio_files),
        human_number(stats.non_audio_files),
        human_number(stats.zero_byte_files),
        human_number(stats.directories),
        human_number(stats.symlinks),
        human_bytes(stats.logical_bytes),
        human_bytes(allocated),
        human_number(len(stats.errors)),
    )


def _extension_text(extension: str) -> Text:
    if extension == "[no extension]":
        return Text("(no extension)", style="dim")
    if extension in AUDIO_EXTENSIONS:
        style = "cyan"
    elif extension in _IMAGE_EXTENSIONS:
        style = "magenta"
    elif extension in _PLAYLIST_EXTENSIONS:
        style = "green"
    elif extension in _METADATA_EXTENSIONS:
        style = "yellow"
    else:
        style = ""
    return Text(extension, style=style)


def _print_extension_table(
    console: Any,
    extensions: dict[str, int],
    extension_bytes: dict[str, int],
) -> None:
    entries = [
        (
            _extension_text(extension),
            human_number(count),
            human_bytes(extension_bytes[extension]),
        )
        for extension, count in sorted(
            extensions.items(), key=lambda item: (-item[1], item[0])
        )
    ]
    column_count = min(3, len(entries))
    row_count = (len(entries) + column_count - 1) // column_count
    rows = []
    for row_index in range(row_count):
        row = []
        for column_index in range(column_count):
            extension_index = row_index + column_index * row_count
            entry = entries[extension_index] if extension_index < len(entries) else ("", "", "")
            extension, count, size = entry
            if column_index:
                prefixed_extension = Text("│ ")
                if isinstance(extension, Text):
                    prefixed_extension.append_text(extension)
                else:
                    prefixed_extension.append(extension)
                extension = prefixed_extension
            row.extend((extension, count, size))
        rows.append(tuple(row))
    headers = []
    for column_index in range(column_count):
        header = "EXTENSION" if column_index == 0 else "│ EXTENSION"
        headers.extend((header, "#", "SIZE"))
    print_table(
        console,
        tuple(headers),
        rows,
        right_aligned=frozenset({"#", "SIZE"}),
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
        area_priority = {name: index for index, name in enumerate(_STATS_AREA_ORDER)}
        ordered_areas = sorted(
            areas.items(),
            key=lambda item: (
                item[0] == ".muse",
                area_priority.get(item[0], len(area_priority)),
                item[0],
            ),
        )
        rows = [_stats_row(name, value) for name, value in ordered_areas]
        rows.append(_stats_row("TOTAL", total))
        print_table(
            console,
            (
                "AREA",
                "FILES",
                "AUDIO",
                "OTHER",
                "ZERO-BYTE",
                "DIRS",
                "LINKS",
                "LOGICAL",
                "ALLOCATED",
                "ERRORS",
            ),
            rows,
            right_aligned=frozenset(
                {
                    "FILES",
                    "AUDIO",
                    "OTHER",
                    "ZERO-BYTE",
                    "DIRS",
                    "LINKS",
                    "LOGICAL",
                    "ALLOCATED",
                    "ERRORS",
                }
            ),
            bold_rows=frozenset({"TOTAL"}),
        )

        if total.extensions:
            console.print()
            _print_extension_table(
                console, total.extensions, total.extension_logical_bytes
            )

        if total.errors:
            error_console = make_console(args.color, stderr=True)
            error_console.print("[bold red]Scan errors[/bold red]")
            for error in total.errors:
                error_console.print(f"  [red]{error.path}:[/red] {error.message}")

        console.print("[dim]Read-only scan; no Muse state was created or changed.[/dim]")

    return 1 if total.errors else 0


def _scan_summary_rows(report: ScanComparison) -> list[tuple[str, str, str]]:
    return [
        (
            "Definite audio on target",
            human_number(report.target_files),
            human_bytes(report.target_bytes),
        ),
        (
            "Already in vault" if report.mode == "thorough" else "Likely in vault",
            human_number(report.present_files),
            human_bytes(report.present_bytes),
        ),
        (
            "Not found in vault",
            human_number(len(report.new_files)),
            human_bytes(report.new_bytes),
        ),
    ]


class _ScanProgressDisplay:
    """Indeterminate progress bar for walking an external target."""

    def __init__(self, mode: str, color: str) -> None:
        self.console = make_console(color, stderr=True)
        self.enabled = mode == "always" or (mode == "auto" and sys.stderr.isatty())
        self.task_id: int | None = None
        self.progress = Progress(
            SpinnerColumn(),
            TextColumn("{task.description}"),
            BarColumn(),
            console=self.console,
            transient=True,
        )
        self.running = False

    def update(self, update: ScanProgress) -> None:
        if not self.enabled:
            return
        if update.complete:
            self.stop()
            return
        description = (
            f"Scanning target — {human_number(update.completed_files)} files, "
            f"{human_bytes(update.completed_bytes)}"
        )
        if not self.running:
            self.progress.start()
            self.task_id = self.progress.add_task(description, total=None)
            self.running = True
        elif self.task_id is not None:
            self.progress.update(self.task_id, description=description)

    def stop(self) -> None:
        if self.running:
            self.progress.stop()
            self.running = False


class _PullProgressDisplay:
    """Determinate progress bar for copying external content into backlog."""

    def __init__(self, mode: str, color: str) -> None:
        self.console = make_console(color, stderr=True)
        self.enabled = mode == "always" or (mode == "auto" and sys.stderr.isatty())
        self.task_id: int | None = None
        self.progress = Progress(
            TextColumn("{task.description}"),
            BarColumn(),
            TaskProgressColumn(),
            TimeRemainingColumn(),
            console=self.console,
            transient=True,
        )
        self.running = False

    def update(self, update: PullProgress) -> None:
        if not self.enabled:
            return
        if update.complete:
            self.stop()
            return
        description = (
            f"Pulling into backlog — {human_number(update.completed_files)}/"
            f"{human_number(update.total_files)} files"
        )
        if not self.running:
            self.progress.start()
            self.task_id = self.progress.add_task(
                description,
                total=max(update.total_bytes, 1),
            )
            self.running = True
        elif self.task_id is not None:
            self.progress.update(
                self.task_id,
                completed=update.completed_bytes,
                description=description,
            )

    def stop(self) -> None:
        if self.running:
            self.progress.stop()
            self.running = False


def _not_found_directory_rows(
    report: ScanComparison, *, show_all: bool = False
) -> list[tuple[Any, ...]]:
    grouped: dict[str, list[ScannedFile]] = {}
    for item in report.new_files:
        directory = str(Path(item.relative).parent)
        grouped.setdefault(directory, []).append(item)

    rows = []
    ordered = sorted(
        grouped.items(),
        key=lambda pair: (-sum(item.size for item in pair[1]), pair[0].casefold()),
    )
    displayed_directories = ordered if show_all else ordered[:25]
    for directory, items in displayed_directories:
        items.sort(key=lambda item: (-item.size, item.path.name.casefold()))
        examples = Text()
        displayed_items = items if show_all else items[:5]
        for index, item in enumerate(displayed_items):
            if index:
                examples.append("\n")
            examples.append(item.path.name, style="bold cyan")
            examples.append(f"  {human_bytes(item.size)}", style="dim")
        omitted = len(items) - len(displayed_items)
        if omitted > 0:
            examples.append(f"\n…and {human_number(omitted)} more", style="dim")
        rows.append(
            (
                directory,
                human_number(len(items)),
                human_bytes(sum(item.size for item in items)),
                examples,
            )
        )
    return rows


def _resolve_pull_destination(root: Path, value: str) -> Path:
    """Resolve an interactive pull destination strictly beneath backlog/."""
    relative = Path(value.strip())
    if not value.strip():
        raise ValueError("a destination is required")
    if relative.is_absolute():
        raise ValueError("enter a relative path beneath backlog/")
    if relative.parts[:1] == ("backlog",):
        relative = Path(*relative.parts[1:])
    if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        raise ValueError("destination must name a directory below backlog/")
    return root / "backlog" / relative


def _scan(args: argparse.Namespace, root: Path) -> int:
    # Unlike vault-scoped commands, scan's relative target is an ordinary path
    # relative to the caller: its main purpose is inspecting external media.
    target = Path(args.target).expanduser().absolute()
    if args.pull and args.json:
        make_console(args.color, stderr=True).print(
            "[red]Scan refused:[/red] --pull is interactive and cannot be combined with --json"
        )
        return 1
    progress = _ScanProgressDisplay(args.progress, args.color)
    try:
        report = compare_with_vault(
            root,
            target,
            thorough=args.thorough,
            max_threads=args.max_threads,
            progress=progress.update,
        )
    finally:
        progress.stop()
    if args.json:
        emit_json(report.to_dict())
        return 1 if report.errors else 0

    console = make_console(args.color)
    console.print("[bold]External source scan[/bold]")
    console.print("[dim]Target[/dim]", str(target))
    console.print(
        "[dim]Comparison[/dim]",
        "SHA-256 content checksums" if args.thorough else "case-insensitive filename + size",
        "\n",
    )
    if report.target_extensions:
        console.print("[bold]Definite audio file types[/bold]")
        _print_extension_table(
            console, report.target_extensions, report.target_extension_bytes
        )
        console.print()

    print_table(
        console,
        ("CATEGORY", "AUDIO FILES", "LOGICAL"),
        _scan_summary_rows(report),
        right_aligned=frozenset({"AUDIO FILES", "LOGICAL"}),
    )

    if report.new_files:
        console.print()
        console.print("[bold]Not-found directories[/bold]")
        directory_rows = _not_found_directory_rows(report, show_all=args.all)
        print_table(
            console,
            ("DIRECTORY", "FILES", "LOGICAL", "FILES" if args.all else "LARGEST FILES"),
            directory_rows,
            right_aligned=frozenset({"FILES", "LOGICAL"}),
        )
        directory_count = len({str(Path(item.relative).parent) for item in report.new_files})
        omitted_directories = directory_count - len(directory_rows)
        if omitted_directories:
            console.print(
                f"[dim]…and {human_number(omitted_directories)} more directories; "
                "use --all (or --json) for all paths.[/dim]"
            )

    if report.new_audio_files:
        audio_noun = "audio file was" if report.new_audio_files == 1 else "audio files were"
        console.print(
            f"[bold yellow]Worth reviewing:[/bold yellow] "
            f"{human_number(report.new_audio_files)} definite {audio_noun} not found in the vault."
        )
    elif report.new_files:
        console.print(
            "[bold green]No new definite audio detected.[/bold green] "
            "Only ambiguous or non-audio file types were not found in the vault."
        )
    elif report.target_files:
        qualifier = "byte-identical copies" if args.thorough else "likely copies"
        console.print(f"[bold green]Everything has {qualifier} in the vault.[/bold green]")
    elif report.inventory_files:
        console.print("[yellow]No definite audio files were found on the target.[/yellow]")
    else:
        console.print("[yellow]No regular files were found on the target.[/yellow]")

    if not args.thorough and report.target_files:
        console.print(
            "[dim]Quick results are heuristic. Use --thorough before discarding the source.[/dim]"
        )
    if report.errors:
        error_console = make_console(args.color, stderr=True)
        error_console.print("[bold red]Scan errors[/bold red]")
        for error in report.errors:
            error_console.print(f"  [red]{error.path}:[/red] {error.message}")

    if args.pull:
        if report.errors:
            make_console(args.color, stderr=True).print(
                "[red]Pull refused:[/red] resolve scan errors before copying content"
            )
            return 1
        if not report.new_files:
            console.print("[dim]Nothing was pulled; no not-found audio was detected.[/dim]")
            return 0
        try:
            value = input("Pull into which new directory under backlog/? ")
            destination = _resolve_pull_destination(root, value)
            pull_progress = _PullProgressDisplay(args.progress, args.color)
            try:
                result = pull_new_directories(
                    root,
                    target,
                    report.new_files,
                    destination,
                    pull_progress.update,
                )
            finally:
                pull_progress.stop()
        except (EOFError, OSError, ValueError) as error:
            make_console(args.color, stderr=True).print(f"[red]Pull refused:[/red] {error}")
            return 1
        relative_destination = result.destination.relative_to(root)
        console.print(
            f"[green]Pulled {human_number(len(result.source_directories))} containing "
            f"director{'y' if len(result.source_directories) == 1 else 'ies'} into "
            f"{relative_destination}.[/green]"
        )
    return 1 if report.errors else 0


class _DuplicateProgressDisplay:
    """Delayed terminal progress for duplicate scans."""

    _DELAY_SECONDS = 2.0
    _LONG_BYTES = 1024**3
    _LONG_FILES = 1_000

    def __init__(self, mode: str, color: str, *, trees: bool = False) -> None:
        self.console = make_console(color, stderr=True)
        self.enabled = mode == "always" or (mode == "auto" and sys.stderr.isatty())
        self.immediate = mode == "always"
        self.trees = trees
        self.started_at = monotonic()
        self.phase: str | None = None
        self.task_id: int | None = None
        self.progress = Progress(
            SpinnerColumn(),
            TextColumn("{task.description}"),
            BarColumn(),
            TaskProgressColumn(),
            TimeRemainingColumn(),
            console=self.console,
            transient=True,
        )
        self.running = False

    def _should_start(self, update: ProgressUpdate) -> bool:
        predicted_long = update.phase == "hashing" and (
            (update.total_bytes or 0) >= self._LONG_BYTES
            or (update.total_files or 0) >= self._LONG_FILES
        )
        return (
            self.immediate
            or predicted_long
            or monotonic() - self.started_at >= self._DELAY_SECONDS
        )

    def _description(self, update: ProgressUpdate) -> str:
        if update.phase == "inventory":
            return (
                f"Inventorying files — {human_number(update.completed_files)} files, "
                f"{human_bytes(update.completed_bytes)}"
            )
        if update.phase == "hashing":
            total_files = human_number(update.total_files or 0)
            cache_total = update.cached_files + (update.total_files or 0)
            cache_rate = update.cached_files / cache_total if cache_total else 0.0
            return (
                f"{'Hashing content' if self.trees else 'Lazy-hashing'} — "
                f"{human_number(update.completed_files)}/{total_files} files, "
                f"{human_bytes(update.completed_bytes)}/{human_bytes(update.total_bytes or 0)}, "
                f"cache {cache_rate:.1%}, using {update.worker_threads} worker "
                f"{'thread' if update.worker_threads == 1 else 'threads'}"
            )
        return (
            f"Analyzing hashes — {human_number(update.completed_files)}/"
            f"{human_number(update.total_files or 0)}"
        )

    def update(self, update: ProgressUpdate) -> None:
        if not self.enabled or update.phase == "complete":
            return
        if not self.running:
            if not self._should_start(update):
                return
            self.progress.start()
            self.running = True

        if update.phase != self.phase:
            if self.task_id is not None:
                self.progress.remove_task(self.task_id)
            total = update.total_bytes if update.phase == "hashing" else update.total_files
            self.task_id = self.progress.add_task(self._description(update), total=total)
            self.phase = update.phase

        if self.task_id is None:
            return
        completed = (
            update.completed_bytes if update.phase == "hashing" else update.completed_files
        )
        self.progress.update(
            self.task_id,
            description=self._description(update),
            completed=completed,
            total=update.total_bytes if update.phase == "hashing" else update.total_files,
        )

    def stop(self) -> None:
        if self.running:
            self.progress.stop()
            self.running = False


def _duplicate_summary_rows(report: DuplicateReport) -> list[tuple[str, str]]:
    return [
        ("Elapsed", human_duration(report.elapsed_seconds)),
        ("Files examined", human_number(report.files)),
        ("Logical size", human_bytes(report.logical_bytes)),
        (
            "Hash candidates",
            f"{human_number(report.hash_candidate_files)} · "
            f"{human_bytes(report.hash_candidate_bytes)}",
        ),
        (
            "Hash cache",
            f"{human_number(report.hashed_files)} computed · "
            f"{human_number(report.cached_files)} reused",
        ),
        (
            "Cache hit rate",
            f"{report.cache_hit_rate:.1%} files · {report.byte_cache_hit_rate:.1%} bytes",
        ),
        ("Hash workers", human_number(report.hash_worker_threads)),
        (
            "Data read",
            f"{human_bytes(report.bytes_read)} · "
            f"{human_bytes(round(report.hash_throughput))}/s",
        ),
        ("Duplicate groups", human_number(len(report.groups))),
        ("Redundant copies", human_number(report.redundant_occurrences)),
        ("Logical repeated bytes", human_bytes(report.logical_repeated_bytes)),
        ("Errors", human_number(len(report.errors))),
    ]


def _tree_summary_rows(report: DuplicateReport) -> list[tuple[str, str]]:
    return [
        ("Elapsed", human_duration(report.elapsed_seconds)),
        ("Files examined", human_number(report.files)),
        ("Logical size", human_bytes(report.logical_bytes)),
        (
            "Hash cache",
            f"{human_number(report.hashed_files)} computed · "
            f"{human_number(report.cached_files)} reused",
        ),
        (
            "Data read",
            f"{human_bytes(report.bytes_read)} · "
            f"{human_bytes(round(report.hash_throughput))}/s",
        ),
        ("Hash workers", human_number(report.hash_worker_threads)),
        ("Duplicate tree groups", human_number(len(report.tree_groups))),
        (
            "Redundant tree copies",
            human_number(sum(len(group.paths) - 1 for group in report.tree_groups)),
        ),
        (
            "Logical repeated bytes",
            human_bytes(sum(group.logical_repeated_bytes for group in report.tree_groups)),
        ),
        ("Errors", human_number(len(report.errors))),
    ]


def _dupes(args: argparse.Namespace, root: Path) -> int:
    targets = [resolve_target(root, target) for target in args.targets] or [root]
    progress = _DuplicateProgressDisplay(args.progress, args.color, trees=args.trees)
    try:
        report = find_duplicates(
            targets,
            root / ".muse" / "muse.db",
            rehash=args.rehash,
            trees=args.trees,
            progress=progress.update,
            max_threads=args.max_threads,
        )
    finally:
        progress.stop()

    if args.json:
        emit_json(report.to_dict())
    else:
        console = make_console(args.color)
        title = "Exact duplicate directory trees" if args.trees else "Exact duplicate files"
        console.print(f"[bold]{title}[/bold]")
        console.print("[dim]Targets[/dim]", ", ".join(map(str, targets)))
        console.print("[dim]Hash cache[/dim]", report.database, "\n")
        print_table(
            console,
            ("METRIC", "VALUE"),
            _tree_summary_rows(report) if args.trees else _duplicate_summary_rows(report),
            right_aligned=frozenset({"VALUE"}),
        )

        if args.trees and report.tree_groups:
            console.print()
            console.print("[bold]Maximal duplicate directory trees[/bold]")
            terminal_output = sys.stdout.isatty()
            path_width = max(24, console.width - 60)
            displayed_groups = report.tree_groups if args.all else report.tree_groups[:10]
            rows = [
                (
                    group.sha256[:12],
                    human_number(len(group.paths)),
                    human_number(group.files),
                    human_bytes(group.logical_bytes),
                    human_bytes(group.logical_repeated_bytes),
                    "\n".join(
                        middle_truncate(path, path_width) if terminal_output else path
                        for path in group.paths
                    ),
                )
                for group in displayed_groups
            ]
            print_table(
                console,
                ("TREE", "COPIES", "FILES", "EACH", "REPEATED", "DIRECTORIES"),
                rows,
                right_aligned=frozenset({"COPIES", "FILES", "EACH", "REPEATED"}),
                column_widths={"DIRECTORIES": path_width} if terminal_output else None,
            )
            if len(report.tree_groups) > len(displayed_groups):
                console.print(
                    f"[dim]{human_number(len(report.tree_groups) - len(displayed_groups))} "
                    "more duplicate tree groups; use --all to show them.[/dim]"
                )

        if report.groups and not args.trees:
            console.print()
            console.print("[bold]Duplicate groups[/bold]")
            terminal_output = sys.stdout.isatty()
            path_width = max(24, console.width - 48)

            def file_and_paths(group: DuplicateGroup) -> Text:
                file_paths = [Path(path) for path in group.paths]
                filenames = {path.name for path in file_paths}
                if len(filenames) != 1:
                    lines = Text()
                    for index, path in enumerate(file_paths):
                        filename = path.name
                        directory = f"{path.parent}/"
                        if terminal_output:
                            filename = middle_truncate(filename, path_width)
                            directory = middle_truncate(directory, path_width)
                        if index:
                            lines.append("\n")
                        lines.append(filename, style="bold cyan")
                        lines.append("\n" + directory, style="dim")
                    return lines
                filename = file_paths[0].name
                directories = [f"{path.parent}/" for path in file_paths]
                if terminal_output:
                    filename = middle_truncate(filename, path_width)
                    directories = [middle_truncate(path, path_width) for path in directories]
                return Text.assemble(
                    (filename, "bold cyan"),
                    ("\n" + "\n".join(directories), "dim"),
                )

            displayed_groups = report.groups if args.all else report.groups[:10]
            rows = [
                (
                    group.sha256[:12],
                    human_number(len(group.paths)),
                    human_bytes(group.size),
                    human_bytes(group.logical_repeated_bytes),
                    file_and_paths(group),
                )
                for group in displayed_groups
            ]
            print_table(
                console,
                ("SHA-256", "COPIES", "EACH", "REPEATED", "FILES"),
                rows,
                right_aligned=frozenset({"COPIES", "EACH", "REPEATED"}),
                column_widths={"FILES": path_width} if terminal_output else None,
            )
            if len(report.groups) > len(displayed_groups):
                console.print(
                    f"[dim]{human_number(len(report.groups) - len(displayed_groups))} "
                    "more duplicate groups; use --all to show them.[/dim]"
                )

        if report.errors:
            error_console = make_console(args.color, stderr=True)
            error_console.print("[bold red]Hash errors[/bold red]")
            for error in report.errors:
                error_console.print(f"  [red]{error.path}:[/red] {error.message}")

        console.print(
            "[dim]Music files were not changed; reusable hashes were stored in .muse/muse.db.[/dim]"
        )

    return 1 if report.errors else 0


def _prune_rows(report: CachePruneReport) -> list[tuple[str, str]]:
    return [
        ("Elapsed", human_duration(report.elapsed_seconds)),
        ("Entries examined", human_number(report.entries_before)),
        ("Entries removed", human_number(report.entries_removed)),
        ("Entries retained", human_number(report.entries_after)),
        ("Missing-file logical size", human_bytes(report.represented_bytes_removed)),
        ("Retained-file logical size", human_bytes(report.represented_bytes_after)),
    ]


def _prune(args: argparse.Namespace, root: Path) -> int:
    report = prune_missing(root / ".muse" / "muse.db")
    if args.json:
        emit_json(report.to_dict())
    else:
        console = make_console(args.color)
        console.print("[bold]Hash cache pruning[/bold]")
        console.print("[dim]Database[/dim]", report.database, "\n")
        print_table(
            console,
            ("METRIC", "VALUE"),
            _prune_rows(report),
            right_aligned=frozenset({"VALUE"}),
        )
        if not report.database_exists:
            console.print("[dim]No hash database exists; nothing was changed.[/dim]")
        elif report.entries_removed:
            console.print("[green]Stale hash entries removed.[/green]")
        else:
            console.print("[dim]The hash cache was already clean.[/dim]")
    return 0


def _diff_summary_rows(report: TreeDiffReport) -> list[tuple[str, str]]:
    return [
        ("Elapsed", human_duration(report.elapsed_seconds)),
        ("Left files", human_number(report.left_files)),
        ("Right files", human_number(report.right_files)),
        ("Hashes computed", human_number(report.hashed_files)),
        ("Hashes reused", human_number(report.cached_files)),
        ("Differences", human_number(len(report.differences))),
        ("Errors", human_number(len(report.errors))),
    ]


def _diff(args: argparse.Namespace, root: Path) -> int:
    report = compare_trees(
        resolve_target(root, args.left),
        resolve_target(root, args.right),
        root / ".muse" / "muse.db",
    )
    if args.json:
        emit_json(report.to_dict())
    else:
        console = make_console(args.color)
        console.print("[bold]Exact tree comparison[/bold]")
        console.print("[dim]Left[/dim]", report.left)
        console.print("[dim]Right[/dim]", report.right, "\n")
        print_table(
            console,
            ("METRIC", "VALUE"),
            _diff_summary_rows(report),
            right_aligned=frozenset({"VALUE"}),
        )
        if report.differences:
            console.print()
            print_table(
                console,
                ("KIND", "PATH"),
                [(difference.kind, difference.path) for difference in report.differences],
            )
    return 1 if report.errors else 0


class _CompactProgressDisplay:
    """Operation-level progress for compaction verification and trash moves."""

    def __init__(self, color: str) -> None:
        self.console = make_console(color, stderr=True)
        self.enabled = sys.stderr.isatty()
        self.phase: str | None = None
        self.task_id: int | None = None
        self.progress = Progress(
            SpinnerColumn(),
            TextColumn("{task.description}"),
            BarColumn(),
            TaskProgressColumn(),
            console=self.console,
            transient=True,
        )
        self.running = False

    def update(self, update: CompactProgress) -> None:
        if not self.enabled:
            return
        if not self.running:
            self.progress.start()
            self.running = True
        if update.phase != self.phase:
            if self.task_id is not None:
                self.progress.remove_task(self.task_id)
            description = (
                f"Reverifying duplicate trees — using {update.worker_threads} worker "
                f"{'thread' if update.worker_threads == 1 else 'threads'}"
                if update.phase == "verify"
                else "Moving duplicate trees to trash"
            )
            self.task_id = self.progress.add_task(description, total=update.total_operations)
            self.phase = update.phase
        if self.task_id is not None:
            self.progress.update(
                self.task_id,
                completed=update.completed_operations,
                total=update.total_operations,
            )

    def stop(self) -> None:
        if self.running:
            self.progress.stop()
            self.running = False


def _compact_rows(operations: list[Any]) -> list[tuple[str, str]]:
    return [
        ("Trees to trash", human_number(len(operations))),
        ("Files to trash", human_number(sum(operation.files for operation in operations))),
        (
            "Logical bytes to trash",
            human_bytes(sum(operation.logical_bytes for operation in operations)),
        ),
    ]


def _compact_path_texts(remove: str, retain: str) -> tuple[Text, Text]:
    """Emphasize changed path components while dimming shared context."""
    remove_parts = Path(remove).parts
    retain_parts = Path(retain).parts
    remove_shared: set[int] = set()
    retain_shared: set[int] = set()
    matcher = SequenceMatcher(a=remove_parts, b=retain_parts, autojunk=False)
    for block in matcher.get_matching_blocks():
        remove_shared.update(range(block.a, block.a + block.size))
        retain_shared.update(range(block.b, block.b + block.size))

    def render(parts: tuple[str, ...], shared: set[int], difference_style: str) -> Text:
        text = Text()
        for index, part in enumerate(parts):
            style = "dim" if index in shared else difference_style
            if index:
                text.append("/", style=style)
            text.append(part, style=style)
        return text

    return (
        render(remove_parts, remove_shared, "bold red"),
        render(retain_parts, retain_shared, "bold green"),
    )


def _show_compact_plan(
    console: Any, operations: list[Any], *, limit: int | None = 10
) -> None:
    print_table(
        console,
        ("METRIC", "VALUE"),
        _compact_rows(operations),
        right_aligned=frozenset({"VALUE"}),
    )
    if operations:
        if limit is None:
            displayed = sorted(operations, key=lambda item: (item.remove.casefold(), item.remove))
            heading = "Planned trash moves"
        else:
            displayed = operations[:limit]
            heading = "Largest trash moves"
        rows = [
            (human_bytes(item.logical_bytes), *_compact_path_texts(item.remove, item.retain))
            for item in displayed
        ]
        console.print()
        console.print(f"[bold]{heading}[/bold]")
        print_table(
            console,
            ("BYTES", "MOVE TO TRASH", "RETAIN"),
            rows,
            right_aligned=frozenset({"BYTES"}),
        )
        if len(operations) > len(rows):
            remaining = human_number(len(operations) - len(rows))
            console.print(f"[dim]{remaining} more trash moves in the plan.[/dim]")


def _compact(args: argparse.Namespace, root: Path) -> int:
    console = make_console(args.color)
    if (args.show or args.apply) and (args.target or args.prefer):
        console.print(
            "[red]A pending plan already defines its scope and preferences; "
            "omit the target and --prefer.[/red]"
        )
        return 1
    if args.show:
        try:
            operations = load_plan(root)
        except FileNotFoundError:
            console.print("[yellow]No pending compact plan.[/yellow]")
            return 1
        _show_compact_plan(console, operations, limit=None)
        return 0
    if args.apply:
        try:
            operations = load_plan(root)
        except FileNotFoundError:
            console.print("[yellow]No pending compact plan.[/yellow]")
            return 1
        _show_compact_plan(console, operations, limit=None if args.all else 10)
        confirmation = input("Type TRASH to apply this plan: ")
        if confirmation != "TRASH":
            console.print("[yellow]Compaction cancelled.[/yellow]")
            return 1
        distinct_paths = len(
            {path for item in operations for path in (item.retain, item.remove)}
        )
        workers = (
            min(args.max_threads or 16, distinct_paths, os.cpu_count() or 1)
            if distinct_paths
            else 0
        )
        console.print(
            f"[dim]Reverifying planned trees before moving them to trash "
            f"using {workers} worker {'thread' if workers == 1 else 'threads'}…[/dim]"
        )
        progress = _CompactProgressDisplay(args.color)
        try:
            receipt = apply_plan(
                root, operations, progress.update, max_threads=args.max_threads
            )
        except ValueError as error:
            console.print(f"[red]Compaction refused:[/red] {error}")
            return 1
        finally:
            progress.stop()
        if receipt is not None:
            console.print(
                f"[green]Compaction applied; removed trees moved to "
                f"{receipt.relative_to(root)}/.[/green]"
            )
            console.print(
                "[yellow]Removing maximal duplicate trees can expose additional "
                "duplicates. Create another compact plan with the same scope and "
                "preferences to check for another pass.[/yellow]"
            )
        else:
            console.print("[green]Compaction applied; the plan contained no trash moves.[/green]")
        console.print("[dim]Audit plan saved in .muse/audit/.[/dim]")
        return 0

    console.print("[bold]Exact-tree compaction plan[/bold]")
    target = resolve_target(root, args.target)
    try:
        target.relative_to(root)
    except ValueError:
        console.print("[red]Compaction target must be inside the library root.[/red]")
        return 1
    preferences = []
    for value in args.prefer:
        preferred = resolve_target(root, value)
        try:
            preferences.append(preferred.relative_to(root))
        except ValueError:
            console.print("[red]Preferred paths must be inside the library root.[/red]")
            return 1
    worker_limit = min(args.max_threads or 16, os.cpu_count() or 1)
    console.print(
        f"[dim]Scanning with up to {worker_limit} worker "
        f"{'thread' if worker_limit == 1 else 'threads'}…[/dim]"
    )
    operations, errors = make_plan(
        root, target, preferences=preferences, max_threads=args.max_threads
    )
    if errors:
        console.print("[red]Compaction plan was not saved because scanning had errors.[/red]")
        return 1
    save_plan(root, operations)
    _show_compact_plan(console, operations, limit=None if args.all else 10)
    console.print(
        "[dim]No files were changed. Review: muse compact --show; apply: muse compact --apply[/dim]"
    )
    return 0


def _slag(args: argparse.Namespace, root: Path) -> int:
    if not args.sources:
        result = slag_stats(root)
        copies = slag_inventory(root)
        if args.json:
            payload = result.to_dict()
            payload["entries"] = [
                {"path": str(item.source.relative_to(root / "slag")), "size": item.size}
                for item in copies
            ]
            emit_json(payload)
        else:
            console = make_console(args.color)
            console.print("[bold]Slag inventory[/bold]")
            grouped: dict[Path, list[Any]] = {}
            for item in copies:
                grouped.setdefault(item.source.parent, []).append(item)
            if args.dirs:
                rows = [
                    (
                        str(directory.relative_to(root / "slag")),
                        human_number(len(items)),
                        human_bytes(sum(item.size for item in items)),
                    )
                    for directory, items in grouped.items()
                ]
                print_table(
                    console,
                    ("DIRECTORY", "FILES", "SIZE"),
                    rows,
                    right_aligned=frozenset({"FILES", "SIZE"}),
                )
            elif args.all:
                rows = [
                    (
                        str(item.source.relative_to(root / "slag")),
                        human_bytes(item.size),
                        item.source.suffix.lower() or "(no extension)",
                    )
                    for item in copies
                ]
                print_table(
                    console,
                    ("PATH", "SIZE", "TYPE") if args.long else ("PATH",),
                    rows if args.long else [(row[0],) for row in rows],
                    right_aligned=frozenset({"SIZE"}),
                )
            else:
                for directory, items in grouped.items():
                    relative = directory.relative_to(root / "slag")
                    console.print(
                        f"[bold]{relative}/[/bold] [dim]{len(items)} files · "
                        f"{human_bytes(sum(item.size for item in items))}[/dim]"
                    )
                    for item in items[:10]:
                        detail = f" [dim]{human_bytes(item.size)}[/dim]" if args.long else ""
                        console.print(f"        {item.source.name}{detail}")
                    if len(items) > 10:
                        console.print(f"        [dim]… {len(items) - 10} more files[/dim]")
                    console.print()
            if args.dirs or args.all:
                console.print()
            print_table(
                console,
                ("METRIC", "VALUE"),
                [
                    ("Files", human_number(result.files)),
                    ("Size", human_bytes(result.logical_bytes)),
                ],
                right_aligned=frozenset({"VALUE"}),
            )
        return 1 if result.errors else 0
    try:
        copies = slag_candidates(
            root,
            [resolve_target(root, source) for source in args.sources],
            thorough=args.thorough,
        )
    except ValueError as error:
        make_console(args.color, stderr=True).print(f"[red]Slag refused:[/red] {error}")
        return 1
    if args.json:
        emit_json(
            {
                "files": len(copies),
                "bytes": sum(item.size for item in copies),
                "copies": [
                    {
                        "source": str(item.source),
                        "destination": str(item.destination),
                        "size": item.size,
                    }
                    for item in copies
                ],
            }
        )
    else:
        console = make_console(args.color)
        console.print("[bold]Slag extraction[/bold]")
        grouped: dict[Path, list[Any]] = {}
        for item in copies:
            grouped.setdefault(item.destination.parent, []).append(item)
        if args.dirs:
            rows = [
                (
                    str(directory.relative_to(root / "slag")),
                    human_number(len(items)),
                    human_bytes(sum(item.size for item in items)),
                )
                for directory, items in grouped.items()
            ]
            print_table(
                console,
                ("DIRECTORY", "FILES", "SIZE"),
                rows,
                right_aligned=frozenset({"FILES", "SIZE"}),
            )
        elif args.all:
            rows = [
                (str(item.destination.relative_to(root / "slag")), human_bytes(item.size))
                if args.long
                else (str(item.destination.relative_to(root / "slag")),)
                for item in copies
            ]
            print_table(
                console,
                ("PATH", "SIZE") if args.long else ("PATH",),
                rows,
                right_aligned=frozenset({"SIZE"}),
            )
        else:
            for directory, items in grouped.items():
                relative = directory.relative_to(root / "slag")
                console.print(
                    f"[bold]{relative}/[/bold] [dim]{len(items)} files · "
                    f"{human_bytes(sum(item.size for item in items))}[/dim]"
                )
                for item in items[:10]:
                    detail = f" [dim]{human_bytes(item.size)}[/dim]" if args.long else ""
                    console.print(f"        {item.destination.name}{detail}")
                if len(items) > 10:
                    console.print(f"        [dim]… {len(items) - 10} more files[/dim]")
                console.print()
        if args.dirs or args.all:
            console.print()
        print_table(
            console,
            ("METRIC", "VALUE"),
            [
                ("Files", human_number(len(copies))),
                ("Bytes", human_bytes(sum(item.size for item in copies))),
            ],
            right_aligned=frozenset({"VALUE"}),
        )
    if not args.apply:
        return 0
    if input("Type MOVE to move these files into slag: ") != "MOVE":
        return 1
    progress = None
    display = None
    if sys.stderr.isatty() and copies:
        display = Progress(
            SpinnerColumn(),
            TextColumn("Moving into slag"),
            BarColumn(),
            TaskProgressColumn(),
            TimeRemainingColumn(),
            console=make_console(args.color, stderr=True),
            transient=True,
        )
        display.start()
        task = display.add_task("Moving into slag", total=sum(item.size for item in copies))
        def progress(completed: int, _size: int) -> None:
            display.update(task, completed=completed)
    try:
        copied, skipped = apply_slag(copies, progress)
    except ValueError as error:
        make_console(args.color, stderr=True).print(f"[red]Slag refused:[/red] {error}")
        return 1
    finally:
        if display:
            display.stop()
    if not args.json:
        console.print(
            f"[green]Moved {copied}; removed {skipped} exact existing source copies.[/green]"
        )
    return 0


def _import(args: argparse.Namespace, root: Path) -> int:
    source = resolve_target(root, args.source)
    if (args.apply or args.abort) and args.destination is not None:
        make_console(args.color, stderr=True).print(
            "[red]Import refused:[/red] omit the destination with --apply or --abort"
        )
        return 1
    try:
        if args.abort:
            plan = abort_import_plan(root, source)
            action = "aborted"
        elif args.apply:
            plan = load_import_plan(root, source)
            if not args.json:
                _show_import_plan(make_console(args.color), plan)
            if input("Type IMPORT to apply this plan: ") != "IMPORT":
                make_console(args.color).print("[yellow]Import cancelled.[/yellow]")
                return 1
            plan = apply_import_plan(root, source)
            action = "completed"
        elif args.destination is not None:
            plan = make_import_plan(root, source, args.destination)
            action = "planned"
        else:
            plan = load_import_plan(root, source)
            action = "shown"
    except (FileNotFoundError, ValueError) as error:
        make_console(args.color, stderr=True).print(f"[red]Import refused:[/red] {error}")
        return 1

    payload = {"action": action, **plan.to_dict()}
    if args.json:
        emit_json(payload)
    else:
        console = make_console(args.color)
        if action in {"planned", "shown"}:
            _show_import_plan(console, plan)
            if action == "planned":
                console.print(
                    "[dim]No music was changed. Apply with muse import SOURCE --apply.[/dim]"
                )
        elif action == "aborted":
            console.print("[green]Import plan aborted; music was not changed.[/green]")
        else:
            console.print(f"[green]Imported[/green] {plan.source} → {plan.destination}")
    return 0


def _show_import_plan(console: Any, plan: Any) -> None:
    console.print("[bold]Strictly validated import plan[/bold]")
    console.print("[dim]Source[/dim]", plan.source)
    console.print("[dim]Destination[/dim]", plan.destination)
    print_table(
        console,
        ("METRIC", "VALUE"),
        [
            ("Status", plan.state),
            ("Validated audio files", human_number(len(plan.files))),
            ("Duration", human_duration(plan.duration_seconds)),
            ("Logical size", human_bytes(plan.logical_bytes)),
        ],
        right_aligned=frozenset({"VALUE"}),
    )


def _move(args: argparse.Namespace, root: Path) -> int:
    source = resolve_target(root, args.source)
    destination = resolve_target(root, args.destination)
    try:
        result = move(root, source, destination)
    except ValueError as error:
        console = make_console(args.color, stderr=True)
        console.print(f"[red]{args.command} refused:[/red] {error}")
        return 1
    if args.json:
        emit_json(result.to_dict())
    else:
        console = make_console(args.color)
        console.print(
            f"[green]{args.command.capitalize()}d[/green] {result.source} → {result.destination}"
        )
        console.print(
            f"[dim]Preserved {human_number(result.cached_paths_updated)} cached hash paths.[/dim]"
        )
    return 0


def _protect_negated_search_terms(argv: list[str]) -> list[str]:
    """Keep search's ``-term`` shorthand from being parsed as an option."""
    arguments = list(argv)
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        if argument in {"--root", "--color"}:
            index += 2
            continue
        if argument.startswith(("--root=", "--color=")) or argument.startswith("-"):
            index += 1
            continue
        if argument != "search":
            return arguments

        for term_index in range(index + 1, len(arguments)):
            term = arguments[term_index]
            if term == "--":
                break
            is_negated = len(term) > 1 and term.startswith("-") and not term.startswith("--")
            if is_negated and term != "-h":
                arguments[term_index] = f"{_NEGATED_SEARCH_TERM}{term[1:]}"
        return arguments
    return arguments


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
    if arguments[:1] == ["help"]:
        arguments = [*arguments[1:], "--help"] if len(arguments) > 1 else ["--help"]
    arguments = _protect_negated_search_terms(arguments)
    parser = build_parser(_help_color(arguments))
    try:
        args = parser.parse_args(arguments)
    except SystemExit as error:
        return int(error.code)
    if args.command is None:
        parser.print_help()
        return 0
    if args.command == "search":
        args.terms = [
            f"-{term.removeprefix(_NEGATED_SEARCH_TERM)}"
            if term.startswith(_NEGATED_SEARCH_TERM)
            else term
            for term in args.terms
        ]
    root = resolve_root(args.root)
    return args.handler(args, root)
