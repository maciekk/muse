"""Read-only structural comparison of two directory trees."""

from __future__ import annotations

import hashlib
import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import Any

from muse.repository import ScanError


@dataclass(frozen=True)
class TreeDifference:
    kind: str
    path: str
    left: str | None = None
    right: str | None = None

    def to_dict(self) -> dict[str, str | None]:
        return {"kind": self.kind, "path": self.path, "left": self.left, "right": self.right}


@dataclass(frozen=True)
class TreeFingerprint:
    sha256: str
    files: int
    logical_bytes: int


@dataclass
class TreeDiffReport:
    left: str
    right: str
    database: str
    left_files: int = 0
    right_files: int = 0
    hashed_files: int = 0
    cached_files: int = 0
    elapsed_seconds: float = 0.0
    differences: list[TreeDifference] = field(default_factory=list)
    errors: list[ScanError] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "left": self.left,
            "right": self.right,
            "database": self.database,
            "left_files": self.left_files,
            "right_files": self.right_files,
            "hashed_files": self.hashed_files,
            "cached_files": self.cached_files,
            "elapsed_seconds": self.elapsed_seconds,
            "differences": [difference.to_dict() for difference in self.differences],
            "errors": [error.to_dict() for error in self.errors],
        }


def _initialize_database(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS file_hashes (
            path TEXT PRIMARY KEY, size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL,
            sha256 TEXT NOT NULL, hashed_at TEXT NOT NULL
        )
        """
    )
    connection.commit()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(4 * 1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _snapshot(
    root: Path,
    database_parent: Path,
    connection: sqlite3.Connection,
    report: TreeDiffReport,
) -> tuple[set[str], dict[str, tuple[str, int]]]:
    directories = {"."}
    files: dict[str, tuple[str, int]] = {}
    for directory, child_directories, child_files in os.walk(root, followlinks=False):
        current = Path(directory)
        child_directories[:] = [
            child for child in child_directories if current / child != database_parent
        ]
        for child in child_directories:
            directories.add(str((current / child).relative_to(root)))
        for child in child_files:
            path = current / child
            try:
                if path.is_symlink():
                    continue
                stat_result = path.stat(follow_symlinks=False)
                row = connection.execute(
                    """
                    SELECT sha256 FROM file_hashes WHERE path = ? AND size = ? AND mtime_ns = ?
                    """,
                    (str(path.absolute()), stat_result.st_size, stat_result.st_mtime_ns),
                ).fetchone()
                if row is None:
                    sha256 = _sha256(path)
                    after = path.stat(follow_symlinks=False)
                    if (stat_result.st_size, stat_result.st_mtime_ns) != (
                        after.st_size,
                        after.st_mtime_ns,
                    ):
                        raise OSError("file changed while being hashed")
                    connection.execute(
                        """
                        INSERT INTO file_hashes (path, size, mtime_ns, sha256, hashed_at)
                        VALUES (?, ?, ?, ?, ?)
                        ON CONFLICT(path) DO UPDATE SET size = excluded.size,
                            mtime_ns = excluded.mtime_ns, sha256 = excluded.sha256,
                            hashed_at = excluded.hashed_at
                        """,
                        (
                            str(path.absolute()), stat_result.st_size, stat_result.st_mtime_ns,
                            sha256, datetime.now(UTC).isoformat(),
                        ),
                    )
                    report.hashed_files += 1
                else:
                    sha256 = str(row[0])
                    report.cached_files += 1
                files[str(path.relative_to(root))] = (sha256, stat_result.st_size)
            except OSError as error:
                report.errors.append(ScanError(str(path), str(error)))
    connection.commit()
    return directories, files


def _fingerprint(
    directories: set[str], files: dict[str, tuple[str, int]]
) -> TreeFingerprint:
    """Produce the same recursive digest used by duplicate-tree planning."""
    children: dict[Path, list[tuple[str, str, str]]] = {
        Path(directory): [] for directory in directories
    }
    counts: dict[Path, tuple[int, int]] = {}
    for relative, (sha256, _size) in files.items():
        path = Path(relative)
        children.setdefault(path.parent, []).append((path.name, "file", sha256))

    digests: dict[Path, str] = {}
    for directory in sorted(children, key=lambda path: len(path.parts), reverse=True):
        digest = hashlib.sha256()
        file_count = 0
        logical_bytes = 0
        for name, kind, value in sorted(children[directory]):
            digest.update(f"{kind}\0{name}\0{value}\n".encode())
            if kind == "file":
                file_count += 1
                logical_bytes += files[str(directory / name)][1]
            else:
                child_count, child_bytes = counts[directory / name]
                file_count += child_count
                logical_bytes += child_bytes
        digests[directory] = digest.hexdigest()
        counts[directory] = (file_count, logical_bytes)
        if directory != Path("."):
            children.setdefault(directory.parent, []).append(
                (directory.name, "directory", digests[directory])
            )

    file_count, logical_bytes = counts[Path(".")]
    return TreeFingerprint(digests[Path(".")], file_count, logical_bytes)


def _metadata_fingerprint(root: Path) -> tuple[TreeFingerprint | None, list[ScanError]]:
    errors: list[ScanError] = []
    if not root.is_dir():
        return None, [ScanError(str(root), "path must be a directory")]
    directories = {"."}
    files: dict[str, tuple[str, int]] = {}

    def walk_error(error: OSError) -> None:
        errors.append(ScanError(str(error.filename or root), str(error)))

    for directory, child_directories, child_files in os.walk(
        root, followlinks=False, onerror=walk_error
    ):
        current = Path(directory)
        child_directories[:] = [
            child for child in child_directories if not (current / child).is_symlink()
        ]
        for child in child_directories:
            directories.add(str((current / child).relative_to(root)))
        for child in child_files:
            path = current / child
            try:
                if path.is_symlink():
                    continue
                stat_result = path.stat(follow_symlinks=False)
                identity = hashlib.sha256(
                    f"{stat_result.st_size}\0{stat_result.st_mtime_ns}\0"
                    f"{stat_result.st_ctime_ns}".encode()
                ).hexdigest()
                files[str(path.relative_to(root))] = (identity, stat_result.st_size)
            except OSError as error:
                errors.append(ScanError(str(path), str(error)))
    return _fingerprint(directories, files), errors


def fingerprint_metadata_trees(
    paths: list[Path], max_threads: int | None = None
) -> tuple[dict[Path, TreeFingerprint], list[ScanError]]:
    """Fingerprint independent trees concurrently from names, sizes, and timestamps."""
    if max_threads is not None and max_threads < 1:
        raise ValueError("max_threads must be at least 1")
    roots = list(dict.fromkeys(item.absolute() for item in paths))
    fingerprints: dict[Path, TreeFingerprint] = {}
    errors: list[ScanError] = []
    if not roots:
        return fingerprints, errors
    workers = min(max_threads or 8, len(roots), os.cpu_count() or 1)
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="muse-scan") as executor:
        for root, (fingerprint, scan_errors) in zip(
            roots, executor.map(_metadata_fingerprint, roots), strict=True
        ):
            errors.extend(scan_errors)
            if fingerprint is not None:
                fingerprints[root] = fingerprint
    return fingerprints, errors


def fingerprint_trees(
    paths: list[Path], database: Path
) -> tuple[dict[Path, TreeFingerprint], list[ScanError]]:
    """Fingerprint each distinct tree once, sharing one hash-cache connection."""
    database = database.absolute()
    report = TreeDiffReport("", "", str(database))
    fingerprints: dict[Path, TreeFingerprint] = {}
    database.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(database) as connection:
        _initialize_database(connection)
        for path in dict.fromkeys(item.absolute() for item in paths):
            if not path.is_dir():
                report.errors.append(ScanError(str(path), "path must be a directory"))
                continue
            directories, files = _snapshot(path, database.parent, connection, report)
            fingerprints[path] = _fingerprint(directories, files)
    return fingerprints, report.errors


def compare_trees(left: Path, right: Path, database: Path) -> TreeDiffReport:
    """Compare two trees by relative path, entry type, and exact file content."""
    started = perf_counter()
    left, right, database = left.absolute(), right.absolute(), database.absolute()
    report = TreeDiffReport(str(left), str(right), str(database))
    if not left.is_dir() or not right.is_dir():
        report.errors.append(ScanError(f"{left} / {right}", "both paths must be directories"))
        return report
    database.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(database) as connection:
        _initialize_database(connection)
        left_directories, left_files = _snapshot(left, database.parent, connection, report)
        right_directories, right_files = _snapshot(right, database.parent, connection, report)
    report.left_files, report.right_files = len(left_files), len(right_files)

    left_entries = {path: "directory" for path in left_directories}
    left_entries.update({path: "file" for path in left_files})
    right_entries = {path: "directory" for path in right_directories}
    right_entries.update({path: "file" for path in right_files})
    for path in sorted(left_entries.keys() | right_entries.keys()):
        left_kind, right_kind = left_entries.get(path), right_entries.get(path)
        if left_kind is None:
            report.differences.append(TreeDifference("only-right", path, right=right_kind))
        elif right_kind is None:
            report.differences.append(TreeDifference("only-left", path, left=left_kind))
        elif left_kind != right_kind:
            report.differences.append(TreeDifference("type-conflict", path, left_kind, right_kind))
        elif left_kind == "file" and left_files[path][0] != right_files[path][0]:
            report.differences.append(TreeDifference("content-mismatch", path))
    report.elapsed_seconds = perf_counter() - started
    return report
