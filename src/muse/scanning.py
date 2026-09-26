"""Compare a prospective external source with all vault content areas."""

from __future__ import annotations

import os
import shutil
import sqlite3
import tempfile
from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock
from typing import Any

from muse import cache
from muse.config import CONTENT_AREAS
from muse.filesystem import WalkError, walk
from muse.hashing import FileIdentity, sha256_file
from muse.repository import AUDIO_EXTENSIONS, ScanError


@dataclass(frozen=True)
class ScannedFile:
    path: Path
    relative: str
    size: int
    identity: FileIdentity | None = None

    @property
    def extension(self) -> str:
        return self.path.suffix.lower() or "[no extension]"

    @property
    def audio(self) -> bool:
        return self.extension in AUDIO_EXTENSIONS


@dataclass(frozen=True)
class ScanProgress:
    """Progress while inventorying the external target."""

    completed_files: int = 0
    completed_bytes: int = 0
    complete: bool = False


ScanProgressCallback = Callable[[ScanProgress], None]


@dataclass(frozen=True)
class PullResult:
    """Directories copied from an external scan into backlog."""

    destination: Path
    source_directories: tuple[Path, ...]


@dataclass(frozen=True)
class PullProgress:
    """Progress while copying selected external directories into backlog."""

    total_files: int
    total_bytes: int
    completed_files: int = 0
    completed_bytes: int = 0
    current: str | None = None
    complete: bool = False


PullProgressCallback = Callable[[PullProgress], None]


@dataclass
class ScanComparison:
    target: str
    mode: str
    vault_areas: tuple[str, ...]
    inventory_files: int = 0
    target_files: int = 0
    target_bytes: int = 0
    target_audio_files: int = 0
    present_files: int = 0
    present_bytes: int = 0
    present_audio_files: int = 0
    new_files: list[ScannedFile] = field(default_factory=list)
    target_extensions: Counter[str] = field(default_factory=Counter)
    target_extension_bytes: Counter[str] = field(default_factory=Counter)
    extensions: Counter[str] = field(default_factory=Counter)
    top_level: Counter[str] = field(default_factory=Counter)
    top_level_bytes: Counter[str] = field(default_factory=Counter)
    hashed_files: int = 0
    hashed_bytes: int = 0
    cached_files: int = 0
    cached_bytes: int = 0
    bytes_read: int = 0
    hash_worker_threads: int = 0
    errors: list[ScanError] = field(default_factory=list)

    @property
    def new_bytes(self) -> int:
        return sum(item.size for item in self.new_files)

    @property
    def new_audio_files(self) -> int:
        return sum(item.audio for item in self.new_files)

    @property
    def coverage(self) -> float:
        return self.present_files / self.target_files if self.target_files else 1.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "target": self.target,
            "mode": self.mode,
            "vault_areas": list(self.vault_areas),
            "target_summary": {
                "path": self.target,
                "files": self.target_files,
                "audio_files": self.target_audio_files,
                "logical_bytes": self.target_bytes,
                "extensions": dict(sorted(self.target_extensions.items())),
                "extension_logical_bytes": dict(sorted(self.target_extension_bytes.items())),
            },
            "present_in_vault": {
                "files": self.present_files,
                "audio_files": self.present_audio_files,
                "logical_bytes": self.present_bytes,
                "file_coverage": self.coverage,
            },
            "not_found_in_vault": {
                "files": len(self.new_files),
                "audio_files": self.new_audio_files,
                "logical_bytes": self.new_bytes,
                "extensions": dict(sorted(self.extensions.items())),
                "top_level": [
                    {
                        "path": name,
                        "files": count,
                        "logical_bytes": self.top_level_bytes[name],
                    }
                    for name, count in self.top_level.most_common()
                ],
                "items": [
                    {
                        "path": item.relative,
                        "size": item.size,
                        "extension": item.extension,
                        "audio": item.audio,
                    }
                    for item in self.new_files
                ],
            },
            "hashing": {
                "files": self.hashed_files,
                "logical_bytes": self.hashed_bytes,
                "cached_files": self.cached_files,
                "cached_bytes": self.cached_bytes,
                "bytes_read": self.bytes_read,
                "worker_threads": self.hash_worker_threads,
            },
            "errors": [error.to_dict() for error in self.errors],
        }


