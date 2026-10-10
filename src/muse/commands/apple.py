"""Apple bridge observation command workflow."""

from __future__ import annotations

import argparse
from dataclasses import asdict
from pathlib import Path

from muse.apple_exchange import import_result_bundle, load_request_bundle
from muse.reporting import emit_json, human_number, make_console, print_table, status_text


def _external_path(value: str) -> Path:
    return Path(value).expanduser().absolute()


def _apple_status(args: argparse.Namespace, _root: Path) -> int:
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
        "state_created": False,
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
    console.print("[dim]Validated request/result bundles; no Muse state was changed.[/dim]")
    return 1 if failed else 0
