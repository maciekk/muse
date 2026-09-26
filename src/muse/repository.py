"""Read-only filesystem inspection for a Muse repository."""

from __future__ import annotations

import os
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

AUDIO_EXTENSIONS = frozenset(
    {
        ".aac",
        ".aif",
        ".aiff",
        ".alac",
        ".ape",
        ".dsf",
        ".flac",
        ".m4a",
        ".m4b",
        ".mp3",
        ".mp4",
        ".mpc",
        ".ogg",
        ".oga",
        ".opus",
        ".wav",
        ".wma",
        ".wv",
    }
)


@dataclass(frozen=True)
class ScanError:
    path: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {"path": self.path, "message": self.message}


@dataclass
class PathStats:
    files: int = 0
    directories: int = 0
    symlinks: int = 0
    other_entries: int = 0
    audio_files: int = 0
    zero_byte_files: int = 0
    logical_bytes: int = 0
    allocated_bytes: int = 0
    allocated_bytes_available: bool = True
    extensions: Counter[str] = field(default_factory=Counter)
    extension_logical_bytes: Counter[str] = field(default_factory=Counter)
    errors: list[ScanError] = field(default_factory=list)

    @property
    def non_audio_files(self) -> int:
        return self.files - self.audio_files

    def add(self, other: PathStats) -> None:
        self.files += other.files
        self.directories += other.directories
        self.symlinks += other.symlinks
        self.other_entries += other.other_entries
        self.audio_files += other.audio_files
        self.zero_byte_files += other.zero_byte_files
        self.logical_bytes += other.logical_bytes
        self.allocated_bytes += other.allocated_bytes
        self.allocated_bytes_available &= other.allocated_bytes_available
        self.extensions.update(other.extensions)
        self.extension_logical_bytes.update(other.extension_logical_bytes)
        self.errors.extend(other.errors)

    def to_dict(self) -> dict[str, Any]:
        return {
            "files": self.files,
            "directories": self.directories,
            "symlinks": self.symlinks,
            "other_entries": self.other_entries,
            "audio_files": self.audio_files,
            "non_audio_files": self.non_audio_files,
            "zero_byte_files": self.zero_byte_files,
            "logical_bytes": self.logical_bytes,
            "allocated_bytes": (self.allocated_bytes if self.allocated_bytes_available else None),
            "extensions": dict(sorted(self.extensions.items())),
            "extension_logical_bytes": dict(sorted(self.extension_logical_bytes.items())),
            "errors": [error.to_dict() for error in self.errors],
        }


def _record_file(stats: PathStats, path: Path, stat_result: os.stat_result) -> None:
    stats.files += 1
    stats.logical_bytes += stat_result.st_size
    if stat_result.st_size == 0:
        stats.zero_byte_files += 1

    blocks = getattr(stat_result, "st_blocks", None)
    if blocks is None:
        stats.allocated_bytes_available = False
    else:
        # POSIX st_blocks is expressed in 512-byte units, including on macOS.
        stats.allocated_bytes += blocks * 512

    extension = path.suffix.lower() or "[no extension]"
    stats.extensions[extension] += 1
    stats.extension_logical_bytes[extension] += stat_result.st_size
    if extension in AUDIO_EXTENSIONS:
        stats.audio_files += 1


def scan_path(path: Path) -> PathStats:
    """Recursively inspect *path* without following symlinks or writing state."""
    stats = PathStats()

    try:
        if path.is_symlink():
            stats.symlinks = 1
            return stats
        if path.is_file():
            _record_file(stats, path, path.stat(follow_symlinks=False))
            return stats
        if not path.exists():
            stats.errors.append(ScanError(str(path), "path does not exist"))
            return stats
        if not path.is_dir():
            stats.other_entries = 1
            return stats
    except OSError as error:
        stats.errors.append(ScanError(str(path), str(error)))
        return stats

    pending = [path]
    while pending:
        directory = pending.pop()
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    entry_path = Path(entry.path)
                    try:
                        if entry.is_symlink():
                            stats.symlinks += 1
                        elif entry.is_dir(follow_symlinks=False):
                            stats.directories += 1
                            pending.append(entry_path)
                        elif entry.is_file(follow_symlinks=False):
                            _record_file(
                                stats,
                                entry_path,
                                entry.stat(follow_symlinks=False),
                            )
                        else:
                            stats.other_entries += 1
                    except OSError as error:
                        stats.errors.append(ScanError(str(entry_path), str(error)))
        except OSError as error:
            stats.errors.append(ScanError(str(directory), str(error)))

    return stats


def scan_root_by_area(root: Path) -> tuple[PathStats, dict[str, PathStats]]:
    """Scan each top-level entry once and return total and per-area statistics."""
    total = PathStats()
    areas: dict[str, PathStats] = {}

    try:
        entries = sorted(root.iterdir(), key=lambda item: item.name)
    except OSError as error:
        total.errors.append(ScanError(str(root), str(error)))
        return total, areas

    for entry in entries:
        area_stats = scan_path(entry)
        areas[entry.name] = area_stats
        total.add(area_stats)
        if entry.is_dir() and not entry.is_symlink():
            # scan_path counts descendants, not the target directory itself.
            total.directories += 1

    return total, areas