def _inventory(
    target: Path,
    label_root: Path,
    errors: list[ScanError],
    progress: ScanProgressCallback | None = None,
) -> list[ScannedFile]:
    files: list[ScannedFile] = []
    scanned_bytes = 0
    for item in walk(target, sort=True):
        if isinstance(item, WalkError):
            errors.append(ScanError(str(item.path), item.message))
            continue
        if item.path == target and item.kind == "symlink":
            errors.append(ScanError(str(target), "target must not be a symlink"))
        elif item.path == target and item.kind == "other":
            errors.append(ScanError(str(target), "path is not a regular file or directory"))
        elif item.kind == "file":
            try:
                identity = FileIdentity.from_path(item.path)
                relative = (
                    target.name if item.path == target else str(item.path.relative_to(label_root))
                )
                scanned = ScannedFile(item.path, relative, identity.size, identity)
                files.append(scanned)
                scanned_bytes += scanned.size
                if progress:
                    progress(ScanProgress(len(files), scanned_bytes))
            except OSError as error:
                errors.append(ScanError(str(item.path), str(error)))
    files.sort(key=lambda item: item.relative.casefold())
    return files


def _pull_totals(sources: list[Path]) -> tuple[int, int]:
    """Count regular files to provide determinate copy progress."""
    files = 0
    logical_bytes = 0
    for source in sources:
        for item in walk(source):
            if isinstance(item, WalkError):
                raise OSError(f"{item.path}: {item.message}")
            if item.kind == "file":
                assert item.stat is not None
                files += 1
                logical_bytes += item.stat.st_size
    return files, logical_bytes


def pull_new_directories(
    root: Path,
    target: Path,
    new_files: list[ScannedFile],
    destination: Path,
    progress: PullProgressCallback | None = None,
) -> PullResult:
    """Copy whole directories containing not-found audio into a new backlog path.

    Copying a containing directory, rather than selected audio files, preserves
    cue sheets, artwork, metadata, and other release-adjacent material. The
    destination is assembled beside its final path and renamed into place so a
    failed copy is never mistaken for a completed pull.
    """
    root = root.absolute()
    target = target.absolute()
    backlog = root / "backlog"
    destination = destination.absolute()
    if not target.is_dir():
        raise ValueError("--pull requires a directory target; scan the containing directory")
    if not backlog.is_dir():
        raise ValueError("backlog directory does not exist")
    if destination == backlog or not destination.is_relative_to(backlog):
        raise ValueError("pull destination must be a new directory below backlog/")
    if destination.exists() or destination.is_symlink():
        raise ValueError(f"pull destination already exists: {destination}")
    if destination.resolve(strict=False).is_relative_to(backlog.resolve()) is False:
        raise ValueError("pull destination escapes backlog through a symlink")

    containing = {item.path.parent for item in new_files}
    for directory in containing:
        if directory != target and not directory.is_relative_to(target):
            raise ValueError(f"not-found file is outside the scan target: {directory}")
    ordered = sorted(containing, key=lambda path: (len(path.parts), str(path).casefold()))
    sources: list[Path] = []
    for directory in ordered:
        if not any(directory == parent or directory.is_relative_to(parent) for parent in sources):
            sources.append(directory)
    if not sources:
        raise ValueError("there are no not-found audio directories to pull")

    total_files, total_bytes = _pull_totals(sources)
    completed_files = 0
    completed_bytes = 0
    if progress:
        progress(PullProgress(total_files, total_bytes))

    def copy_file(source: str, copied_destination: str) -> str:
        nonlocal completed_files, completed_bytes
        size = os.stat(source, follow_symlinks=False).st_size
        result = shutil.copy2(source, copied_destination)
        completed_files += 1
        completed_bytes += size
        if progress:
            progress(
                PullProgress(
                    total_files,
                    total_bytes,
                    completed_files,
                    completed_bytes,
                    source,
                )
            )
        return result

    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{destination.name}.pull-", dir=destination.parent))
    try:
        for source in sources:
            if source == target:
                shutil.copytree(
                    source,
                    staging,
                    symlinks=True,
                    copy_function=copy_file,
                    dirs_exist_ok=True,
                )
            else:
                shutil.copytree(
                    source,
                    staging / source.relative_to(target),
                    symlinks=True,
                    copy_function=copy_file,
                )
        staging.replace(destination)
    except (OSError, shutil.Error):
        shutil.rmtree(staging, ignore_errors=True)
        raise
    if progress:
        progress(
            PullProgress(
                total_files,
                total_bytes,
                completed_files,
                completed_bytes,
                complete=True,
            )
        )
    return PullResult(destination, tuple(sources))


