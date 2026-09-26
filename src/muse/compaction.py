"""Conservative exact-tree compaction planning and application."""

from __future__ import annotations

import json
import shutil
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from muse.duplicates import find_duplicates
from muse.tree_diff import compare_trees

PLAN_NAME = "compact-plan.json"
REMOVABLE_AREAS = frozenset({"backlog", "stopgap", "slag"})
RETENTION_PRIORITY = {"master": 0, "backlog": 1, "stopgap": 2, "incoming": 3, "slag": 4}


@dataclass(frozen=True)
class CompactOperation:
    retain: str
    remove: str
    files: int
    logical_bytes: int
    tree_sha256: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


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


def apply_plan(root: Path, operations: list[CompactOperation]) -> None:
    """Reverify every operation, then remove only planned duplicate trees."""
    for operation in operations:
        retain = (root / operation.retain).resolve()
        remove = (root / operation.remove).resolve()
        if _area(operation.remove) not in REMOVABLE_AREAS or root not in remove.parents:
            raise ValueError(f"unsafe removal path in plan: {operation.remove}")
        comparison = compare_trees(retain, remove, root / ".muse" / "muse.db")
        if comparison.errors or comparison.differences:
            raise ValueError(f"planned trees no longer match: {operation.remove}")
    for operation in operations:
        shutil.rmtree(root / operation.remove)
    audit = root / ".muse" / "audit"
    audit.mkdir(parents=True, exist_ok=True)
    plan_path(root).replace(audit / f"compact-{datetime.now(UTC):%Y%m%dT%H%M%SZ}.json")
