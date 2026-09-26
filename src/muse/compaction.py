"""Conservative exact-tree compaction planning and application."""

from __future__ import annotations

import json
import secrets
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from muse.duplicates import find_duplicates
from muse.tree_diff import compare_trees

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

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class CompactProgress:
    phase: str
    completed_operations: int
    total_operations: int
    path: str | None = None


ProgressCallback = Callable[[CompactProgress], None]


def plan_path(root: Path) -> Path:
    return root / ".muse" / PLAN_NAME


def _area(path: str) -> str:
    return Path(path).parts[0]


def make_plan(
    root: Path, target: Path | None = None
) -> tuple[list[CompactOperation], list[dict[str, str]]]:
    """Create the sole pending plan from maximal exact duplicate trees."""
    target = root if target is None else target
    report = find_duplicates(target, root / ".muse" / "muse.db", trees=True)
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
        candidates = sorted(paths, key=lambda path: (RETENTION_PRIORITY[_area(path)], path))
        retain = candidates[0]
        for remove in candidates[1:]:
            if _area(remove) not in REMOVABLE_AREAS:
                continue
            operations.append(
                CompactOperation(retain, remove, group.files, group.logical_bytes, group.sha256)
            )
    operations.sort(key=lambda item: (-item.logical_bytes, item.remove))
    return operations, []


def save_plan(root: Path, operations: list[CompactOperation]) -> Path:
    path = plan_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    value = {
        "created_at": datetime.now(UTC).isoformat(),
        "root": str(root),
        "operations": [operation.to_dict() for operation in operations],
    }
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    return path


def load_plan(root: Path) -> list[CompactOperation]:
    value = json.loads(plan_path(root).read_text())
    if value.get("root") != str(root):
        raise ValueError("plan belongs to a different library root")
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
        (receipt / "receipt.json").write_text(
            json.dumps(value, indent=2, sort_keys=True) + "\n"
        )
        return receipt
    raise ValueError("could not allocate a unique trash receipt")


def _complete_trash_receipt(receipt: Path) -> None:
    path = receipt / "receipt.json"
    value = json.loads(path.read_text())
    value["state"] = "complete"
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def apply_plan(
    root: Path,
    operations: list[CompactOperation],
    progress: ProgressCallback | None = None,
) -> Path | None:
    """Reverify planned duplicate trees, then move them into a trash receipt."""
    total = len(operations)
    if progress is not None:
        progress(CompactProgress("verify", 0, total))
    resolved_root = root.resolve()
    for completed, operation in enumerate(operations, start=1):
        retain_path = Path(operation.retain)
        remove_path = Path(operation.remove)
        remove_area = _area(operation.remove)
        retain = (root / retain_path).resolve()
        remove = (root / remove_path).resolve()
        unsafe = (
            retain_path.is_absolute()
            or remove_path.is_absolute()
            or ".." in retain_path.parts
            or ".." in remove_path.parts
            or resolved_root not in retain.parents
            or remove_area not in REMOVABLE_AREAS
            or not remove.is_relative_to(resolved_root / remove_area)
        )
        if unsafe:
            raise ValueError(f"unsafe removal path in plan: {operation.remove}")
        comparison = compare_trees(retain, remove, root / ".muse" / "muse.db")
        if comparison.errors or comparison.differences:
            raise ValueError(f"planned trees no longer match: {operation.remove}")
        if progress is not None:
            progress(CompactProgress("verify", completed, total, operation.remove))
    receipt = _create_trash_receipt(root, operations) if operations else None
    if progress is not None:
        progress(CompactProgress("trash", 0, total))
    for completed, operation in enumerate(operations, start=1):
        source = root / operation.remove
        assert receipt is not None
        destination = receipt / operation.remove
        if destination.exists():
            raise ValueError(f"trash destination already exists: {destination}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            source.rename(destination)
        except OSError as error:
            raise ValueError(
                f"could not move {operation.remove} to trash receipt {receipt}: {error}"
            ) from error
        if progress is not None:
            progress(CompactProgress("trash", completed, total, operation.remove))
    if receipt is not None:
        _complete_trash_receipt(receipt)
    audit = root / ".muse" / "audit"
    audit.mkdir(parents=True, exist_ok=True)
    plan_path(root).replace(audit / f"compact-{datetime.now(UTC):%Y%m%dT%H%M%SZ}.json")
    return receipt