def _hash_files(
    files: list[ScannedFile],
    report: ScanComparison,
    max_threads: int | None,
    connection: sqlite3.Connection,
    vault_paths: set[Path],
) -> dict[Path, str]:
    hashes: dict[Path, str] = {}
    pending = []
    for item in files:
        try:
            if item.identity is None or FileIdentity.from_path(item.path) != item.identity:
                raise OSError("file changed after inventory")
            cached = (
                cache.lookup(connection, item.path, item.identity)
                if item.path in vault_paths
                else None
            )
            if cached is None:
                pending.append(item)
            else:
                if FileIdentity.from_path(item.path) != item.identity:
                    raise OSError("file changed after inventory")
                hashes[item.path] = cached
                report.cached_files += 1
                report.cached_bytes += item.size
        except OSError as error:
            report.errors.append(ScanError(str(item.path), str(error)))
    workers = min(max_threads or 16, len(pending), os.cpu_count() or 1)
    report.hash_worker_threads = workers
    if not workers:
        return hashes
    progress_lock = Lock()

    def digest(item: ScannedFile) -> str:
        def count_bytes(count: int) -> None:
            with progress_lock:
                report.bytes_read += count

        return sha256_file(item.path, expected=item.identity, on_bytes_read=count_bytes)

    pending_writes = 0
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="muse-scan") as executor:
        futures = {executor.submit(digest, item): item for item in pending}
        for future in as_completed(futures):
            item = futures[future]
            try:
                sha256 = future.result()
            except OSError as error:
                report.errors.append(ScanError(str(item.path), str(error)))
                continue
            hashes[item.path] = sha256
            report.hashed_files += 1
            report.hashed_bytes += item.size
            if item.path in vault_paths and item.identity is not None:
                cache.insert(connection, item.path, item.identity, sha256)
                pending_writes += 1
                if pending_writes >= 100:
                    connection.commit()
                    pending_writes = 0
    connection.commit()
    return hashes


def compare_with_vault(
    root: Path,
    target: Path,
    *,
    thorough: bool = False,
    max_threads: int | None = None,
    progress: ScanProgressCallback | None = None,
) -> ScanComparison:
    """Find target files which have no likely or exact copy in the vault.

    The cheap comparison uses case-insensitive basename plus file size. Thorough
    mode uses SHA-256 and regards names and locations as irrelevant.
    """
    if max_threads is not None and max_threads < 1:
        raise ValueError("max_threads must be at least 1")
    root = root.absolute()
    target = target.absolute()
    area_paths = tuple(root / area for area in CONTENT_AREAS if (root / area).exists())
    report = ScanComparison(
        str(target),
        "thorough" if thorough else "quick",
        tuple(area.name for area in area_paths),
    )
    try:
        if not root.exists():
            report.errors.append(ScanError(str(root), "vault root does not exist"))
            return report
        if not root.is_dir():
            report.errors.append(ScanError(str(root), "vault root is not a directory"))
            return report
    except OSError as error:
        report.errors.append(ScanError(str(root), str(error)))
        return report

    for area in area_paths:
        if target == area or target.is_relative_to(area) or area.is_relative_to(target):
            report.errors.append(
                ScanError(str(target), f"target overlaps vault content area {area}")
            )
            return report

    if progress:
        progress(ScanProgress())
    target_inventory = _inventory(
        target,
        target if target.is_dir() else target.parent,
        report.errors,
        progress,
    )
    if progress:
        progress(
            ScanProgress(
                len(target_inventory),
                sum(item.size for item in target_inventory),
                complete=True,
            )
        )
    vault_files: list[ScannedFile] = []
    for area in area_paths:
        vault_files.extend(_inventory(area, root, report.errors))

    report.inventory_files = len(target_inventory)

    # Scan is an audio-vault assessment. Other files are encountered while
    # walking the target, but are not reported or compared.
    target_files = [item for item in target_inventory if item.audio]
    report.target_files = len(target_files)
    report.target_bytes = sum(item.size for item in target_files)
    report.target_audio_files = report.target_files
    for item in target_files:
        report.target_extensions[item.extension] += 1
        report.target_extension_bytes[item.extension] += item.size

    if thorough:
        target_sizes = {item.size for item in target_files}
        vault_candidates = [item for item in vault_files if item.size in target_sizes]
        vault_sizes = {item.size for item in vault_candidates}
        target_candidates = [item for item in target_files if item.size in vault_sizes]
        database = root / ".muse" / "muse.db"
        database.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(database) as connection:
            cache.initialize_database(connection)
            hashes = _hash_files(
                [*target_candidates, *vault_candidates],
                report,
                max_threads,
                connection,
                {item.path for item in vault_candidates},
            )
        vault_hashes = {hashes[item.path] for item in vault_candidates if item.path in hashes}
        present = {
            item.path
            for item in target_candidates
            if item.path in hashes and hashes[item.path] in vault_hashes
        }
    else:
        vault_keys = {(item.path.name.casefold(), item.size) for item in vault_files}
        present = {
            item.path
            for item in target_files
            if (item.path.name.casefold(), item.size) in vault_keys
        }

    report.present_files = sum(item.path in present for item in target_files)
    report.present_bytes = sum(item.size for item in target_files if item.path in present)
    report.present_audio_files = sum(item.audio for item in target_files if item.path in present)
    report.new_files = [item for item in target_files if item.path not in present]
    for item in report.new_files:
        report.extensions[item.extension] += 1
        top = Path(item.relative).parts[0]
        report.top_level[top] += 1
        report.top_level_bytes[top] += item.size
    return report
