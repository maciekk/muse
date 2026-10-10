"""Durable operational identity for canonical and publishable music items."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path

ITEM_AREAS = frozenset({"master", "stopgap"})


def initialize_database(connection: sqlite3.Connection) -> None:
    """Create the additive operational-ledger schema."""
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS library_items (
            id INTEGER PRIMARY KEY,
            area TEXT NOT NULL CHECK (area IN ('master', 'stopgap')),
            path TEXT NOT NULL UNIQUE,
            current_sha256 TEXT NOT NULL,
            first_seen_at TEXT NOT NULL,
            admitted_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS item_versions (
            id INTEGER PRIMARY KEY,
            item_id INTEGER NOT NULL REFERENCES library_items(id) ON DELETE CASCADE,
            sha256 TEXT NOT NULL,
            size INTEGER NOT NULL CHECK (size >= 0),
            recorded_at TEXT NOT NULL,
            reason TEXT NOT NULL,
            UNIQUE (item_id, sha256)
        )
        """
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS item_versions_sha256_idx ON item_versions (sha256)"
    )


def _normalize_relative(path: Path) -> str:
    if path.is_absolute() or not path.parts or ".." in path.parts:
        raise ValueError(f"operational item path must be repository-relative: {path}")
    area = path.parts[0]
    if area not in ITEM_AREAS:
        raise ValueError(f"operational items must remain beneath master/ or stopgap/: {path}")
    return path.as_posix()


def admit(
    connection: sqlite3.Connection,
    files: Iterable[tuple[Path, int, str]],
    *,
    reason: str = "import",
    observed_at: str | None = None,
) -> tuple[int, ...]:
    """Create stable item rows and initial exact versions, idempotently."""
    timestamp = observed_at or datetime.now(UTC).isoformat()
    item_ids: list[int] = []
    for path, size, sha256 in files:
        path_text = _normalize_relative(path)
        area = path.parts[0]
        row = connection.execute(
            "SELECT id, area, current_sha256 FROM library_items WHERE path = ?",
            (path_text,),
        ).fetchone()
        if row is None:
            cursor = connection.execute(
                """
                INSERT INTO library_items
                    (area, path, current_sha256, first_seen_at, admitted_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (area, path_text, sha256, timestamp, timestamp),
            )
            item_id = int(cursor.lastrowid)
        else:
            item_id, recorded_area, current_sha256 = int(row[0]), str(row[1]), str(row[2])
            if recorded_area != area or current_sha256 != sha256:
                raise ValueError(f"operational item conflicts with imported file: {path_text}")
        connection.execute(
            """
            INSERT OR IGNORE INTO item_versions
                (item_id, sha256, size, recorded_at, reason)
            VALUES (?, ?, ?, ?, ?)
            """,
            (item_id, sha256, size, timestamp, reason),
        )
        item_ids.append(item_id)
    return tuple(item_ids)


def paths_beneath(connection: sqlite3.Connection, source: Path) -> list[str]:
    """Return item paths at or below a repository-relative source path."""
    source_text = source.as_posix()
    return [
        str(row[0])
        for row in connection.execute(
            """
            SELECT path FROM library_items
            WHERE path = ? OR substr(path, 1, length(?) + 1) = ? || '/'
            ORDER BY path
            """,
            (source_text, source_text, source_text),
        )
    ]


def validate_relocation(connection: sqlite3.Connection, source: Path, destination: Path) -> int:
    """Check that a plain move preserves registered items' lifecycle area."""
    paths = paths_beneath(connection, source)
    if not paths:
        return 0
    _normalize_relative(destination)
    destination_area = destination.parts[0]
    source_text = source.as_posix()
    areas = {
        str(row[0])
        for row in connection.execute(
            """
            SELECT DISTINCT area FROM library_items
            WHERE path = ? OR substr(path, 1, length(?) + 1) = ? || '/'
            """,
            (source_text, source_text, source_text),
        )
    }
    if areas != {destination_area}:
        raise ValueError("registered items require a lifecycle operation to change areas")
    return len(paths)


def relocate(connection: sqlite3.Connection, source: Path, destination: Path) -> int:
    """Relocate item paths under *source* while retaining IDs and version history."""
    paths = paths_beneath(connection, source)
    if not paths:
        return 0
    validate_relocation(connection, source, destination)
    destination_text = _normalize_relative(destination)
    source_text = source.as_posix()
    area = destination.parts[0]
    replacements = [
        (f"{destination_text}{path[len(source_text):]}", area, path) for path in paths
    ]
    try:
        connection.executemany(
            "UPDATE library_items SET path = ?, area = ? WHERE path = ?", replacements
        )
    except sqlite3.IntegrityError as error:
        raise ValueError("operational item destination already exists") from error
    return len(paths)
