"""Duplicates command workflows and output."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from rich.text import Text

from muse.cache import CachePruneReport, prune_missing
from muse.commands.progress import _DuplicateProgressDisplay
from muse.config import resolve_target
from muse.duplicates import DuplicateGroup, DuplicateReport, find_duplicates
from muse.reporting import (
    emit_json,
    human_bytes,
    human_duration,
    human_number,
    make_console,
    middle_truncate,
    print_table,
)
from muse.tree_diff import TreeDiffReport, compare_trees


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
            f"{human_bytes(report.bytes_read)} · {human_bytes(round(report.hash_throughput))}/s",
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
            f"{human_bytes(report.bytes_read)} · {human_bytes(round(report.hash_throughput))}/s",
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
    excluded = (root / "slag", root / "trash") if root in targets else ()
    progress = _DuplicateProgressDisplay(args.progress, args.color, trees=args.trees)
    try:
        report = find_duplicates(
            targets,
            root / ".muse" / "muse.db",
            rehash=args.rehash,
            trees=args.trees,
            progress=progress.update,
            max_threads=args.max_threads,
            exclude=excluded,
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
