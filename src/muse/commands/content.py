"""Content command workflows and output."""

from __future__ import annotations

import argparse
from pathlib import Path

from rich.console import Console

from muse.commands.progress import _SlagProgressDisplay
from muse.config import MANAGED_AREAS, resolve_target
from muse.importing import ImportPlan, ImportValidationError
from muse.importing import abort_plan as abort_import_plan
from muse.importing import apply_plan as apply_import_plan
from muse.importing import load_plan as load_import_plan
from muse.importing import make_plan as make_import_plan
from muse.importing import plan_path as import_plan_path
from muse.moves import move
from muse.reporting import (
    emit_json,
    human_bytes,
    human_duration,
    human_number,
    make_console,
    print_table,
)
from muse.slag import SlagCopy
from muse.slag import apply as apply_slag
from muse.slag import candidates as slag_candidates
from muse.slag import inventory as slag_inventory
from muse.slag import stats as slag_stats


def _render_slag_entries(
    console: Console,
    root: Path,
    copies: list[SlagCopy],
    args: argparse.Namespace,
    *,
    inventory: bool,
) -> None:
    """Render the selected slag paths using the same grouping in both views."""
    paths = [item.source if inventory else item.destination for item in copies]
    grouped: dict[Path, list[SlagCopy]] = {}
    for path, item in zip(paths, copies, strict=True):
        grouped.setdefault(path.parent, []).append(item)
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
        rows = []
        for path, item in zip(paths, copies, strict=True):
            row = (str(path.relative_to(root / "slag")),)
            if args.long:
                row += (human_bytes(item.size),)
                if inventory:
                    row += (path.suffix.lower() or "(no extension)",)
            rows.append(row)
        headers = ("PATH", "SIZE", "TYPE") if inventory else ("PATH", "SIZE")
        print_table(
            console,
            headers if args.long else ("PATH",),
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
                path = item.source if inventory else item.destination
                detail = f" [dim]{human_bytes(item.size)}[/dim]" if args.long else ""
                console.print(f"        {path.name}{detail}")
            if len(items) > 10:
                console.print(f"        [dim]… {len(items) - 10} more files[/dim]")
            console.print()
    if args.dirs or args.all:
        console.print()


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
            _render_slag_entries(console, root, copies, args, inventory=True)
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
        _render_slag_entries(console, root, copies, args, inventory=False)
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
    display = _SlagProgressDisplay(args.color, sum(item.size for item in copies))
    try:
        copied, skipped = apply_slag(copies, display.update, root=root)
    except ValueError as error:
        make_console(args.color, stderr=True).print(f"[red]Slag refused:[/red] {error}")
        return 1
    finally:
        display.stop()
    if not args.json:
        console.print(
            f"[green]Moved {copied}; removed {skipped} exact existing source copies.[/green]"
        )
    return 0


def _make_import_plan_with_confirmation(
    args: argparse.Namespace, root: Path, source: Path
) -> ImportPlan:
    try:
        return make_import_plan(root, source, args.destination)
    except ImportValidationError as error:
        issue = next(
            (
                finding.message
                for finding in error.findings
                if finding.code == "inconsistent_album_artist"
            ),
            None,
        )
        if issue is None:
            raise
        if args.json:
            raise ValueError(f"{issue}; rerun without --json to review and accept it") from error
        console = make_console(args.color)
        console.print(f"[yellow]Review required:[/yellow] {issue}")
        try:
            accepted = input("Are these album artist tags intentional? [y/N] ")
        except EOFError:
            accepted = ""
        if accepted.strip().lower() not in {"y", "yes"}:
            raise ValueError("inconsistent album artist tags were not accepted") from error
        return make_import_plan(
            root,
            source,
            args.destination,
            accept_inconsistent_album_artists=True,
        )


def _import(args: argparse.Namespace, root: Path) -> int:
    source: Path | None = None
    if args.source is not None:
        source_value = Path(args.source).expanduser()
        if not source_value.is_absolute() and source_value.parts[:1] not in {
            (area,) for area in (*MANAGED_AREAS, "trash")
        }:
            source_value = Path("backlog") / source_value
        source = resolve_target(root, source_value)
    if (args.apply or args.abort) and args.destination is not None:
        make_console(args.color, stderr=True).print(
            "[red]Import refused:[/red] omit the destination with --apply or --abort"
        )
        return 1
    destination_suggested = False
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
        elif source is None:
            plan = load_import_plan(root)
            action = "shown"
        elif args.destination is not None:
            plan = _make_import_plan_with_confirmation(args, root, source)
            action = "planned"
        elif import_plan_path(root, source).is_file():
            plan = load_import_plan(root, source)
            action = "shown"
        else:
            plan = _make_import_plan_with_confirmation(args, root, source)
            action = "planned"
            destination_suggested = True
    except (FileNotFoundError, ValueError) as error:
        make_console(args.color, stderr=True).print(f"[red]Import refused:[/red] {error}")
        return 1

    payload = {
        "action": action,
        "destination_suggested": destination_suggested,
        **plan.to_dict(),
    }
    if args.json:
        emit_json(payload)
    else:
        console = make_console(args.color)
        if action in {"planned", "shown"}:
            _show_import_plan(console, plan, destination_suggested=destination_suggested)
            if action == "planned":
                message = "No files were moved. Apply with muse import --apply."
                if plan.fixups:
                    message = (
                        "The listed metadata fixups were applied; no files were moved. "
                        "Apply with muse import --apply."
                    )
                console.print(f"[dim]{message}[/dim]")
        elif action == "aborted":
            console.print("[green]Import plan aborted; music was not changed.[/green]")
        else:
            console.print(f"[green]Imported[/green] {plan.source} → {plan.destination}")
    return 0


def _show_import_plan(
    console: Console, plan: ImportPlan, *, destination_suggested: bool = False
) -> None:
    console.print("[bold]Strictly validated import plan[/bold]")
    console.print("[dim]Source[/dim]", plan.source)
    label = "Suggested destination" if destination_suggested else "Destination"
    console.print(f"[dim]{label}[/dim]", plan.destination)
    print_table(
        console,
        ("METRIC", "VALUE"),
        [
            ("Status", plan.state),
            ("Profile", plan.profile),
            ("Validated audio files", human_number(len(plan.files))),
            ("Preserved artifacts", human_number(len(plan.artifacts))),
            ("Automatic fixups", human_number(len(plan.fixups))),
            ("Duration", human_duration(plan.duration_seconds)),
            ("Logical size", human_bytes(plan.logical_bytes)),
        ],
        right_aligned=frozenset({"VALUE"}),
    )
    for fixup in plan.fixups:
        console.print(f"[cyan]Fixed:[/cyan] {fixup}")
    for warning in plan.warnings:
        console.print(f"[yellow]Warning:[/yellow] {warning}")


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
