"""Durable Muse-side state for Apple observations and publication."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime

from muse.apple_protocol import AppleRequest, AppleResult, validate_result

PUBLICATION_DECISIONS = frozenset({"include", "exclude"})


@dataclass(frozen=True)
class RecordedObservation:
    """Identity and creation state of a durable Apple observation."""

    observation_id: int
    created: bool


@dataclass(frozen=True)
class RecordedPublicationRun:
    """Identity and creation state of a durable publication run."""

    run_id: int
    created: bool


def initialize_database(connection: sqlite3.Connection) -> None:
    """Create the additive Apple observation and publication schema."""
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
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS apple_publication_decisions (
            item_id INTEGER PRIMARY KEY REFERENCES library_items(id) ON DELETE CASCADE,
            decision TEXT NOT NULL CHECK (decision IN ('include', 'exclude')),
            decided_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS apple_publication_runs (
            id INTEGER PRIMARY KEY,
            batch_id TEXT NOT NULL UNIQUE,
            request_sha256 TEXT NOT NULL,
            request_json TEXT NOT NULL,
            requested_at TEXT NOT NULL,
            recorded_at TEXT NOT NULL,
            result_json TEXT,
            bridge_id TEXT,
            observed_at TEXT,
            library_identity TEXT,
            completed_at TEXT
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS apple_publication_outcomes (
            run_id INTEGER NOT NULL REFERENCES apple_publication_runs(id) ON DELETE CASCADE,
            operation_id TEXT NOT NULL,
            item_id INTEGER NOT NULL REFERENCES library_items(id) ON DELETE RESTRICT,
            source_sha256 TEXT NOT NULL,
            staging_sha256 TEXT NOT NULL,
            staging_size INTEGER NOT NULL CHECK (staging_size >= 0),
            status TEXT CHECK (status IN ('succeeded', 'failed', 'uncertain')),
            apple_persistent_id TEXT,
            error_code TEXT,
            message TEXT,
            PRIMARY KEY (run_id, operation_id)
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS apple_item_mappings (
            item_id INTEGER PRIMARY KEY REFERENCES library_items(id) ON DELETE RESTRICT,
            library_identity TEXT NOT NULL,
            apple_persistent_id TEXT NOT NULL,
            source_sha256 TEXT NOT NULL,
            staging_sha256 TEXT NOT NULL,
            first_published_at TEXT NOT NULL,
            last_verified_at TEXT NOT NULL,
            local_file_location TEXT,
            local_copy_verified INTEGER NOT NULL DEFAULT 0
                CHECK (local_copy_verified IN (0, 1)),
            UNIQUE (library_identity, apple_persistent_id)
        )
        """
    )


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


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


def set_publication_decision(
    connection: sqlite3.Connection,
    item_id: int,
    decision: str,
    *,
    decided_at: str | None = None,
) -> None:
    """Set an item's explicit Apple inclusion or exclusion decision."""
    if decision not in PUBLICATION_DECISIONS:
        raise ValueError("Apple publication decision must be 'include' or 'exclude'")
    initialize_database(connection)
    item = connection.execute(
        "SELECT 1 FROM library_items WHERE id = ?", (item_id,)
    ).fetchone()
    if item is None:
        raise ValueError(f"unknown library item {item_id}")
    timestamp = decided_at or datetime.now(UTC).isoformat()
    connection.execute(
        """
        INSERT INTO apple_publication_decisions (item_id, decision, decided_at)
        VALUES (?, ?, ?)
        ON CONFLICT(item_id) DO UPDATE SET
            decision = excluded.decision,
            decided_at = excluded.decided_at
        """,
        (item_id, decision, timestamp),
    )


def publication_decision(connection: sqlite3.Connection, item_id: int) -> str | None:
    """Return an explicit publication decision; absence means unpublished by default."""
    initialize_database(connection)
    row = connection.execute(
        "SELECT decision FROM apple_publication_decisions WHERE item_id = ?", (item_id,)
    ).fetchone()
    return str(row[0]) if row is not None else None


