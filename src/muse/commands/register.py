"""Existing-Master operational identity registration workflow."""

from __future__ import annotations

import argparse
from pathlib import Path

from muse.commands.progress import _RegistrationProgressDisplay
from muse.config import resolve_target
from muse.registration import RegistrationPlan, RegistrationValidationError
from muse.registration import abort_plan as abort_registration_plan
from muse.registration import apply_plan as apply_registration_plan
from muse.registration import load_plan as load_registration_plan
from muse.registration import make_plan as make_registration_plan
from muse.reporting import emit_json, human_bytes, human_number, make_console, print_table


def _show_plan(plan: RegistrationPlan, color: str, *, show_all: bool = False) -> None:
    console = make_console(color)
    console.print("[bold]Existing Master registration plan[/bold]")
    console.print("[dim]Target[/dim]", plan.target)
    print_table(
        console,
        ("METRIC", "VALUE"),
        [
            ("Status", plan.state),
            ("Files to register", human_number(len(plan.files))),
            ("Already registered", human_number(len(plan.already_registered))),
            ("Non-audio files ignored", human_number(plan.ignored_files)),
            ("Validated size", human_bytes(plan.logical_bytes)),
        ],
        right_aligned=frozenset({"VALUE"}),
    )
    if plan.files:
        console.print("\n[bold]Files to register[/bold]")
        shown = plan.files if show_all else plan.files[:20]
        print_table(
            console,
            ("SIZE", "PATH"),
            [(human_bytes(item.size), item.path) for item in shown],
            right_aligned=frozenset({"SIZE"}),
        )
        omitted = len(plan.files) - len(shown)
        if omitted:
            console.print(
                f"[dim]… {human_number(omitted)} more files; use --all to show them.[/dim]"
            )


def _register(args: argparse.Namespace, root: Path) -> int:
    if (args.apply or args.abort) and args.target is not None:
        make_console(args.color, stderr=True).print(
            "[red]Registration refused:[/red] omit PATH with --apply or --abort"
        )
        return 1

    progress: _RegistrationProgressDisplay | None = None
    try:
        if args.abort:
            plan = abort_registration_plan(root)
            action = "aborted"
        elif args.apply:
            plan = load_registration_plan(root)
            if not args.json:
                _show_plan(plan, args.color, show_all=args.all)
            if input("Type REGISTER to apply this plan: ") != "REGISTER":
                make_console(args.color).print("[yellow]Registration cancelled.[/yellow]")
                return 1
            progress = _RegistrationProgressDisplay(args.progress, args.color, applying=True)
            plan = apply_registration_plan(root, progress.update)
            action = "completed"
        else:
            target = resolve_target(root, args.target or "master")
            progress = _RegistrationProgressDisplay(args.progress, args.color)
            plan = make_registration_plan(root, target, progress.update)
            action = "planned"
    except (FileNotFoundError, RegistrationValidationError, ValueError) as error:
        make_console(args.color, stderr=True).print(f"[red]Registration refused:[/red] {error}")
        return 1
    finally:
        if progress is not None:
            progress.stop()

    if args.json:
        emit_json({"action": action, **plan.to_dict()})
    elif action == "aborted":
        make_console(args.color).print(
            "[green]Registration plan aborted; the operational ledger was not changed.[/green]"
        )
    elif action == "completed":
        make_console(args.color).print(
            f"[green]Registered {human_number(len(plan.files))} existing Master files.[/green]"
        )
    else:
        _show_plan(plan, args.color, show_all=args.all)
        make_console(args.color).print(
            "[dim]No item rows were created. Apply with muse register --apply.[/dim]"
        )
    return 0
