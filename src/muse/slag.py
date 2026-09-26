"""Copy non-audio backlog artifacts into provenance-preserving slag."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from muse import cache
from muse.filesystem import WalkError, walk
from muse.mutation import checked_path, mutation_lock, verified_copy_remove
from muse.repository import AUDIO_EXTENSIONS, scan_path

AUDIO_ADJACENT_EXTENSIONS = frozenset(
    {
        ".bmp",
        ".cue",
        ".gif",
        ".jpeg",
        ".jpg",
        ".log",
        ".m3u",
        ".m3u8",
        ".md5",
        ".nfo",
        ".pdf",
        ".pls",
        ".png",
        ".sfv",
        ".sha1",
        ".sha256",
        ".sha512",
        ".tif",
        ".tiff",
        ".txt",
        ".webp",
    }
)


@dataclass(frozen=True)
class SlagCopy:
    source: Path
    destination: Path
    size: int


def candidates(root: Path, sources: list[Path], *, thorough: bool = False) -> list[SlagCopy]:
    """List non-audio files beneath backlog sources and their slag destinations."""
    backlog = root / "backlog"
    result = []
    for source in sources:
        source = source.absolute()
        try:
            source.relative_to(backlog)
        except ValueError as error:
            raise ValueError("--from paths must be beneath backlog/") from error
        entries = list(walk(source))
        for item in entries:
            if isinstance(item, WalkError):
                raise OSError(f"{item.path}: {item.message}")
        paths = sorted(
            item.path for item in entries if not isinstance(item, WalkError) and item.kind == "file"
        )
        for path in paths:
            if path.is_symlink() or path.suffix.lower() in AUDIO_EXTENSIONS:
                continue
            if not thorough and path.suffix.lower() in AUDIO_ADJACENT_EXTENSIONS:
                continue
            destination = root / "slag" / path.relative_to(backlog)
            result.append(SlagCopy(path, destination, path.stat().st_size))
    return result


def apply(
    copies: list[SlagCopy],
    progress: Callable[[int, int], None] | None = None,
    *,
    root: Path | None = None,
) -> tuple[int, int]:
    """Move candidates after a verified copy, preserving exact existing destinations."""
    if root is None:
        if not copies:
            return 0, 0
        root = next(
            (
                parent.parent
                for parent in copies[0].source.absolute().parents
                if parent.name == "backlog"
                and copies[0].destination.absolute().is_relative_to(parent.parent / "slag")
            ),
            None,
        )
        if root is None:
            raise ValueError("slag copies must connect backlog/ and slag/ in one library")
    with mutation_lock(root):
        return _apply(copies, progress, root=root)


def _apply(
    copies: list[SlagCopy],
    progress: Callable[[int, int], None] | None,
    *,
    root: Path,
) -> tuple[int, int]:
    moved = skipped = completed_bytes = 0
    database = root / ".muse" / "muse.db"
    for item in copies:
        checked_path(root, item.source.absolute().relative_to(root.absolute()), area="backlog")
        checked_path(root, item.destination.absolute().relative_to(root.absolute()), area="slag")

        def reconcile(copy: SlagCopy = item) -> None:
            if database.exists():
                with sqlite3.connect(database) as connection:
                    cache.initialize_database(connection)
                    cache.relocate(connection, copy.source, copy.destination)

        was_moved = verified_copy_remove(
            item.source, item.destination, before_source_removal=reconcile
        )
        moved += int(was_moved)
        skipped += int(not was_moved)
        completed_bytes += item.size
        if progress:
            progress(completed_bytes, item.size)
    return moved, skipped


def inventory(root: Path) -> list[SlagCopy]:
    """List files already preserved in slag."""
    slag = root / "slag"
    if not slag.exists():
        return []
    entries = list(walk(slag))
    for item in entries:
        if isinstance(item, WalkError):
            raise OSError(f"{item.path}: {item.message}")
    return [
        SlagCopy(item.path, item.path, item.stat.st_size)
        for item in sorted(
            (item for item in entries if not isinstance(item, WalkError) and item.kind == "file"),
            key=lambda item: item.path,
        )
        if item.stat is not None
    ]


def stats(root: Path):
    return scan_path(root / "slag")
