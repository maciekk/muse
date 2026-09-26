"""Persistent exact-file hashing and duplicate reporting."""

from __future__ import annotations

import hashlib
import os
import sqlite3
from collections import Counter, defaultdict
from collections.abc import Callable, Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from time import perf_counter
from typing import Any

from muse.repository import ScanError


@dataclass(frozen=True)
class ProgressUpdate:
    """A phase update suitable for terminal or programmatic progress reporting."""

    phase: str
    completed_files: int = 0
    completed_bytes: int = 0
    total_files: int | None = None
    total_bytes: int | None = None
    cached_files: int = 0
    cached_bytes: int = 0
    worker_threads: int = 0


ProgressCallback = Callable[[ProgressUpdate], None]


@dataclass(frozen=True)
class FileCandidate:
    path: Path
    size: int
    mtime_ns: int
    cached_sha256: str | None


@dataclass(frozen=True)
class HashedFile:
    path: str
    size: int
    sha256: str


@dataclass(frozen=True)
class TreeDuplicateGroup:
    sha256: str
    files: int
    logical_bytes: int
    paths: tuple[str, ...]

    @property
    def logical_repeated_bytes(self) -> int:
        return self.logical_bytes * (len(self.paths) - 1)

    def to_dict(self) -> dict[str, Any]:
        return {
            "sha256": self.sha256,
            "files_per_copy": self.files,
            "logical_bytes_per_copy": self.logical_bytes,
            "occurrences": len(self.paths),
            "redundant_occurrences": len(self.paths) - 1,
            "logical_repeated_bytes": self.logical_repeated_bytes,
            "directories": list(self.paths),
        }


@dataclass(frozen=True)
class DuplicateGroup:
    sha256: str
    size: int
    paths: tuple[str, ...]

    @property
    def logical_repeated_bytes(self) -> int:
        return self.size * (len(self.paths) - 1)

    def to_dict(self) -> dict[str, Any]:
        return {
            "sha256": self.sha256,
            "size": self.size,
            "occurrences": len(self.paths),
            "redundant_occurrences": len(self.paths) - 1,
            "logical_repeated_bytes": self.logical_repeated_bytes,
            "files": list(self.paths),
        }


@dataclass
class DuplicateReport:
    target: str
    database: str
    files: int = 0
    logical_bytes: int = 0
    hash_candidate_files: int = 0
    hash_candidate_bytes: int = 0
    hashed_files: int = 0
    hashed_bytes: int = 0
    cached_files: int = 0
    cached_bytes: int = 0
    bytes_read: int = 0
    hash_worker_threads: int = 0
    inventory_seconds: float = 0.0
    hashing_seconds: float = 0.0
    analysis_seconds: float = 0.0
    elapsed_seconds: float = 0.0
    groups: list[DuplicateGroup] = field(default_factory=list)
    tree_groups: list[TreeDuplicateGroup] = field(default_factory=list)
    errors: list[ScanError] = field(default_factory=list)

    @property
    def duplicate_occurrences(self) -> int:
        return sum(len(group.paths) for group in self.groups)

    @property
    def redundant_occurrences(self) -> int:
        return sum(len(group.paths) - 1 for group in self.groups)

    @property
    def logical_repeated_bytes(self) -> int:
        return sum(group.logical_repeated_bytes for group in self.groups)

    @property
    def cache_hit_rate(self) -> float:
        if not self.hash_candidate_files:
            return 0.0
        return self.cached_files / self.hash_candidate_files

    @property
    def byte_cache_hit_rate(self) -> float:
        if not self.hash_candidate_bytes:
            return 0.0
        return self.cached_bytes / self.hash_candidate_bytes

    @property
    def hash_throughput(self) -> float:
        return self.bytes_read / self.hashing_seconds if self.hashing_seconds else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "target": self.target,
            "database": self.database,
            "files": self.files,
            "logical_bytes": self.logical_bytes,
            "hash_candidate_files": self.hash_candidate_files,
            "hash_candidate_bytes": self.hash_candidate_bytes,
            "hashed_files": self.hashed_files,
            "hashed_bytes": self.hashed_bytes,
            "cached_files": self.cached_files,
            "cached_bytes": self.cached_bytes,
            "cache_hit_rate": self.cache_hit_rate,
            "byte_cache_hit_rate": self.byte_cache_hit_rate,
            "bytes_read": self.bytes_read,
            "hash_worker_threads": self.hash_worker_threads,
            "hash_throughput_bytes_per_second": self.hash_throughput,
            "inventory_seconds": self.inventory_seconds,
            "hashing_seconds": self.hashing_seconds,
            "analysis_seconds": self.analysis_seconds,
            "elapsed_seconds": self.elapsed_seconds,
            "duplicate_groups": len(self.groups),
            "duplicate_occurrences": self.duplicate_occurrences,
            "redundant_occurrences": self.redundant_occurrences,
            "logical_repeated_bytes": self.logical_repeated_bytes,
            "groups": [group.to_dict() for group in self.groups],
            "tree_duplicate_groups": len(self.tree_groups),
            "tree_groups": [group.to_dict() for group in self.tree_groups],
            "errors": [error.to_dict() for error in self.errors],
        }


