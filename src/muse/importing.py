"""Strict media validation plus durable import planning and application."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from muse.media import MediaInfo, MediaInspectionError, inspect_media
from muse.repository import AUDIO_EXTENSIONS

SCHEMA_VERSION = 2
SUPPORTED_IMPORT_EXTENSIONS = frozenset({".flac", ".m4a", ".mp3", ".ogg", ".oga", ".opus", ".wav"})


class ImportValidationError(ValueError):
    """All readiness blockers found while inspecting an import source."""

    def __init__(self, blockers: list[str]) -> None:
        self.blockers = tuple(blockers)
        super().__init__("import is not ready:\n- " + "\n- ".join(blockers))


@dataclass(frozen=True)
class ImportFile:
    path: str
    size: int
    sha256: str
    media: MediaInfo

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

    @property
    def duration_seconds(self) -> float:
        return sum(item.media.duration_seconds for item in self.files)

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["files"] = [item.to_dict() for item in self.files]
        value["logical_bytes"] = self.logical_bytes
        value["duration_seconds"] = self.duration_seconds
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


def _album_blockers(files: list[ImportFile]) -> list[str]:
    blockers: list[str] = []
    for field, label in (("album", "album"), ("album_artist", "album artist")):
        values = {getattr(item.media, field) for item in files}
        if len(values) > 1:
            blockers.append(f"inconsistent {label} tags: {', '.join(sorted(values))}")

    positions: dict[tuple[int, int], list[str]] = {}
    by_disc: dict[int, list[ImportFile]] = {}
    for item in files:
        position = (item.media.disc_number, item.media.track_number)
        positions.setdefault(position, []).append(item.path)
        by_disc.setdefault(item.media.disc_number, []).append(item)
    for (disc, track), paths in sorted(positions.items()):
        if len(paths) > 1:
            blockers.append(f"duplicate disc {disc} track {track}: {', '.join(paths)}")
    discs = sorted(by_disc)
    if discs != list(range(1, max(discs, default=0) + 1)):
        blockers.append(f"disc numbering has gaps: {', '.join(map(str, discs))}")
    for disc, items in sorted(by_disc.items()):
        tracks = sorted(item.media.track_number for item in items)
        if tracks != list(range(1, max(tracks, default=0) + 1)):
            blockers.append(f"disc {disc} track numbering has gaps: {', '.join(map(str, tracks))}")
        totals = {item.media.track_total for item in items if item.media.track_total is not None}
        if len(totals) > 1 or (totals and next(iter(totals)) != max(tracks)):
            blockers.append(f"disc {disc} has inconsistent track totals")
    disc_totals = {item.media.disc_total for item in files if item.media.disc_total is not None}
    if len(disc_totals) > 1 or (disc_totals and next(iter(disc_totals)) != max(discs)):
        blockers.append("album has inconsistent disc totals")

    years = {item.media.year for item in files if item.media.year is not None}
    if len(years) > 1:
        blockers.append(f"inconsistent year tags: {', '.join(sorted(years))}")
    release_ids = {item.media.release_id for item in files if item.media.release_id is not None}
    if len(release_ids) > 1:
        blockers.append(f"inconsistent release identifiers: {', '.join(sorted(release_ids))}")
    technical = {(item.media.codec, item.media.sample_rate, item.media.bit_depth) for item in files}
    if len(technical) > 1:
        details = ", ".join(
            f"{codec}/{rate} Hz/{depth or 'unknown'} bit"
            for codec, rate, depth in sorted(technical, key=str)
        )
        blockers.append(f"mixed codec or audio parameters require review: {details}")
    return blockers


def _inventory(source: Path) -> tuple[ImportFile, ...]:
    if not source.exists():
        raise ValueError("source does not exist")
    if not source.is_dir() or source.is_symlink():
        raise ValueError("source must be a real directory")

    files: list[ImportFile] = []
    blockers: list[str] = []
    for path in sorted(source.rglob("*")):
        relative = path.relative_to(source).as_posix()
        if path.is_symlink():
            blockers.append(f"symbolic links are not supported: {relative}")
            continue
        if path.is_dir():
            continue
        if not path.is_file():
            blockers.append(f"special files are not supported: {relative}")
            continue
        extension = path.suffix.lower()
        if extension not in AUDIO_EXTENSIONS:
            blockers.append(f"non-audio files are not supported yet: {relative}")
            continue
        if extension not in SUPPORTED_IMPORT_EXTENSIONS:
            blockers.append(f"unsupported audio format: {relative} ({extension})")
            continue
        try:
            stat = path.stat()
            if stat.st_size == 0:
                blockers.append(f"empty audio file: {relative}")
                continue
            media = inspect_media(path)
            sha256 = _hash(path)
            after = path.stat()
            identity = (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)
            after_identity = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
            if identity != after_identity:
                blockers.append(f"file changed while it was being inspected: {relative}")
                continue
            files.append(ImportFile(relative, stat.st_size, sha256, media))
        except (OSError, MediaInspectionError) as error:
            details = error.errors if isinstance(error, MediaInspectionError) else (str(error),)
            blockers.extend(f"{relative}: {detail}" for detail in details)
    if not files and not blockers:
        blockers.append("source contains no audio files")
    if files:
        blockers.extend(_album_blockers(files))
    if blockers:
        raise ImportValidationError(blockers)
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
    """Validate an album and create or replace its one ready import plan."""
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
        files=tuple(
            ImportFile(
                path=item["path"],
                size=item["size"],
                sha256=item["sha256"],
                media=MediaInfo(**item["media"]),
            )
            for item in value["files"]
        ),
    )


def _verify(directory: Path, expected: tuple[ImportFile, ...]) -> None:
    try:
        actual = _inventory(directory)
    except ImportValidationError as error:
        raise ValueError(f"content has changed since the import was planned: {error}") from error
    if actual != expected:
        raise ValueError("content has changed since the import was planned")


def apply_plan(root: Path, source: Path) -> ImportPlan:
    """Revalidate and atomically rename a ready album directory into master."""
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
