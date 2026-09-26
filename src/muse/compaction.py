"""Conservative exact-tree compaction planning and application."""

from __future__ import annotations

import json
import os
import secrets
import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from muse import cache
from muse.duplicates import find_duplicates
from muse.mutation import (
    archive_record,
    atomic_json,
    checked_path,
    mutation_lock,
    occupied,
    rename_exact,
)
from muse.tree_diff import fingerprint_metadata_trees, fingerprint_trees

PLAN_NAME = "compact-plan.json"
MIN_COMPACT_TREE_BYTES = 3
REMOVABLE_AREAS = frozenset({"backlog", "stopgap", "slag"})
RETENTION_PRIORITY = {
    "master": 0,
    "backlog": 1,
    "stopgap": 2,
    "incoming": 3,
    "slag": 4,
    "trash": 5,
}


@dataclass(frozen=True)
class CompactOperation:
    retain: str
    remove: str
    files: int
    logical_bytes: int
    tree_sha256: str
    retain_metadata_sha256: str | None = None
    remove_metadata_sha256: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class CompactProgress:
    phase: str
    completed_operations: int
    total_operations: int
    path: str | None = None
    worker_threads: int = 0


def verification_workers(distinct_paths: int, max_threads: int | None) -> int:
    """Return the number of workers used to reverify a pending plan."""
    return min(max_threads or 16, distinct_paths, os.cpu_count() or 1) if distinct_paths else 0


ProgressCallback = Callable[[CompactProgress], None]


def plan_path(root: Path) -> Path:
    return root / ".muse" / PLAN_NAME


def _area(path: str) -> str:
    return Path(path).parts[0]


def make_plan(
    root: Path,
    target: Path | None = None,
    *,
    preferences: Sequence[Path] = (),
    max_threads: int | None = None,
) -> tuple[list[CompactOperation], list[dict[str, str]]]:
    """Create the sole pending plan from maximal exact duplicate trees.

    Preferences are ordered library-relative subtrees. They rank copies within
    the same managed-area retention tier; the fixed area policy still wins.
    """
    target = root if target is None else target
    report = find_duplicates(
        target, root / ".muse" / "muse.db", trees=True, max_threads=max_threads
    )
    if report.errors:
        return [], [error.to_dict() for error in report.errors]

    operations = []
    prefix = target.relative_to(root)
    for group in report.tree_groups:
        # Empty and tiny trees are commonly incidental marker files or empty
        # directories, not meaningful copies worth destructive cleanup.
        if group.logical_bytes < MIN_COMPACT_TREE_BYTES:
            continue
        paths = [str(prefix / path) for path in group.paths]

        def retention_key(path: str) -> tuple[int, int, str]:
            candidate = Path(path)
            preference = next(
                (
                    index
                    for index, preferred in enumerate(preferences)
                    if candidate == preferred or candidate.is_relative_to(preferred)
                ),
                len(preferences),
            )
            return RETENTION_PRIORITY[_area(path)], preference, path

        candidates = sorted(paths, key=retention_key)
        retain = candidates[0]
        for remove in candidates[1:]:
            if _area(remove) not in REMOVABLE_AREAS:
                continue
            operations.append(
                CompactOperation(retain, remove, group.files, group.logical_bytes, group.sha256)
            )
    operations.sort(key=lambda item: (-item.logical_bytes, item.remove))

    # Persist a cheap pre-mutation snapshot. Applying the plan can then prove
    # that names, sizes, and timestamps are unchanged without consulting or
    # recomputing every content hash.
    paths = [root / path for item in operations for path in (item.retain, item.remove)]
    fingerprints, fingerprint_errors = fingerprint_metadata_trees(paths, max_threads)
    if fingerprint_errors:
        return [], [error.to_dict() for error in fingerprint_errors]
    operations = [
        replace(
            item,
            retain_metadata_sha256=fingerprints[(root / item.retain).absolute()].sha256,
            remove_metadata_sha256=fingerprints[(root / item.remove).absolute()].sha256,
        )
        for item in operations
    ]
    return operations, []


def save_plan(root: Path, operations: list[CompactOperation]) -> Path:
    with mutation_lock(root):
        return _save_plan(root, operations)