def _initialize_database(connection: sqlite3.Connection) -> None:
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
    connection.execute(
        "CREATE INDEX IF NOT EXISTS file_hashes_sha256_idx ON file_hashes (sha256)"
    )
    connection.commit()


def _iter_files(target: Path, excluded: Path, errors: list[ScanError]) -> Iterator[Path]:
    if target.is_file():
        yield target
        return

    pending = [target]
    while pending:
        directory = pending.pop()
        try:
            with os.scandir(directory) as entries:
                children = sorted(entries, key=lambda entry: entry.name, reverse=True)
        except OSError as error:
            errors.append(ScanError(str(directory), str(error)))
            continue

        for entry in children:
            path = Path(entry.path)
            try:
                if path == excluded or entry.is_symlink():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    pending.append(path)
                elif entry.is_file(follow_symlinks=False):
                    yield path
            except OSError as error:
                errors.append(ScanError(str(path), str(error)))


def _display_path(path: Path, target: Path) -> str:
    if target.is_file():
        return path.name
    try:
        return str(path.relative_to(target))
    except ValueError:
        return str(path)


def _sha256(path: Path, on_bytes_read: Callable[[int], None]) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(4 * 1024 * 1024):
            digest.update(block)
            on_bytes_read(len(block))
    return digest.hexdigest()


def _notify(callback: ProgressCallback | None, update: ProgressUpdate) -> None:
    if callback is not None:
        callback(update)


def _inventory(
    target: Path,
    excluded: Path,
    connection: sqlite3.Connection,
    report: DuplicateReport,
    rehash: bool,
    progress: ProgressCallback | None,
) -> list[FileCandidate]:
    candidates = []
    discovered_cached_files = 0
    discovered_cached_bytes = 0
    _notify(progress, ProgressUpdate("inventory"))

    for path in _iter_files(target, excluded, report.errors):
        try:
            stat_result = path.stat(follow_symlinks=False)
            cache_row = None
            if not rehash:
                cache_row = connection.execute(
                    """
                    SELECT sha256 FROM file_hashes
                    WHERE path = ? AND size = ? AND mtime_ns = ?
                    """,
                    (str(path.absolute()), stat_result.st_size, stat_result.st_mtime_ns),
                ).fetchone()
            cached_sha256 = str(cache_row[0]) if cache_row is not None else None
            if cached_sha256 is not None:
                discovered_cached_files += 1
                discovered_cached_bytes += stat_result.st_size
            candidates.append(
                FileCandidate(path, stat_result.st_size, stat_result.st_mtime_ns, cached_sha256)
            )
            report.files += 1
            report.logical_bytes += stat_result.st_size
            _notify(
                progress,
                ProgressUpdate(
                    "inventory",
                    completed_files=report.files,
                    completed_bytes=report.logical_bytes,
                    cached_files=discovered_cached_files,
                    cached_bytes=discovered_cached_bytes,
                ),
            )
        except OSError as error:
            report.errors.append(ScanError(str(path), str(error)))

    return candidates


