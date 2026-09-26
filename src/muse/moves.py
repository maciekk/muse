"""Safe in-vault moves that preserve cached hash paths."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from muse.duplicates import _initialize_database


@dataclass(frozen=True)
class MoveResult:
    source: str
    destination: str
    cached_paths_updated: int

    def to_dict(self) -> dict[str, str | int]:
        return {
            "source": self.source,
            "destination": self.destination,
            "cached_paths_updated": self.cached_paths_updated,
        }


def _within_root(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def move(root: Path, source: Path, destination: Path) -> MoveResult:
    """Rename a file or directory and atomically update its cached hash paths."""
    root = root.absolute()
    source, destination = source.absolute(), destination.absolute()
    if not _within_root(source, root) or not _within_root(destination, root):
        raise ValueError("source and destination must be inside the library root")
    if source == root or source == root / ".muse" or source.is_relative_to(root / ".muse"):
        raise ValueError("cannot move library operational state")
    if not source.exists():
        raise ValueError("source does not exist")
    if destination.exists():
        if not destination.is_dir():
            raise ValueError("destination already exists and is not a directory")
        destination = destination / source.name
    if destination.exists():
        raise ValueError("destination already contains an entry with that name")
    if not destination.parent.is_dir():
        raise ValueError("destination parent does not exist")
    if source.is_dir() and destination.is_relative_to(source):
        raise ValueError("cannot move a directory into itself")

    database = root / ".muse" / "muse.db"
    database.parent.mkdir(parents=True, exist_ok=True)
    source_text, destination_text = str(source), str(destination)
    source.rename(destination)
    try:
        with sqlite3.connect(database) as connection:
            _initialize_database(connection)
            cursor = connection.execute(
                """
                UPDATE file_hashes
                SET path = ? || substr(path, length(?) + 1)
                WHERE substr(path, 1, length(?)) = ?
                  AND (path = ? OR substr(path, length(?) + 1, 1) = '/')
                """,
                (
                    destination_text,
                    source_text,
                    source_text,
                    source_text,
                    source_text,
                    source_text,
                ),
            )
            updated = cursor.rowcount
    except Exception:
        destination.rename(source)
        raise
    return MoveResult(source_text, destination_text, updated)