def create_publication_run(
    connection: sqlite3.Connection,
    request: AppleRequest,
    *,
    recorded_at: str | None = None,
) -> RecordedPublicationRun:
    """Persist an immutable, exclusion-aware add-track request before exchange."""
    request = AppleRequest.from_dict(request.to_dict())
    add_operations = [
        operation for operation in request.operations if operation.action == "add-track"
    ]
    if not add_operations or len(add_operations) != len(request.operations):
        raise ValueError("publication runs require only add-track operations")
    item_ids = [operation.item_id for operation in add_operations]
    if len(set(item_ids)) != len(item_ids):
        raise ValueError("publication runs must not add the same item more than once")
    initialize_database(connection)
    request_json = _canonical_json(request.to_dict())
    existing = connection.execute(
        "SELECT id, request_sha256, request_json FROM apple_publication_runs WHERE batch_id = ?",
        (request.batch_id,),
    ).fetchone()
    if existing is not None:
        if (str(existing[1]), str(existing[2])) != (request.sha256, request_json):
            raise ValueError(
                f"Apple publication batch {request.batch_id} conflicts with durable state"
            )
        return RecordedPublicationRun(int(existing[0]), False)

    for operation in add_operations:
        assert operation.item_id is not None
        assert operation.source_sha256 is not None
        assert operation.staged_file is not None
        row = connection.execute(
            "SELECT area, current_sha256 FROM library_items WHERE id = ?", (operation.item_id,)
        ).fetchone()
        if row is None:
            raise ValueError(f"publication operation references unknown item {operation.item_id}")
        if str(row[1]) != operation.source_sha256:
            raise ValueError(f"publication source version is stale for item {operation.item_id}")
        decision = publication_decision(connection, operation.item_id)
        if decision != "include":
            state = "explicitly excluded" if decision == "exclude" else "not explicitly included"
            raise ValueError(f"library item {operation.item_id} is {state} for Apple publication")
        expected_playlist = "Muse — Master" if str(row[0]) == "master" else "Muse — Stopgap"
        if operation.playlist_memberships != (expected_playlist,):
            raise ValueError(
                f"publication playlist does not match item {operation.item_id} lifecycle area"
            )

    timestamp = recorded_at or datetime.now(UTC).isoformat()
    cursor = connection.execute(
        """
        INSERT INTO apple_publication_runs
            (batch_id, request_sha256, request_json, requested_at, recorded_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (request.batch_id, request.sha256, request_json, request.created_at, timestamp),
    )
    run_id = int(cursor.lastrowid)
    connection.executemany(
        """
        INSERT INTO apple_publication_outcomes
            (run_id, operation_id, item_id, source_sha256, staging_sha256, staging_size)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            (
                run_id,
                operation.operation_id,
                operation.item_id,
                operation.source_sha256,
                operation.staged_file.sha256,
                operation.staged_file.size,
            )
            for operation in add_operations
            if operation.staged_file is not None
        ),
    )
    return RecordedPublicationRun(run_id, True)


def record_publication_result(
    connection: sqlite3.Connection,
    request: AppleRequest,
    result: AppleResult,
    *,
    completed_at: str | None = None,
) -> int:
    """Record correlated outcomes and successful item mappings atomically and idempotently."""
    validate_result(request, result)
    initialize_database(connection)
    run = connection.execute(
        """
        SELECT id, request_sha256, request_json, result_json
        FROM apple_publication_runs WHERE batch_id = ?
        """,
        (request.batch_id,),
    ).fetchone()
    if run is None:
        raise ValueError(f"Apple publication batch {request.batch_id} has no durable run")
    request_json = _canonical_json(request.to_dict())
    if (str(run[1]), str(run[2])) != (request.sha256, request_json):
        raise ValueError(f"Apple publication batch {request.batch_id} conflicts with durable state")
    result_json = _canonical_json(result.to_dict())
    if run[3] is not None:
        if str(run[3]) != result_json:
            raise ValueError(
                f"Apple publication result {request.batch_id} conflicts with durable state"
            )
        return int(run[0])

    run_id = int(run[0])
    operations = {operation.operation_id: operation for operation in request.operations}
    if any(
        operation.action == "add-track" and outcome.status == "succeeded"
        for operation, outcome in (
            (operations[outcome.operation_id], outcome) for outcome in result.operations
        )
    ) and (result.library is None or result.library.identity is None):
        raise ValueError("successful publication requires a Music.app library identity")

    timestamp = completed_at or datetime.now(UTC).isoformat()
    for outcome in result.operations:
        operation = operations[outcome.operation_id]
        if operation.action != "add-track":
            raise ValueError("publication run result contains a non-publication operation")
        connection.execute(
            """
            UPDATE apple_publication_outcomes
            SET status = ?, apple_persistent_id = ?, error_code = ?, message = ?
            WHERE run_id = ? AND operation_id = ?
            """,
            (
                outcome.status,
                outcome.apple_persistent_id,
                outcome.error_code,
                outcome.message,
                run_id,
                outcome.operation_id,
            ),
        )
        if outcome.status != "succeeded":
            continue
        assert operation.item_id is not None
        assert operation.source_sha256 is not None
        assert operation.staged_file is not None
        assert outcome.apple_persistent_id is not None
        assert result.library is not None and result.library.identity is not None
        existing = connection.execute(
            """
            SELECT library_identity, apple_persistent_id, first_published_at
            FROM apple_item_mappings WHERE item_id = ?
            """,
            (operation.item_id,),
        ).fetchone()
        first_published_at = str(existing[2]) if existing is not None else result.observed_at
        connection.execute(
            """
            INSERT INTO apple_item_mappings (
                item_id, library_identity, apple_persistent_id, source_sha256,
                staging_sha256, first_published_at, last_verified_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(item_id) DO UPDATE SET
                library_identity = excluded.library_identity,
                apple_persistent_id = excluded.apple_persistent_id,
                source_sha256 = excluded.source_sha256,
                staging_sha256 = excluded.staging_sha256,
                last_verified_at = excluded.last_verified_at,
                local_file_location = NULL,
                local_copy_verified = 0
            """,
            (
                operation.item_id,
                result.library.identity,
                outcome.apple_persistent_id,
                operation.source_sha256,
                operation.staged_file.sha256,
                first_published_at,
                result.observed_at,
            ),
        )

    connection.execute(
        """
        UPDATE apple_publication_runs
        SET result_json = ?, bridge_id = ?, observed_at = ?, library_identity = ?,
            completed_at = ?
        WHERE id = ?
        """,
        (
            result_json,
            result.bridge_id,
            result.observed_at,
            result.library.identity if result.library is not None else None,
            timestamp,
            run_id,
        ),
    )
    return run_id
