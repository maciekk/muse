"""Durable Muse-side state for Apple library observations."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime

from muse.apple_protocol import AppleRequest, AppleResult, validate_result


@dataclass(frozen=True)
class RecordedObservation:
    """Identity and creation state of a durable Apple observation."""

    observation_id: int
    created: bool


def initialize_database(connection: sqlite3.Connection) -> None:
    """Create the additive Apple observation schema."""
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS apple_library_observations (
            id INTEGER PRIMARY KEY,
            batch_id TEXT NOT NULL UNIQUE,
            request_sha256 TEXT NOT NULL,
            request_created_at TEXT NOT NULL,
            bridge_id TEXT NOT NULL,
            observed_at TEXT NOT NULL,
            recorded_at TEXT NOT NULL,
            library_identity TEXT,
            library_name TEXT,
            application_version TEXT,
            total_tracks INTEGER NOT NULL CHECK (total_tracks >= 0),
            managed_tracks INTEGER NOT NULL CHECK (
                managed_tracks >= 0 AND managed_tracks <= total_tracks
            ),
            capabilities_json TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS apple_library_observations_library_idx
        ON apple_library_observations (library_identity, observed_at)
        """
    )


def _capabilities_json(result: AppleResult) -> str:
    return json.dumps(
        result.capabilities.to_dict(), ensure_ascii=False, separators=(",", ":"), sort_keys=True
    )


def _stored_values(request: AppleRequest, result: AppleResult) -> tuple[object, ...]:
    library = result.library
    assert library is not None
    return (
        result.request_sha256,
        request.created_at,
        result.bridge_id,
        result.observed_at,
        library.identity,
        library.name,
        library.application_version,
        library.total_tracks,
        library.managed_tracks,
        _capabilities_json(result),
    )


def validate_observation(request: AppleRequest, result: AppleResult) -> None:
    """Require a valid result containing a successful library inspection."""
    validate_result(request, result)
    outcomes = {outcome.operation_id: outcome for outcome in result.operations}
    successful_inspection = any(
        operation.action == "inspect-library"
        and outcomes[operation.operation_id].status == "succeeded"
        for operation in request.operations
    )
    if not successful_inspection or result.library is None:
        raise ValueError("result contains no successful Music.app library observation")


def record_observation(
    connection: sqlite3.Connection,
    request: AppleRequest,
    result: AppleResult,
    *,
    recorded_at: str | None = None,
) -> RecordedObservation:
    """Record a successful inspection, accepting an exact retry idempotently."""
    validate_observation(request, result)
    initialize_database(connection)
    values = _stored_values(request, result)
    existing = connection.execute(
        """
        SELECT id, request_sha256, request_created_at, bridge_id, observed_at,
               library_identity, library_name, application_version, total_tracks,
               managed_tracks, capabilities_json
        FROM apple_library_observations
        WHERE batch_id = ?
        """,
        (result.batch_id,),
    ).fetchone()
    if existing is not None:
        if tuple(existing[1:]) != values:
            raise ValueError(
                f"Apple observation batch {result.batch_id} conflicts with durable state"
            )
        return RecordedObservation(int(existing[0]), False)

    timestamp = recorded_at or datetime.now(UTC).isoformat()
    cursor = connection.execute(
        """
        INSERT INTO apple_library_observations (
            batch_id, request_sha256, request_created_at, bridge_id, observed_at,
            recorded_at, library_identity, library_name, application_version,
            total_tracks, managed_tracks, capabilities_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            result.batch_id,
            values[0],
            values[1],
            values[2],
            values[3],
            timestamp,
            values[4],
            values[5],
            values[6],
            values[7],
            values[8],
            values[9],
        ),
    )
    return RecordedObservation(int(cursor.lastrowid), True)
