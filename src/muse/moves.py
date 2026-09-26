"""Safe in-vault moves with resumable cache reconciliation."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from muse import cache
from muse.mutation import (
    atomic_json,
    checked_path,
    endpoint_state,
    mutation_lock,
    occupied,
    rename_exact,
    sync_directory,
)


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


def _identity(path: Path) -> list[int]:
    stat = path.stat(follow_symlinks=False)
    return [stat.st_dev, stat.st_ino]


def _move_entry(source: Path, destination: Path, identity: list[int]) -> None:
    state = endpoint_state(source, destination)
    if state == "conflicting":
        raise ValueError(f"both source and destination exist: {source}")
    if state == "missing":
        raise ValueError(f"neither source nor destination exists: {source}")
    current = source if state == "pending" else destination
    if current.is_symlink() or _identity(current) != identity:
        raise ValueError(f"planned move content has changed: {current}")
    if current == source:
        destination.parent.mkdir(parents=True, exist_ok=True)
        rename_exact(source, destination)


def move(root: Path, source: Path, destination: Path) -> MoveResult:
    """Move a path and its companion slag path, resuming an interrupted rename."""
    with mutation_lock(root):
        return _move(root, source, destination)


def _move(root: Path, source: Path, destination: Path) -> MoveResult:
    root = root.absolute()
    source, requested = source.absolute(), destination.absolute()
    if not source.is_relative_to(root) or not requested.is_relative_to(root):
        raise ValueError("source and destination must be inside the library root")
    checked_path(root, source.relative_to(root))
    checked_path(root, requested.relative_to(root))
    if source == root or source == root / ".muse" or source.is_relative_to(root / ".muse"):
        raise ValueError("cannot move library operational state")

    journal = root / ".muse" / "move.json"
    if journal.is_symlink():
        raise ValueError("move journal must not be a symlink")
    if journal.exists():
        record = json.loads(journal.read_text())
        if record.get("source") != str(source) or record.get("requested") != str(requested):
            raise ValueError("an interrupted move exists; retry the original move first")
        destination = Path(record["destination"])
        entries = record["entries"]
    else:
        if not occupied(source):
            raise ValueError("source does not exist")
        if occupied(requested):
            if requested.is_symlink() or not requested.is_dir():
                raise ValueError("destination already exists and is not a directory")
            destination = requested / source.name
        else:
            destination = requested
        checked_path(root, destination.relative_to(root))
        if occupied(destination):
            raise ValueError("destination already contains an entry with that name")
        if not destination.parent.is_dir():
            raise ValueError("destination parent does not exist")
        if source.is_dir() and destination.is_relative_to(source):
            raise ValueError("cannot move a directory into itself")
        entries = []
        backlog = root / "backlog"
        if source.is_relative_to(backlog):
            slag_source = root / "slag" / source.relative_to(backlog)
            slag_destination = root / "slag" / destination.relative_to(backlog)
            checked_path(root, slag_source.relative_to(root))
            checked_path(root, slag_destination.relative_to(root))
            if occupied(slag_source):
                if occupied(slag_destination):
                    raise ValueError("corresponding slag destination already exists")
                entries.append(
                    {
                        "source": str(slag_source),
                        "destination": str(slag_destination),
                        "identity": _identity(slag_source),
                    }
                )
        entries.append(
            {
                "source": str(source),
                "destination": str(destination),
                "identity": _identity(source),
            }
        )
        atomic_json(
            journal,
            {
                "schema_version": 1,
                "source": str(source),
                "requested": str(requested),
                "destination": str(destination),
                "entries": entries,
            },
        )

    for entry in entries:
        entry_source = checked_path(root, Path(entry["source"]).relative_to(root))
        entry_destination = checked_path(root, Path(entry["destination"]).relative_to(root))
        _move_entry(entry_source, entry_destination, entry["identity"])

    database = root / ".muse" / "muse.db"
    updated = 0
    if database.exists():
        with sqlite3.connect(database) as connection:
            cache.initialize_database(connection)
            for entry in entries:
                updated += cache.relocate(
                    connection, Path(entry["source"]), Path(entry["destination"])
                )
    journal.unlink()
    sync_directory(journal.parent)
    return MoveResult(str(source), str(destination), updated)
