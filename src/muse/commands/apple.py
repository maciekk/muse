"""Apple bridge observation command workflow."""

from __future__ import annotations

import argparse
import sqlite3
from dataclasses import asdict
from pathlib import Path

from muse import apple_ledger, apple_publication
from muse.apple_exchange import import_result_bundle, load_request_bundle
from muse.reporting import emit_json, human_number, make_console, print_table, status_text


def _external_path(value: str) -> Path:
    return Path(value).expanduser().absolute()


def _publication_report(plan: apple_publication.PublicationPlan) -> dict[str, object]:
    return {
        "batch_id": plan.batch_id,
        "target": plan.target,
        "mode": plan.mode,
        "state": plan.state,
        "created_at": plan.created_at,
        "items": [asdict(item) for item in plan.items],
        "excluded_paths": list(plan.excluded_paths),
        "already_published_paths": list(plan.already_published_paths),
        "request_bundle": plan.request_bundle,
    }


def _publish_apple(args: argparse.Namespace, root: Path) -> int:
    try:
        if args.result is not None:
            if args.target is not None or args.apply or args.exclude or args.include_excluded:
                raise ValueError("--result cannot be combined with a target or planning options")
            plan = apple_publication.record_result(root, _external_path(args.result))
        elif args.apply:
            if args.target is not None or args.exclude or args.include_excluded:
                raise ValueError("--apply resumes the current plan and accepts no target")
            plan = apple_publication.apply_plan(root)
        else:
            if args.target is None:
                if args.exclude or args.include_excluded:
                    raise ValueError("planning options require a Master target")
                plan = apple_publication.load_plan(root)
            else:
                target = Path(args.target).expanduser()
                target = (
                    (root / target).absolute() if not target.is_absolute() else target.absolute()
                )
                plan = apple_publication.make_plan(
                    root,
                    target,
                    exclude=args.exclude,
                    include_excluded=args.include_excluded,
                )
    except (OSError, sqlite3.Error, ValueError) as error:
        make_console(args.color, stderr=True).print(
            f"[red]Apple publication unavailable:[/red] {error}"
        )
        return 1

    report = _publication_report(plan)
    if args.json:
        emit_json(report)
        return 0

    console = make_console(args.color)
    heading = "Apple publication exclusion" if plan.mode == "exclude" else "Apple publication"
    console.print(f"[bold]{heading}[/bold]")
    console.print("[dim]Target[/dim]", plan.target)
    console.print("[dim]State[/dim]", plan.state)
    console.print("[dim]Selected items[/dim]", human_number(len(plan.items)))
    if plan.excluded_paths:
        console.print(
            f"[yellow]{len(plan.excluded_paths)} explicitly excluded item(s) "
            "were preserved.[/yellow]"
        )
    if plan.already_published_paths:
        console.print(
            f"[dim]{len(plan.already_published_paths)} current item(s) are already published.[/dim]"
        )
    if plan.request_bundle is not None:
        console.print("[green]Request bundle[/green]", plan.request_bundle)
        console.print(
            "[dim]Copy this directory to the Mac and apply it with "
            "muse-apple-bridge.[/dim]"
        )
    elif plan.state == "ready":
        console.print("[dim]Preview only; run muse publish-apple --apply to continue.[/dim]")
    elif plan.state == "completed":
        console.print("[green]Apple publication state recorded.[/green]")
    elif plan.state == "uncertain":
        console.print(
            "[yellow]A bridge outcome is uncertain; retained artifacts require "
            "reconciliation.[/yellow]"
        )
    elif plan.state == "failed":
        console.print("[red]The bridge rejected at least one publication operation.[/red]")
    return 0


