"""Previewable bootstrap of durable item identity for existing Master files."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from muse import ledger
from muse.filesystem import WalkError, walk
from muse.hashing import FileIdentity, sha256_file
from muse.media import MediaInspectionError, inspect_media
from muse.mutation import archive_record, atomic_json, checked_path, mutation_lock, sync_directory
from muse.repository import AUDIO_EXTENSIONS

SCHEMA_VERSION = 1
PLAN_NAME = "register-plan.json"


@dataclass(frozen=True)
class RegistrationProgress:
    completed_files: int
    total_files: int
    completed_bytes: int
    total_bytes: int
    current_path: str | None = None
    complete: bool = False


@dataclass(frozen=True)
class RegistrationStatus:
    master_audio_files: int
    registered_files: int
    unregistered_files: int
    stale_item_paths: int
    unregistered_examples: tuple[str, ...]
    stale_examples: tuple[str, ...]
    errors: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RegistrationFile:
    path: str
    size: int
    sha256: str


@dataclass(frozen=True)
class RegistrationPlan:
    schema_version: int
    target: str
    state: str
    created_at: str
    files: tuple[RegistrationFile, ...]
    already_registered: tuple[str, ...]
    ignored_files: int

    @property
    def logical_bytes(self) -> int:
        return sum(item.size for item in self.files)

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["files"] = [asdict(item) for item in self.files]
        value["already_registered"] = list(self.already_registered)
        value["logical_bytes"] = self.logical_bytes
        return value


class RegistrationValidationError(ValueError):
    """All blockers found while validating existing canonical files."""

    def __init__(self, errors: list[str]) -> None:
        self.errors = tuple(errors)
        super().__init__("registration is not ready:\n- " + "\n- ".join(errors))


def plan_path(root: Path) -> Path:
    return root / ".muse" / PLAN_NAME


def _target(root: Path, target: Path) -> tuple[Path, str]:
    root = root.absolute()
    target = target.absolute()
    master = root / "master"
    if target != master and not target.is_relative_to(master):
        raise ValueError("registration target must be master/ or a path beneath it")
    relative = target.relative_to(root).as_posix()
    checked = checked_path(root, relative, area="master")
    if not checked.exists():
        raise ValueError("registration target does not exist")
    if checked.is_symlink() or not (checked.is_file() or checked.is_dir()):
        raise ValueError("registration target must be a real file or directory")
    return checked, relative


def _registered_paths(root: Path) -> set[str]:
    database = root / ".muse" / "muse.db"
    if not database.exists():
        return set()
    with sqlite3.connect(database) as connection:
        table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'library_items'"
        ).fetchone()
        if table is None:
            return set()
        return {str(row[0]) for row in connection.execute("SELECT path FROM library_items")}


def inspect_status(root: Path) -> RegistrationStatus:
    """Compare Master audio paths with durable items without changing state."""
    root = root.absolute()
    master = root / "master"
    if not master.is_dir() or master.is_symlink():
        return RegistrationStatus(0, 0, 0, 0, (), (), ())
    audio, errors, _ignored = _entries(master, root)
    actual = {path.relative_to(root).as_posix() for path in audio}
    try:
        recorded = {path for path in _registered_paths(root) if path.startswith("master/")}
    except sqlite3.Error as error:
        errors.append(f"cannot read operational item ledger: {error}")
        recorded = set()
    unregistered = sorted(actual - recorded)
    stale = sorted(recorded - actual)
    return RegistrationStatus(
        master_audio_files=len(actual),
        registered_files=len(actual & recorded),
        unregistered_files=len(unregistered),
        stale_item_paths=len(stale),
        unregistered_examples=tuple(unregistered[:10]),
        stale_examples=tuple(stale[:10]),
        errors=tuple(errors),
    )


def _entries(target: Path, root: Path) -> tuple[list[Path], list[str], int]:
    audio: list[Path] = []
    errors: list[str] = []
    ignored = 0
    for entry in walk(target):
        if isinstance(entry, WalkError):
            errors.append(f"{entry.path}: {entry.message}")
        elif entry.kind == "symlink":
            errors.append(f"symbolic links are not supported: {entry.path.relative_to(root)}")
        elif entry.kind == "file":
            if entry.path.suffix.lower() in AUDIO_EXTENSIONS:
                audio.append(entry.path)
            else:
                ignored += 1
        elif entry.kind not in {"directory"}:
            errors.append(f"special files are not supported: {entry.path.relative_to(root)}")
    return sorted(audio), errors, ignored


def _inspect_file(path: Path, root: Path) -> RegistrationFile:
    relative = path.relative_to(root).as_posix()
    before = FileIdentity.from_path(path)
    inspect_media(path)
    digest = sha256_file(path, expected=before)
    return RegistrationFile(relative, before.size, digest)


def _write(path: Path, plan: RegistrationPlan) -> None:
    atomic_json(path, plan.to_dict())


def make_plan(
    root: Path,
    target: Path,
    progress: Callable[[RegistrationProgress], None] | None = None,
) -> RegistrationPlan:
    with mutation_lock(root):
        checked, target_text = _target(root, target)
        path = plan_path(root)
        if path.exists():
            current = load_plan(root)
            if current.state == "applying":
                raise ValueError("an applying registration plan must be resumed")
            if current.target != target_text:
                raise ValueError(
                    f"a registration plan already exists for {current.target}; "
                    "apply or abort it first"
                )

        audio, errors, ignored = _entries(checked, root)
        registered = _registered_paths(root)
        existing = tuple(
            path.relative_to(root).as_posix()
            for path in audio
            if path.relative_to(root).as_posix() in registered
        )
        candidates = [
            path for path in audio if path.relative_to(root).as_posix() not in registered
        ]
        total_bytes = 0
        for candidate in candidates:
            try:
                total_bytes += candidate.stat().st_size
            except OSError as error:
                errors.append(f"{candidate.relative_to(root)}: {error}")

        files: list[RegistrationFile] = []
        completed_bytes = 0
        for index, candidate in enumerate(candidates):
            relative = candidate.relative_to(root).as_posix()
            try:
                item = _inspect_file(candidate, root)
                files.append(item)
                completed_bytes += item.size
            except (OSError, MediaInspectionError) as error:
                details = error.errors if isinstance(error, MediaInspectionError) else (str(error),)
                errors.extend(f"{relative}: {detail}" for detail in details)
            if progress is not None:
                progress(
                    RegistrationProgress(
                        index + 1,
                        len(candidates),
                        completed_bytes,
                        total_bytes,
                        relative,
                    )
                )
        if progress is not None:
            progress(
                RegistrationProgress(
                    len(candidates),
                    len(candidates),
                    completed_bytes,
                    total_bytes,
                    complete=True,
                )
            )
        if errors:
            raise RegistrationValidationError(errors)

        plan = RegistrationPlan(
            SCHEMA_VERSION,
            target_text,
            "ready",
            datetime.now(UTC).isoformat(),
            tuple(files),
            existing,
            ignored,
        )
        _write(path, plan)
        return plan


def load_plan(root: Path) -> RegistrationPlan:
    path = plan_path(root)
    if not path.is_file():
        raise ValueError("no current registration plan exists")
    value = json.loads(path.read_text())
    if value.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("unsupported registration plan version")
    return RegistrationPlan(
        value["schema_version"],
        value["target"],
        value["state"],
        value["created_at"],
        tuple(RegistrationFile(**item) for item in value["files"]),
        tuple(value.get("already_registered", [])),
        value.get("ignored_files", 0),
    )


def _verify_scope(root: Path, plan: RegistrationPlan) -> None:
    target = checked_path(root, plan.target, area="master")
    audio, errors, _ignored = _entries(target, root)
    actual = {path.relative_to(root).as_posix() for path in audio}
    expected = {*plan.already_registered, *(item.path for item in plan.files)}
    if errors:
        raise RegistrationValidationError(errors)
    if actual != expected:
        raise ValueError("Master audio paths have changed since registration was planned")


def apply_plan(
    root: Path,
    progress: Callable[[RegistrationProgress], None] | None = None,
) -> RegistrationPlan:
    with mutation_lock(root):
        path = plan_path(root)
        plan = load_plan(root)
        if plan.state not in {"ready", "applying", "completed"}:
            raise ValueError(f"registration plan cannot be applied from state {plan.state}")
        _verify_scope(root, plan)

        verified: list[RegistrationFile] = []
        total_bytes = plan.logical_bytes
        completed_bytes = 0
        for index, expected in enumerate(plan.files):
            file_path = checked_path(root, expected.path, area="master")
            try:
                actual = _inspect_file(file_path, root)
            except (OSError, MediaInspectionError) as error:
                raise ValueError(f"cannot reverify {expected.path}: {error}") from error
            if actual != expected:
                raise ValueError(
                    f"content has changed since registration was planned: {expected.path}"
                )
            verified.append(actual)
            completed_bytes += actual.size
            if progress is not None:
                progress(
                    RegistrationProgress(
                        index + 1,
                        len(plan.files),
                        completed_bytes,
                        total_bytes,
                        expected.path,
                    )
                )
        if progress is not None:
            progress(
                RegistrationProgress(
                    len(plan.files),
                    len(plan.files),
                    completed_bytes,
                    total_bytes,
                    complete=True,
                )
            )

        applying = replace(plan, state="applying")
        _write(path, applying)
        database = root / ".muse" / "muse.db"
        database.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(database) as connection:
            ledger.initialize_database(connection)
            ledger.admit(
                connection,
                ((Path(item.path), item.size, item.sha256) for item in verified),
                reason="existing-master-registration",
                observed_at=plan.created_at,
            )

        completed = replace(plan, state="completed")
        _write(path, completed)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        archive_record(path, root / ".muse" / "audit", f"register-{stamp}")
        return completed


def abort_plan(root: Path) -> RegistrationPlan:
    with mutation_lock(root):
        path = plan_path(root)
        plan = load_plan(root)
        if plan.state != "ready":
            raise ValueError("only a ready registration plan can be aborted")
        path.unlink()
        sync_directory(path.parent)
        return plan
