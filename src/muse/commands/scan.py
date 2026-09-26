"""Scan command workflows and output."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from rich.text import Text

from muse.commands.overview import _print_extension_table
from muse.commands.progress import _PullProgressDisplay, _ScanProgressDisplay
from muse.reporting import (
    emit_json,
    human_bytes,
    human_number,
    make_console,
    print_table,
)
from muse.scanning import ScanComparison, ScannedFile, compare_with_vault, pull_new_directories


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
        _print_extension_table(console, report.target_extensions, report.target_extension_bytes)
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