def _collect_hashes(
    candidates: list[FileCandidate],
    target: Path,
    connection: sqlite3.Connection,
    report: DuplicateReport,
    progress: ProgressCallback | None,
    max_threads: int | None,
) -> tuple[dict[str, list[HashedFile]], dict[Path, str]]:
    by_hash: dict[str, list[HashedFile]] = defaultdict(list)
    hashes: dict[Path, str] = {}
    uncached_files = sum(candidate.cached_sha256 is None for candidate in candidates)
    uncached_bytes = sum(
        candidate.size for candidate in candidates if candidate.cached_sha256 is None
    )
    completed_files = 0
    completed_bytes = 0
    pending_writes = 0

    def notify_hashing() -> None:
        _notify(
            progress,
            ProgressUpdate(
                "hashing",
                completed_files=completed_files,
                completed_bytes=completed_bytes,
                total_files=uncached_files,
                total_bytes=uncached_bytes,
                cached_files=report.cached_files,
                cached_bytes=report.cached_bytes,
                worker_threads=report.hash_worker_threads,
            ),
        )

    report.hash_worker_threads = (
        min(max_threads or 16, uncached_files, os.cpu_count() or 1)
        if uncached_files
        else 0
    )
    notify_hashing()

    def record(candidate: FileCandidate, sha256: str) -> None:
        hashes[candidate.path] = sha256
        by_hash[sha256].append(
            HashedFile(
                _display_path(candidate.path, target),
                candidate.size,
                sha256,
            )
        )

    uncached = []
    for candidate in candidates:
        if candidate.cached_sha256 is None:
            uncached.append(candidate)
            continue
        try:
            before = candidate.path.stat(follow_symlinks=False)
            if (before.st_size, before.st_mtime_ns) != (candidate.size, candidate.mtime_ns):
                raise OSError("file changed after inventory")
            record(candidate, candidate.cached_sha256)
        except OSError as error:
            report.errors.append(ScanError(str(candidate.path), str(error)))

    # hashlib releases the GIL while processing these large blocks. Independent
    # files can therefore use multiple cores (and overlap storage latency), while
    # all SQLite writes remain serialized in this thread.
    progress_lock = Lock()

    def hash_candidate(candidate: FileCandidate) -> tuple[FileCandidate, str]:
        before = candidate.path.stat(follow_symlinks=False)
        if (before.st_size, before.st_mtime_ns) != (candidate.size, candidate.mtime_ns):
            raise OSError("file changed after inventory")

        def record_bytes(count: int) -> None:
            # Reading bytes is not completion: the digest still has to finish,
            # be checked against a final stat, and be stored. Keep accounting
            # here, but advance progress only when the future is collected.
            with progress_lock:
                report.bytes_read += count

        sha256 = _sha256(candidate.path, record_bytes)
        after = candidate.path.stat(follow_symlinks=False)
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise OSError("file changed while being hashed")
        return candidate, sha256

    if uncached:
        # Start the longest jobs first. A SHA-256 stream cannot be split across
        # workers, so this minimizes the end-of-run tail where only a few large
        # files remain and the other workers would otherwise be idle.
        uncached.sort(key=lambda candidate: candidate.size, reverse=True)
        with ThreadPoolExecutor(
            max_workers=report.hash_worker_threads, thread_name_prefix="muse-hash"
        ) as executor:
            futures = {
                executor.submit(hash_candidate, candidate): candidate
                for candidate in uncached
            }
            for future in as_completed(futures):
                candidate = futures[future]
                try:
                    candidate, sha256 = future.result()
                except OSError as error:
                    report.errors.append(ScanError(str(candidate.path), str(error)))
                    with progress_lock:
                        completed_files += 1
                        completed_bytes += candidate.size
                        notify_hashing()
                    continue
                connection.execute(
                    """
                    INSERT INTO file_hashes (path, size, mtime_ns, sha256, hashed_at)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(path) DO UPDATE SET
                        size = excluded.size,
                        mtime_ns = excluded.mtime_ns,
                        sha256 = excluded.sha256,
                        hashed_at = excluded.hashed_at
                    """,
                    (
                        str(candidate.path.absolute()),
                        candidate.size,
                        candidate.mtime_ns,
                        sha256,
                        datetime.now(UTC).isoformat(),
                    ),
                )
                record(candidate, sha256)
                report.hashed_files += 1
                report.hashed_bytes += candidate.size
                with progress_lock:
                    completed_files += 1
                    completed_bytes += candidate.size
                    notify_hashing()
                pending_writes += 1
                if pending_writes >= 100:
                    connection.commit()
                    pending_writes = 0

    connection.commit()
    return by_hash, hashes


