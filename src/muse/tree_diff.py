"""Read-only structural comparison of two directory trees."""

from __future__ import annotations

import hashlib
import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from time import perf_counter
from typing import Any

from muse import cache
from muse.filesystem import WalkError, walk
from muse.hashing import FileIdentity, sha256_file
from muse.repository import ScanError
from muse.trees import TreeFingerprint, fingerprints


@dataclass(frozen=True)
class TreeDifference:
    kind: str
    path: str
    left: str | None = None
    right: str | None = None

    def to_dict(self) -> dict[str, str | None]:
        return {"kind": self.kind, "path": self.path, "left": self.left, "right": self.right}


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


def _snapshot(
    root: Path,
    database_parent: Path,
    connection: sqlite3.Connection,
    report: TreeDiffReport,
) -> tuple[set[str], dict[str, tuple[str, int]]]:
    directories = {"."}
    files: dict[str, tuple[str, int]] = {}
    pending_writes = 0
    for item in walk(root, exclude=lambda path: path == database_parent, follow_root=True):
        if isinstance(item, WalkError):
            report.errors.append(ScanError(str(item.path), item.message))
            continue
        path = item.path
        if item.kind == "directory":
            directories.add(str(path.relative_to(root)))
        elif item.kind == "file":
            try:
                identity = FileIdentity.from_path(path)
                sha256 = cache.lookup(connection, path, identity)
                if sha256 is None:
                    sha256 = sha256_file(path, expected=identity)
                    cache.insert(connection, path, identity, sha256)
                    report.hashed_files += 1
                    pending_writes += 1
                    if pending_writes >= 100:
                        connection.commit()
                        pending_writes = 0
                else:
                    if FileIdentity.from_path(path) != identity:
                        raise OSError("file changed after inventory")
                    report.cached_files += 1
                files[str(path.relative_to(root))] = (sha256, identity.size)
            except OSError as error:
                report.errors.append(ScanError(str(path), str(error)))
    connection.commit()
    return directories, files


def _fingerprint(directories: set[str], files: dict[str, tuple[str, int]]) -> TreeFingerprint:
    """Produce the same recursive digest used by duplicate-tree planning."""
    return fingerprints(directories, files)["."]


def _metadata_fingerprint(root: Path) -> tuple[TreeFingerprint | None, list[ScanError]]:
    errors: list[ScanError] = []
    if not root.is_dir():
        return None, [ScanError(str(root), "path must be a directory")]
    directories = {"."}
    files: dict[str, tuple[str, int]] = {}

    for item in walk(root, follow_root=True):
        if isinstance(item, WalkError):
            errors.append(ScanError(str(item.path), item.message))
            continue
        path = item.path
        if item.kind == "directory":
            directories.add(str(path.relative_to(root)))
        elif item.kind == "file":
            try:
                stat_result = item.stat
                assert stat_result is not None
                identity = hashlib.sha256(
                    f"{stat_result.st_size}\0{stat_result.st_mtime_ns}\0"
                    f"{stat_result.st_ctime_ns}".encode()
                ).hexdigest()
                files[str(path.relative_to(root))] = (identity, stat_result.st_size)
            except OSError as error:
                errors.append(ScanError(str(path), str(error)))
    return (_fingerprint(directories, files) if not errors else None), errors


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
    workers = min(max_threads or 16, len(roots), os.cpu_count() or 1)
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
        cache.initialize_database(connection)
        for path in dict.fromkeys(item.absolute() for item in paths):
            if not path.is_dir():
                report.errors.append(ScanError(str(path), "path must be a directory"))
                continue
            before_errors = len(report.errors)
            directories, files = _snapshot(path, database.parent, connection, report)
            if len(report.errors) == before_errors:
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
        cache.initialize_database(connection)
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
