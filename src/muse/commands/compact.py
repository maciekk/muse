"""Compact command workflows and output."""

from __future__ import annotations

import argparse
from difflib import SequenceMatcher
from pathlib import Path

from rich.console import Console
from rich.text import Text

from muse.commands.progress import _CompactProgressDisplay
from muse.compaction import (
    CompactOperation,
    apply_plan,
    load_plan,
    make_plan,
    save_plan,
    verification_workers,
)
from muse.config import resolve_target
from muse.reporting import (
    human_bytes,
    human_number,
    make_console,
    print_table,
)


def _compact_rows(operations: list[CompactOperation]) -> list[tuple[str, str]]:
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
    console: Console, operations: list[CompactOperation], *, limit: int | None = 10
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
        distinct_paths = len({path for item in operations for path in (item.retain, item.remove)})
        workers = verification_workers(distinct_paths, args.max_threads)
        console.print(
            f"[dim]Reverifying planned trees before moving them to trash "
            f"using {workers} worker {'thread' if workers == 1 else 'threads'}…[/dim]"
        )
        progress = _CompactProgressDisplay(args.color)
        try:
            receipt = apply_plan(root, operations, progress.update, max_threads=args.max_threads)
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
    worker_limit = args.max_threads or 16
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