def _save_plan(root: Path, operations: list[CompactOperation]) -> Path:
    path = plan_path(root)
    checked_path(root, path.relative_to(root))
    if path.exists():
        existing = json.loads(path.read_text())
        if existing.get("state") == "moving":
            raise ValueError("a compaction is in progress; resume it before creating a new plan")
    value = {
        "schema_version": 2,
        "created_at": datetime.now(UTC).isoformat(),
        "root": str(root),
        "state": "ready",
        "receipt": None,
        "operations": [operation.to_dict() for operation in operations],
    }
    atomic_json(path, value)
    return path


def load_plan(root: Path) -> list[CompactOperation]:
    value = json.loads(plan_path(root).read_text())
    if value.get("root") != str(root):
        raise ValueError("plan belongs to a different library root")
    if value.get("schema_version", 1) not in {1, 2}:
        raise ValueError("unsupported compaction plan version")
    return [CompactOperation(**operation) for operation in value["operations"]]


def _create_trash_receipt(root: Path, operations: list[CompactOperation]) -> Path:
    trash = root / "trash"
    if trash.is_symlink():
        raise ValueError("trash directory must not be a symlink")
    trash.mkdir(parents=True, exist_ok=True)
    if not trash.is_dir() or root.resolve() not in trash.resolve().parents:
        raise ValueError("unsafe trash directory")
    now = datetime.now().astimezone()
    date_directory = trash / f"{now:%Y-%m-%d}"
    checked_path(root, date_directory.relative_to(root), area="trash")
    date_directory.mkdir(exist_ok=True)
    for _attempt in range(100):
        receipt = date_directory / f"{now:%H%M%S}-compact-{secrets.token_hex(2)}"
        try:
            receipt.mkdir()
        except FileExistsError:
            continue
        value = {
            "created_at": now.isoformat(),
            "operation": "compact",
            "root": str(root),
            "state": "moving",
            "entries": [operation.to_dict() for operation in operations],
        }
        atomic_json(receipt / "receipt.json", value)
        return receipt
    raise ValueError("could not allocate a unique trash receipt")


def _complete_trash_receipt(receipt: Path) -> None:
    path = receipt / "receipt.json"
    value = json.loads(path.read_text())
    value["state"] = "complete"
    atomic_json(path, value)


def apply_plan(
    root: Path,
    operations: list[CompactOperation],
    progress: ProgressCallback | None = None,
    *,
    max_threads: int | None = None,
) -> Path | None:
    """Reverify planned duplicate trees, then move them into a trash receipt."""
    with mutation_lock(root):
        return _apply_plan(root, operations, progress, max_threads=max_threads)