def _directory_paths(target: Path, excluded: Path, errors: list[ScanError]) -> list[Path]:
    directories = []
    pending = [target]
    while pending:
        directory = pending.pop()
        directories.append(directory)
        try:
            with os.scandir(directory) as entries:
                children = sorted(entries, key=lambda entry: entry.name, reverse=True)
        except OSError as error:
            errors.append(ScanError(str(directory), str(error)))
            continue
        for entry in children:
            path = Path(entry.path)
            try:
                if path != excluded and not entry.is_symlink() and entry.is_dir(
                    follow_symlinks=False
                ):
                    pending.append(path)
            except OSError as error:
                errors.append(ScanError(str(path), str(error)))
    return directories


def _analyze_trees(
    target: Path,
    directories: list[Path],
    hashes: dict[Path, str],
    report: DuplicateReport,
) -> None:
    children: dict[Path, list[tuple[str, str, str]]] = defaultdict(list)
    counts: dict[Path, tuple[int, int]] = {}
    for path, sha256 in hashes.items():
        children[path.parent].append((path.name, "file", sha256))
    digests: dict[Path, str] = {}
    for directory in sorted(directories, key=lambda path: len(path.parts), reverse=True):
        digest = hashlib.sha256()
        files = 0
        logical_bytes = 0
        for name, kind, value in sorted(children[directory]):
            digest.update(f"{kind}\0{name}\0{value}\n".encode())
            if kind == "file":
                files += 1
                logical_bytes += (directory / name).stat(follow_symlinks=False).st_size
            else:
                child_files, child_bytes = counts[directory / name]
                files += child_files
                logical_bytes += child_bytes
        digests[directory] = digest.hexdigest()
        counts[directory] = (files, logical_bytes)
        if directory != target:
            children[directory.parent].append((directory.name, "directory", digests[directory]))

    by_digest: dict[str, list[Path]] = defaultdict(list)
    for directory, digest in digests.items():
        if directory != target:
            by_digest[digest].append(directory)
    duplicate_directories = {
        directory for paths in by_digest.values() if len(paths) > 1 for directory in paths
    }
    for digest, paths in by_digest.items():
        paths = [path for path in paths if path.parent not in duplicate_directories]
        if len(paths) < 2:
            continue
        paths.sort()
        files, logical_bytes = counts[paths[0]]
        # Empty files still participate in structural fingerprints for mixed
        # trees, but an all-zero-byte tree is not a useful duplicate group.
        if logical_bytes == 0:
            continue
        report.tree_groups.append(
            TreeDuplicateGroup(
                digest,
                files,
                logical_bytes,
                tuple(_display_path(path, target) for path in paths),
            )
        )
    report.tree_groups.sort(key=lambda group: (-group.logical_repeated_bytes, group.sha256))


