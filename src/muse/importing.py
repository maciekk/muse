"""Durable planning and application for minimal audio-only imports."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from muse.repository import AUDIO_EXTENSIONS

SCHEMA_VERSION = 1


@dataclass(frozen=True)
class ImportFile:
    path: str
    size: int
    sha256: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ImportPlan:
    schema_version: int
    source: str
    destination: str
    state: str
    created_at: str
    files: tuple[ImportFile, ...]

    @property
    def logical_bytes(self) -> int:
        return sum(item.size for item in self.files)

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["files"] = [item.to_dict() for item in self.files]
        value["logical_bytes"] = self.logical_bytes
        return value


def _reject_symlink_ancestors(root: Path, path: Path) -> None:
    current = path
    while current != root:
        if current.is_symlink():
            raise ValueError(f"symbolic path components are not supported: {current}")
        current = current.parent


def _relative_source(root: Path, source: Path) -> str:
    root = root.absolute()
    source = source.absolute()
    backlog = root / "backlog"
    if source == backlog or not source.is_relative_to(backlog):
        raise ValueError("source must be a directory below backlog/")
    _reject_symlink_ancestors(root, source)
    return source.relative_to(root).as_posix()


def _plan_key(source: str) -> str:
    digest = hashlib.sha256(source.encode()).hexdigest()[:16]
    return f"{digest}.json"


def plan_path(root: Path, source: Path) -> Path:
    return root / ".muse" / "imports" / _plan_key(_relative_source(root, source))


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _inventory(source: Path) -> tuple[ImportFile, ...]:
    if not source.exists():
        raise ValueError("source does not exist")
    if not source.is_dir() or source.is_symlink():
        raise ValueError("source must be a real directory")

    files: list[ImportFile] = []
    for path in sorted(source.rglob("*")):
        relative = path.relative_to(source).as_posix()
        if path.is_symlink():
            raise ValueError(f"symbolic links are not supported yet: {relative}")
        if path.is_dir():
            continue
        if not path.is_file():
            raise ValueError(f"special files are not supported: {relative}")
        if path.suffix.lower() not in AUDIO_EXTENSIONS:
            raise ValueError(f"non-audio files are not supported yet: {relative}")
        stat = path.stat()
        if stat.st_size == 0:
            raise ValueError(f"empty audio file: {relative}")
        files.append(ImportFile(relative, stat.st_size, _hash(path)))
    if not files:
        raise ValueError("source contains no audio files")
    return tuple(files)


def _destination(root: Path, source: Path, value: str | Path) -> Path:
    raw = Path(value)
    if raw.is_absolute() or ".." in raw.parts:
        raise ValueError("destination must be a relative path beneath master/")
    if raw.parts[:1] == ("master",):
        raw = Path(*raw.parts[1:])
    elif raw.parts and raw.parts[0] in {"backlog", "incoming", "stopgap", "slag", ".muse"}:
        raise ValueError("destination must be beneath master/")
    if not raw.parts:
        raise ValueError("destination must name a path beneath master/")

    destination = (root / "master" / raw).absolute()
    master = (root / "master").absolute()
    if not destination.is_relative_to(master):
        raise ValueError("destination must be beneath master/")
    if destination.exists():
        if not destination.is_dir() or destination.is_symlink():
            raise ValueError("destination already exists and is not a directory")
        destination /= source.name
    if destination.exists():
        raise ValueError("final destination already exists")
    if not destination.parent.is_dir():
        raise ValueError("destination parent does not exist")
    _reject_symlink_ancestors(root, destination.parent)
    return destination


def _write(path: Path, plan: ImportPlan) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    with temporary.open("w") as stream:
        json.dump(plan.to_dict(), stream, indent=2, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def make_plan(root: Path, source: Path, destination: str | Path) -> ImportPlan:
    """Create or replace the one ready plan for an audio-only backlog source."""
    source_text = _relative_source(root, source)
    destination_path = _destination(root, source, destination)
    path = root / ".muse" / "imports" / _plan_key(source_text)
    if path.exists() and load_plan(root, source).state == "applying":
        raise ValueError("an applying import plan cannot be replaced")
    plan = ImportPlan(
        schema_version=SCHEMA_VERSION,
        source=source_text,
        destination=destination_path.relative_to(root).as_posix(),
        state="ready",
        created_at=datetime.now(UTC).isoformat(),
        files=_inventory(source),
    )
    _write(path, plan)
    return plan


def load_plan(root: Path, source: Path) -> ImportPlan:
    path = plan_path(root, source)
    value = json.loads(path.read_text())
    if value.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("unsupported import plan version")
    return ImportPlan(
        schema_version=value["schema_version"],
        source=value["source"],
        destination=value["destination"],
        state=value["state"],
        created_at=value["created_at"],
        files=tuple(ImportFile(**item) for item in value["files"]),
    )


def _verify(directory: Path, expected: tuple[ImportFile, ...]) -> None:
    actual = _inventory(directory)
    if actual != expected:
        raise ValueError("content has changed since the import was planned")


def apply_plan(root: Path, source: Path) -> ImportPlan:
    """Reverify and atomically rename a planned audio-only directory into master."""
    path = plan_path(root, source)
    plan = load_plan(root, source)
    source_path = root / plan.source
    destination = root / plan.destination

    source_exists = source_path.exists()
    destination_exists = destination.exists()
    if source_exists and destination_exists:
        raise ValueError("both source and destination exist")
    if not source_exists and not destination_exists:
        raise ValueError("neither source nor destination exists")
    if source_exists:
        if plan.state not in {"ready", "applying"}:
            raise ValueError(f"plan cannot be applied from state {plan.state}")
        _verify(source_path, plan.files)
        if not destination.parent.is_dir():
            raise ValueError("destination parent no longer exists")
        applying = ImportPlan(**{**plan.__dict__, "state": "applying"})
        _write(path, applying)
        source_path.rename(destination)
        plan = applying
    _verify(destination, plan.files)

    completed = ImportPlan(**{**plan.__dict__, "state": "completed"})
    _write(path, completed)
    audit = root / ".muse" / "audit"
    audit.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path.replace(audit / f"import-{stamp}-{_plan_key(plan.source)}")
    return completed


def abort_plan(root: Path, source: Path) -> ImportPlan:
    path = plan_path(root, source)
    plan = load_plan(root, source)
    if plan.state != "ready":
        raise ValueError("only a ready import plan can be aborted")
    path.unlink()
    return plan
