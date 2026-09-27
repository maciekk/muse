"""Artwork upgrade command output."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from muse.art import plan
from muse.reporting import emit_json, make_console


def _confirm_override(prompt: str) -> bool:
    try:
        answer = input(prompt)
    except EOFError:
        return False
    return answer.strip().casefold() in {"y", "yes"}


def _can_prompt() -> bool:
    return sys.stdin.isatty()


def _fix_art(args: argparse.Namespace, root: Path) -> int:
    try:
        report = plan(
            root,
            args.targets,
            min_size=args.min_size,
            force=args.force,
            apply=args.apply,
            cover=Path(args.cover).expanduser().absolute() if args.cover else None,
            allow_small=args.allow_small,
            approve_override=_confirm_override if not args.json and _can_prompt() else None,
        )
    except ValueError as error:
        make_console(args.color, stderr=True).print(f"[red]Artwork refused:[/red] {error}")
        return 1
    if args.json:
        emit_json(report)
    else:
        console = make_console(args.color)
        for album in report["albums"]:
            console.print(
                f"[bold]{album['artist']} — {album['album']}[/bold] "
                f"[dim]{album['artwork']} · {album['source']}[/dim]"
            )
            if album["matched_title"] and album["matched_title"] != album["album"]:
                console.print(f"  [cyan]Matched release:[/cyan] {album['matched_title']}")
            for item in album["files"]:
                current = item["existing"] or "none"
                detail = f" — {item['reason']}" if item["reason"] else ""
                console.print(f"  {item['status']}: {item['path']} ({current}){detail}")
                if item["backup"]:
                    console.print(f"    [dim]original: {item['backup']}[/dim]")
        for album in report["skipped"]:
            console.print(
                f"[yellow]Skipped:[/yellow] {album['artist']} — {album['album']}: {album['reason']}"
            )
        for error in report["errors"]:
            console.print(f"[red]Error:[/red] {error}")
        if not args.apply:
            console.print("[dim]Preview only. Use --apply to embed selected artwork.[/dim]")
    return 1 if report["errors"] else 0
