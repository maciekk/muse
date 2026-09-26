"""Overview command workflows and output."""

from __future__ import annotations

import argparse
import os
import platform
import shutil
from pathlib import Path
from typing import Any

from rich.text import Text

from muse.config import MANAGED_AREAS, MASTER_SHELVES, resolve_target
from muse.reporting import (
    emit_json,
    human_bytes,
    human_number,
    make_console,
    print_table,
    status_text,
)
from muse.repository import AUDIO_EXTENSIONS, PathStats, scan_path, scan_root_by_area
from muse.search import search_vault

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
        for extension, count in sorted(extensions.items(), key=lambda item: (-item[1], item[0]))
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
            _print_extension_table(console, total.extensions, total.extension_logical_bytes)

        if total.errors:
            error_console = make_console(args.color, stderr=True)
            error_console.print("[bold red]Scan errors[/bold red]")
            for error in total.errors:
                error_console.print(f"  [red]{error.path}:[/red] {error.message}")

        console.print("[dim]Read-only scan; no Muse state was created or changed.[/dim]")

    return 1 if total.errors else 0
