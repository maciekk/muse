"""Maintenance operations for Muse's reusable file-hash cache."""

from __future__ import annotations

import os
import sqlite3
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter

from muse.hashing import FileIdentity


def initialize_database(connection: sqlite3.Connection) -> None:
    """Create the original cache schema, including its duplicate lookup index."""
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS file_hashes (
            path TEXT PRIMARY KEY,
            size INTEGER NOT NULL,
            mtime_ns INTEGER NOT NULL,
            sha256 TEXT NOT NULL,
            hashed_at TEXT NOT NULL
        )
        """
    )
    connection.execute("CREATE INDEX IF NOT EXISTS file_hashes_sha256_idx ON file_hashes (sha256)")
    connection.commit()


def lookup(connection: sqlite3.Connection, path: Path, identity: FileIdentity) -> str | None:
    """Reuse a metadata-matching digest; callers still verify current file identity."""
    row = connection.execute(
        "SELECT sha256 FROM file_hashes WHERE path = ? AND size = ? AND mtime_ns = ?",
        (str(path.absolute()), identity.size, identity.mtime_ns),
    ).fetchone()
    return str(row[0]) if row is not None else None


def insert(connection: sqlite3.Connection, path: Path, identity: FileIdentity, sha256: str) -> None:
    connection.execute(
        """
        INSERT INTO file_hashes (path, size, mtime_ns, sha256, hashed_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(path) DO UPDATE SET size = excluded.size,
            mtime_ns = excluded.mtime_ns, sha256 = excluded.sha256,
            hashed_at = excluded.hashed_at
        """,
        (
            str(path.absolute()),
            identity.size,
            identity.mtime_ns,
            sha256,
            datetime.now(UTC).isoformat(),
        ),
    )


def relocate(connection: sqlite3.Connection, source: Path, destination: Path) -> int:
    """Update an exact path and its descendants without matching sibling prefixes."""
    source_text, destination_text = str(source.absolute()), str(destination.absolute())
    if source_text == destination_text or Path(destination_text).is_relative_to(source_text):
        raise ValueError("cache relocation requires distinct, non-nested paths")
    condition = """substr(path, 1, length(?)) = ?
          AND (path = ? OR substr(path, length(?) + 1, 1) = '/')"""
    parameters = (source_text, source_text, source_text, source_text)
    count = connection.execute(
        f"SELECT count(*) FROM file_hashes WHERE {condition}", parameters
    ).fetchone()[0]
    connection.execute(
        """
        UPDATE OR IGNORE file_hashes SET path = ? || substr(path, length(?) + 1)
        WHERE substr(path, 1, length(?)) = ?
          AND (path = ? OR substr(path, length(?) + 1, 1) = '/')
        """,
        (destination_text, source_text, source_text, source_text, source_text, source_text),
    )
    connection.execute(f"DELETE FROM file_hashes WHERE {condition}", parameters)
    return int(count)


@dataclass(frozen=True)
class CachePruneReport:
    database: str
    database_exists: bool
    entries_before: int
    entries_removed: int
    entries_after: int
    represented_bytes_before: int
    represented_bytes_removed: int
    represented_bytes_after: int
    elapsed_seconds: float

    def to_dict(self) -> dict[str, str | bool | int | float]:
        return asdict(self)


def prune_missing(database: Path) -> CachePruneReport:
    """Remove cached hashes whose absolute file paths no longer exist."""
    started = perf_counter()
    database = database.absolute()
    if not database.is_file():
        return CachePruneReport(
            database=str(database),
            database_exists=False,
            entries_before=0,
            entries_removed=0,
            entries_after=0,
            represented_bytes_before=0,
            represented_bytes_removed=0,
            represented_bytes_after=0,
            elapsed_seconds=perf_counter() - started,
        )

    with sqlite3.connect(database) as connection:
        initialize_database(connection)
        rows = connection.execute("SELECT path, size FROM file_hashes").fetchall()
        missing = [(str(path), int(size)) for path, size in rows if not os.path.exists(path)]
        connection.executemany(
            "DELETE FROM file_hashes WHERE path = ?",
            ((path,) for path, _size in missing),
        )
        connection.commit()

    represented_before = sum(int(size) for _path, size in rows)
    represented_removed = sum(size for _path, size in missing)
    return CachePruneReport(
        database=str(database),
        database_exists=True,
        entries_before=len(rows),
        entries_removed=len(missing),
        entries_after=len(rows) - len(missing),
        represented_bytes_before=represented_before,
        represented_bytes_removed=represented_removed,
        represented_bytes_after=represented_before - represented_removed,
        elapsed_seconds=perf_counter() - started,
    )
