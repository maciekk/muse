"""Terminal and machine-readable output helpers."""

from __future__ import annotations

import json
import sys
from collections.abc import Iterable
from typing import Any

from rich import box
from rich.console import Console, RenderableType
from rich.table import Table
from rich.text import Text

ColorMode = str


def make_console(color: ColorMode, *, stderr: bool = False) -> Console:
    """Create a console honoring explicit color policy and Rich's TTY detection."""
    if color == "always":
        return Console(stderr=stderr, force_terminal=True)
    if color == "never":
        return Console(stderr=stderr, force_terminal=False, color_system=None)
    return Console(stderr=stderr)


def emit_json(value: Any) -> None:
    """Emit stable, undecorated JSON suitable for pipes and files."""
    json.dump(value, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")


def human_number(value: int) -> str:
    """Format an integer for human-readable output."""
    return f"{value:,}"


def middle_truncate(value: str, width: int) -> str:
    """Shorten text while retaining its beginning and end."""
    if len(value) <= width:
        return value
    if width <= 1:
        return "…"[:width]
    front = (width - 1) // 2
    return f"{value[:front]}…{value[-(width - front - 1):]}"


def human_duration(seconds: float) -> str:
    """Format elapsed time compactly while preserving useful precision."""
    if seconds < 10:
        return f"{seconds:.2f}s"
    if seconds < 60:
        return f"{seconds:.1f}s"
    total_seconds = round(seconds)
    minutes, remaining_seconds = divmod(total_seconds, 60)
    if minutes < 60:
        return f"{minutes}m {remaining_seconds:02d}s"
    hours, remaining_minutes = divmod(minutes, 60)
    return f"{hours}h {remaining_minutes:02d}m {remaining_seconds:02d}s"


def human_bytes(value: int | None) -> str:
    if value is None:
        return "n/a"
    units = ("B", "KB", "MB", "GB", "TB", "PB")
    amount = float(value)
    for unit in units:
        if abs(amount) < 1024 or unit == units[-1]:
            return f"{amount:.0f} {unit}" if unit == "B" else f"{amount:.1f} {unit}"
        amount /= 1024
    raise AssertionError("unreachable")


def status_text(status: str) -> Text:
    """Render a semantic status consistently throughout the CLI."""
    normalized = status.lower()
    styles = {
        "ok": "bold green",
        "available": "bold green",
        "warning": "bold yellow",
        "unavailable": "bold yellow",
        "error": "bold red",
    }
    return Text(status.upper(), style=styles.get(normalized, "bold"))


def print_table(
    console: Console,
    headers: tuple[str, ...],
    rows: Iterable[tuple[RenderableType, ...]],
    *,
    right_aligned: frozenset[str] = frozenset(),
    bold_rows: frozenset[str] = frozenset(),
    column_widths: dict[str, int] | None = None,
) -> None:
    """Print a restrained table intended for routine terminal use."""
    table = Table(box=box.SIMPLE_HEAD, show_edge=False, pad_edge=False)
    for header in headers:
        table.add_column(
            header,
            style="bold dim",
            justify="right" if header in right_aligned else "left",
            no_wrap=header in right_aligned or header in {"AREA", "STATUS", "TOOL"},
            min_width=len(header),
            width=column_widths.get(header) if column_widths else None,
        )

    for row in rows:
        first = str(row[0]) if row else ""
        table.add_row(*row, style="bold" if first in bold_rows else None)

    console.print(table)