def _apply_plan(
    root: Path,
    operations: list[CompactOperation],
    progress: ProgressCallback | None = None,
    *,
    max_threads: int | None = None,
) -> Path | None:
    total = len(operations)
    plan_file = plan_path(root)
    checked_path(root, plan_file.relative_to(root))
    state = json.loads(plan_file.read_text()) if plan_file.exists() else None
    if state is not None:
        if state.get("root") != str(root) or state.get("operations") != [
            item.to_dict() for item in operations
        ]:
            raise ValueError("compaction plan has changed; reload the current plan")
        if state.get("schema_version", 1) not in {1, 2}:
            raise ValueError("unsupported compaction plan version")
        if state.get("state", "ready") not in {"ready", "moving"}:
            raise ValueError("unsupported compaction plan state")
    receipt: Path | None = None
    if state and state.get("state") == "moving":
        if state.get("schema_version") != 2 or not state.get("receipt"):
            raise ValueError(
                "legacy interrupted compaction has no receipt; inspect the trash "
                "and restore the source before retrying"
            )
        receipt = checked_path(root, state["receipt"], area="trash")
        if not (receipt / "receipt.json").is_file():
            raise ValueError("compaction receipt is missing; inspect the trash before retrying")
        receipt_state = json.loads((receipt / "receipt.json").read_text())
        if (
            receipt_state.get("root") != str(root)
            or receipt_state.get("operation") != "compact"
            or receipt_state.get("entries") != state["operations"]
        ):
            raise ValueError("compaction receipt does not match the current plan")
    prepared: list[tuple[CompactOperation, Path, Path, Path | None]] = []
    for operation in operations:
        remove_area = _area(operation.remove)
        if remove_area not in REMOVABLE_AREAS:
            raise ValueError(f"unsafe removal path in plan: {operation.remove}")
        try:
            retain = checked_path(root, operation.retain)
            remove = checked_path(root, operation.remove, area=remove_area)
            moved = (
                checked_path(root, (receipt / operation.remove).relative_to(root))
                if receipt
                else None
            )
        except ValueError as error:
            raise ValueError(f"unsafe removal path in plan: {operation.remove}") from error
        if remove == root / remove_area:
            raise ValueError(f"unsafe removal path in plan: {operation.remove}")
        if occupied(remove) and moved is not None and occupied(moved):
            raise ValueError(f"both source and trash destination exist: {operation.remove}")
        if not occupied(remove) and moved is None and state and state.get("schema_version", 1) == 1:
            raise ValueError(
                "legacy compaction source is missing; inspect the trash before retrying"
            )
        if not occupied(remove) and moved is not None and not occupied(moved):
            raise ValueError(f"neither source nor trash destination exists: {operation.remove}")
        prepared.append((operation, retain, remove, moved))

    # A retained tree can back hundreds or thousands of removals. Fingerprint
    # every distinct path only once. New plans use the cheap metadata snapshot;
    # old pending plans fall back to content fingerprints via the hash cache.
    paths = [
        path
        for _operation, retain, remove, moved in prepared
        for path in (retain, moved if moved is not None and occupied(moved) else remove)
    ]
    distinct_paths = len(dict.fromkeys(paths))
    worker_threads = verification_workers(distinct_paths, max_threads)
    if progress is not None:
        progress(CompactProgress("verify", 0, total, worker_threads=worker_threads))
    metadata_plan = all(
        operation.retain_metadata_sha256 is not None
        and operation.remove_metadata_sha256 is not None
        for operation, _retain, _remove, _moved in prepared
    )
    if metadata_plan:
        fingerprints, errors = fingerprint_metadata_trees(paths, max_threads)
    else:
        fingerprints, errors = fingerprint_trees(paths, root / ".muse" / "muse.db")
    if errors:
        raise ValueError("planned trees could not be reverified")
    for completed, (operation, retain, remove, moved) in enumerate(prepared, start=1):
        expected = (
            operation.retain_metadata_sha256 if metadata_plan else operation.tree_sha256,
            operation.files,
            operation.logical_bytes,
        )
        retain_fingerprint = fingerprints.get(retain)
        remove_fingerprint = fingerprints.get(
            moved if moved is not None and occupied(moved) else remove
        )
        actual_retain = (
            None
            if retain_fingerprint is None
            else (
                retain_fingerprint.sha256,
                retain_fingerprint.files,
                retain_fingerprint.logical_bytes,
            )
        )
        actual_remove = (
            None
            if remove_fingerprint is None
            else (
                remove_fingerprint.sha256,
                remove_fingerprint.files,
                remove_fingerprint.logical_bytes,
            )
        )
        expected_remove = (
            (
                operation.remove_metadata_sha256,
                operation.files,
                operation.logical_bytes,
            )
            if metadata_plan
            else expected
        )
        if actual_retain != expected or actual_remove != expected_remove:
            raise ValueError(f"planned trees no longer match: {operation.remove}")
        if progress is not None:
            progress(CompactProgress("verify", completed, total, operation.remove, worker_threads))
    if receipt is None and operations:
        receipt = _create_trash_receipt(root, operations)
        if state is None:
            state = {
                "schema_version": 2,
                "created_at": datetime.now(UTC).isoformat(),
                "root": str(root),
                "operations": [item.to_dict() for item in operations],
            }
        state.update(state="moving", receipt=receipt.relative_to(root).as_posix(), schema_version=2)
        atomic_json(plan_file, state)
    if progress is not None:
        progress(CompactProgress("trash", 0, total))
    for completed, operation in enumerate(operations, start=1):
        source = root / operation.remove
        assert receipt is not None
        destination = receipt / operation.remove
        if not occupied(destination):
            destination.parent.mkdir(parents=True, exist_ok=True)
            try:
                rename_exact(source, destination)
            except OSError as error:
                raise ValueError(
                    f"could not move {operation.remove} to trash receipt {receipt}: {error}"
                ) from error
        database = root / ".muse" / "muse.db"
        if database.exists():
            with sqlite3.connect(database) as connection:
                cache.initialize_database(connection)
                cache.relocate(connection, source, destination)
        if progress is not None:
            progress(CompactProgress("trash", completed, total, operation.remove))
    if receipt is not None:
        _complete_trash_receipt(receipt)
    audit = root / ".muse" / "audit"
    checked_path(root, audit.relative_to(root))
    if plan_file.exists():
        archive_record(plan_file, audit, f"compact-{datetime.now(UTC):%Y%m%dT%H%M%SZ}")
    return receipt
