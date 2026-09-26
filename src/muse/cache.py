"""Maintenance operations for Muse's reusable file-hash cache."""

from __future__ import annotations

import os
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path
from time import perf_counter

from muse.duplicates import _initialize_database


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
        _initialize_database(connection)
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