def _apple_status(args: argparse.Namespace, root: Path) -> int:
    request_bundle = _external_path(args.request_bundle)
    result_bundle = _external_path(args.result_bundle)
    try:
        request = load_request_bundle(request_bundle)
        result = import_result_bundle(result_bundle, request)
    except (OSError, ValueError) as error:
        make_console(args.color, stderr=True).print(
            f"[red]Apple status unavailable:[/red] {error}"
        )
        return 1

    observation_id: int | None = None
    state_created = False
    if args.record:
        try:
            apple_ledger.validate_observation(request, result)
        except ValueError as error:
            make_console(args.color, stderr=True).print(
                f"[red]Apple observation not recorded:[/red] {error}"
            )
            return 1
        if not root.is_dir():
            make_console(args.color, stderr=True).print(
                f"[red]Apple observation not recorded:[/red] library root does not exist: {root}"
            )
            return 1
        try:
            state_directory = root / ".muse"
            state_directory.mkdir(exist_ok=True)
            with sqlite3.connect(state_directory / "muse.db") as connection:
                recorded = apple_ledger.record_observation(connection, request, result)
            observation_id = recorded.observation_id
            state_created = recorded.created
        except (OSError, sqlite3.Error, ValueError) as error:
            make_console(args.color, stderr=True).print(
                f"[red]Apple observation not recorded:[/red] {error}"
            )
            return 1

    report = {
        "request_bundle": str(request_bundle),
        "result_bundle": str(result_bundle),
        "batch_id": result.batch_id,
        "request_created_at": request.created_at,
        "request_sha256": result.request_sha256,
        "bridge_id": result.bridge_id,
        "observed_at": result.observed_at,
        "capabilities": result.capabilities.to_dict(),
        "library": (
            {
                "identity": result.library.identity,
                "name": result.library.name,
                "application_version": result.library.application_version,
                "total_tracks": result.library.total_tracks,
                "managed_tracks": result.library.managed_tracks,
            }
            if result.library is not None
            else None
        ),
        "operations": [asdict(item) for item in result.operations],
        "observation_id": observation_id,
        "state_created": state_created,
    }
    failed = any(item.status != "succeeded" for item in result.operations)

    if args.json:
        emit_json(report)
        return 1 if failed else 0

    console = make_console(args.color)
    console.print("[bold]Apple bridge observation[/bold]")
    console.print("[dim]Observed[/dim]", result.observed_at)
    console.print("[dim]Request bundle[/dim]", str(request_bundle))
    console.print("[dim]Result bundle[/dim]", str(result_bundle))
    console.print()

    library = result.library
    if library is not None:
        print_table(
            console,
            ("PROPERTY", "VALUE"),
            [
                ("Music.app version", library.application_version or "not reported"),
                ("Library name", library.name or "not reported"),
                ("Library identity", library.identity or "not reported"),
                ("Library tracks", human_number(library.total_tracks)),
                ("Muse-managed tracks", human_number(library.managed_tracks)),
            ],
        )
    else:
        console.print("[yellow]No library observation was returned.[/yellow]")

    console.print("\n[bold]Bridge[/bold]")
    print_table(
        console,
        ("PROPERTY", "VALUE"),
        [
            ("Bridge ID", result.bridge_id),
            ("Bridge version", result.capabilities.bridge_version),
            (
                "Protocol versions",
                ", ".join(map(str, result.capabilities.supported_protocol_versions)),
            ),
            ("Supported actions", ", ".join(result.capabilities.supported_actions) or "none"),
            (
                "Readable properties",
                ", ".join(result.capabilities.readable_properties) or "none",
            ),
            (
                "Writable properties",
                ", ".join(result.capabilities.writable_properties) or "none",
            ),
        ],
    )

    console.print("\n[bold]Operations[/bold]")
    outcomes = {item.operation_id: item for item in result.operations}
    print_table(
        console,
        ("STATUS", "ACTION", "ITEM", "DETAIL"),
        [
            (
                status_text(outcomes[operation.operation_id].status),
                operation.action,
                str(operation.item_id) if operation.item_id is not None else "—",
                outcomes[operation.operation_id].message
                or outcomes[operation.operation_id].error_code
                or "—",
            )
            for operation in request.operations
        ],
    )
    if args.record:
        if state_created:
            console.print(
                "[green]Recorded Apple library observation "
                f"{observation_id} in .muse/muse.db.[/green]"
            )
        else:
            console.print(
                f"[dim]Apple library observation {observation_id} was already recorded.[/dim]"
            )
    else:
        console.print("[dim]Validated request/result bundles; no Muse state was changed.[/dim]")
    return 1 if failed else 0