def _analyze(
    by_hash: dict[str, list[HashedFile]],
    report: DuplicateReport,
    progress: ProgressCallback | None,
) -> None:
    _notify(progress, ProgressUpdate("analysis", total_files=len(by_hash)))
    for completed, (sha256, files) in enumerate(by_hash.items(), start=1):
        if len(files) >= 2:
            files.sort(key=lambda item: item.path)
            report.groups.append(
                DuplicateGroup(
                    sha256=sha256,
                    size=files[0].size,
                    paths=tuple(file.path for file in files),
                )
            )
        _notify(
            progress,
            ProgressUpdate("analysis", completed_files=completed, total_files=len(by_hash)),
        )
    report.groups.sort(key=lambda group: (-group.logical_repeated_bytes, group.sha256))


def find_duplicates(
    target: Path | Sequence[Path],
    database: Path,
    *,
    rehash: bool = False,
    trees: bool = False,
    progress: ProgressCallback | None = None,
    max_threads: int | None = None,
) -> DuplicateReport:
    """Find exact duplicates, retaining hashes in SQLite for later scans."""
    if max_threads is not None and max_threads < 1:
        raise ValueError("max_threads must be at least 1")
    started = perf_counter()
    targets = (
        [target.absolute()]
        if isinstance(target, Path)
        else [path.absolute() for path in target]
    )
    database = database.absolute()
    report = DuplicateReport(", ".join(map(str, targets)), str(database))

    for scan_target in targets:
        try:
            exists = scan_target.exists()
            supported = scan_target.is_file() or scan_target.is_dir()
        except OSError as error:
            report.errors.append(ScanError(str(scan_target), str(error)))
            continue
        if not exists:
            report.errors.append(ScanError(str(scan_target), "path does not exist"))
        elif not supported:
            report.errors.append(
                ScanError(str(scan_target), "path is not a regular file or directory")
            )
    if report.errors:
        report.elapsed_seconds = perf_counter() - started
        return report
    if trees and len(targets) != 1:
        report.errors.append(ScanError("--trees", "accepts exactly one target"))
        report.elapsed_seconds = perf_counter() - started
        return report
    display_target = targets[0]

    database.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database)
    _initialize_database(connection)

    try:
        phase_started = perf_counter()
        inventory = []
        for scan_target in targets:
            inventory.extend(
                _inventory(
                    scan_target, database.parent, connection, report, rehash, progress
                )
            )
        size_counts = Counter(candidate.size for candidate in inventory)
        candidates = inventory if trees else [
            candidate
            for candidate in inventory
            if candidate.size > 0 and size_counts[candidate.size] > 1
        ]
        report.hash_candidate_files = len(candidates)
        report.hash_candidate_bytes = sum(candidate.size for candidate in candidates)
        report.cached_files = sum(
            candidate.cached_sha256 is not None for candidate in candidates
        )
        report.cached_bytes = sum(
            candidate.size for candidate in candidates if candidate.cached_sha256 is not None
        )
        report.inventory_seconds = perf_counter() - phase_started

        phase_started = perf_counter()
        by_hash, hashes = _collect_hashes(
            candidates, display_target, connection, report, progress, max_threads
        )
        report.hashing_seconds = perf_counter() - phase_started

        phase_started = perf_counter()
        _analyze(by_hash, report, progress)
        if trees:
            directories = _directory_paths(display_target, database.parent, report.errors)
            if not report.errors:
                _analyze_trees(display_target, directories, hashes, report)
        report.analysis_seconds = perf_counter() - phase_started
    finally:
        connection.close()

    report.elapsed_seconds = perf_counter() - started
    _notify(progress, ProgressUpdate("complete", report.files, report.logical_bytes))
    return report
