"""Previewable, disconnected publication of registered Master items to Music.app."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from muse import apple_ledger, ledger
from muse.apple_exchange import export_request_bundle, import_result_bundle, load_request_bundle
from muse.apple_protocol import AppleOperation, AppleRequest, StagedFile, TrackMetadata, new_id
from muse.filesystem import WalkError, walk
from muse.hashing import FileIdentity, sha256_file
from muse.media import MediaInfo, MediaInspectionError, inspect_media
from muse.mutation import archive_record, atomic_json, checked_path, mutation_lock, sync_directory
from muse.repository import AUDIO_EXTENSIONS

SCHEMA_VERSION = 1
PLAN_NAME = "apple-publication-plan.json"


@dataclass(frozen=True)
class PublicationItem:
    item_id: int
    operation_id: str
    path: str
    size: int
    source_sha256: str
    metadata: dict[str, Any]
    conversion: str


@dataclass(frozen=True)
class PublicationPlan:
    schema_version: int
    batch_id: str
    target: str
    mode: str
    state: str
    created_at: str
    items: tuple[PublicationItem, ...]
    excluded_paths: tuple[str, ...]
    already_published_paths: tuple[str, ...]
    request_bundle: str | None = None

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["items"] = [asdict(item) for item in self.items]
        value["excluded_paths"] = list(self.excluded_paths)
        value["already_published_paths"] = list(self.already_published_paths)
        return value


class PublicationValidationError(ValueError):
    """Selected content cannot safely enter an Apple publication plan."""

    def __init__(self, errors: list[str]) -> None:
        self.errors = tuple(errors)
        super().__init__("Apple publication is not ready:\n- " + "\n- ".join(errors))


def plan_path(root: Path) -> Path:
    return root / ".muse" / PLAN_NAME


def _database(root: Path) -> Path:
    return root / ".muse" / "muse.db"


def _target(root: Path, target: Path) -> tuple[Path, str]:
    root = root.absolute()
    target = target.absolute()
    master = root / "master"
    if target != master and not target.is_relative_to(master):
        raise ValueError("Apple publication target must be master/ or a path beneath it")
    relative = target.relative_to(root).as_posix()
    checked = checked_path(root, relative, area="master")
    if checked.is_symlink() or not (checked.is_file() or checked.is_dir()):
        raise ValueError("Apple publication target must be a real file or directory")
    return checked, relative


def _metadata(info: MediaInfo) -> dict[str, Any]:
    return asdict(
        TrackMetadata(
            title=info.title,
            artist=info.artist,
            album=info.album,
            album_artist=info.album_artist,
            track_number=info.track_number,
            track_total=info.track_total,
            disc_number=info.disc_number,
            disc_total=info.disc_total,
            year=info.year,
        )
    )


def _conversion(info: MediaInfo) -> str:
    return "copy" if info.container in {"mp3", "m4a"} else "alac"


def _audio_paths(target: Path, root: Path) -> tuple[set[str], list[str]]:
    paths: set[str] = set()
    errors: list[str] = []
    for entry in walk(target):
        if isinstance(entry, WalkError):
            errors.append(f"{entry.path}: {entry.message}")
        elif entry.kind == "symlink":
            errors.append(f"symbolic links are not supported: {entry.path.relative_to(root)}")
        elif entry.kind == "file" and entry.path.suffix.lower() in AUDIO_EXTENSIONS:
            paths.add(entry.path.relative_to(root).as_posix())
    return paths, errors


def _selected_rows(connection: sqlite3.Connection, target: str) -> list[tuple[Any, ...]]:
    return connection.execute(
        """
        SELECT i.id, i.path, i.current_sha256, d.decision,
               m.source_sha256, m.apple_persistent_id
        FROM library_items AS i
        LEFT JOIN apple_publication_decisions AS d ON d.item_id = i.id
        LEFT JOIN apple_item_mappings AS m ON m.item_id = i.id
        WHERE i.area = 'master'
          AND (i.path = ? OR substr(i.path, 1, length(?) + 1) = ? || '/')
        ORDER BY i.path
        """,
        (target, target, target),
    ).fetchall()


def make_plan(
    root: Path,
    target: Path,
    *,
    exclude: bool = False,
    include_excluded: bool = False,
) -> PublicationPlan:
    """Inspect a Master selection and save its sole current publication plan."""
    if exclude and include_excluded:
        raise ValueError("--exclude and --include-excluded cannot be combined")
    root = root.absolute()
    if not root.is_dir():
        raise ValueError(f"library root does not exist: {root}")
    with mutation_lock(root):
        checked, target_text = _target(root, target)
        current_path = plan_path(root)
        if current_path.exists() and load_plan(root).state in {"exported", "uncertain"}:
            raise ValueError("the current Apple publication plan must be reconciled first")
        database = _database(root)
        if not database.is_file():
            raise ValueError("no durable item ledger exists; register Master files first")
        with sqlite3.connect(database) as connection:
            ledger.initialize_database(connection)
            apple_ledger.initialize_database(connection)
            rows = _selected_rows(connection, target_text)
        registered_paths = {str(row[1]) for row in rows}
        audio_paths, inventory_errors = _audio_paths(checked, root)
        unregistered = sorted(audio_paths - registered_paths)
        if unregistered:
            inventory_errors.extend(
                f"{path}: Master audio is not registered" for path in unregistered
            )
        if inventory_errors:
            raise PublicationValidationError(inventory_errors)
        if not rows:
            raise ValueError("Apple publication target contains no registered Master items")

        items: list[PublicationItem] = []
        excluded_paths: list[str] = []
        published_paths: list[str] = []
        errors: list[str] = []
        for item_id, path_text, current_sha256, decision, mapped_sha256, persistent_id in rows:
            path_text = str(path_text)
            if exclude:
                items.append(
                    PublicationItem(
                        int(item_id), new_id(), path_text, 0, str(current_sha256), {}, "none"
                    )
                )
                continue
            if decision == "exclude" and not include_excluded:
                excluded_paths.append(path_text)
                continue
            if persistent_id is not None:
                if str(mapped_sha256) == str(current_sha256):
                    published_paths.append(path_text)
                else:
                    errors.append(
                        f"{path_text}: published source changed; metadata/media update "
                        "is not supported yet"
                    )
                continue
            path = checked_path(root, path_text, area="master")
            try:
                identity = FileIdentity.from_path(path)
                digest = sha256_file(path, expected=identity)
                if digest != str(current_sha256):
                    errors.append(f"{path_text}: current file does not match its admitted version")
                    continue
                info = inspect_media(path)
            except (OSError, MediaInspectionError, ValueError) as error:
                errors.append(f"{path_text}: {error}")
                continue
            items.append(
                PublicationItem(
                    int(item_id),
                    new_id(),
                    path_text,
                    identity.size,
                    digest,
                    _metadata(info),
                    _conversion(info),
                )
            )
        if errors:
            raise PublicationValidationError(errors)

        plan = PublicationPlan(
            SCHEMA_VERSION,
            new_id(),
            target_text,
            "exclude" if exclude else "include",
            "ready",
            datetime.now(UTC).isoformat(),
            tuple(items),
            tuple(excluded_paths),
            tuple(published_paths),
        )
        path = plan_path(root)
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_json(path, plan.to_dict())
        return plan


def load_plan(root: Path) -> PublicationPlan:
    path = plan_path(root)
    if not path.is_file():
        raise ValueError("no current Apple publication plan exists")
    value = json.loads(path.read_text())
    if value.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("unsupported Apple publication plan version")
    return PublicationPlan(
        schema_version=value["schema_version"],
        batch_id=value["batch_id"],
        target=value["target"],
        mode=value["mode"],
        state=value["state"],
        created_at=value["created_at"],
        items=tuple(PublicationItem(**item) for item in value["items"]),
        excluded_paths=tuple(value.get("excluded_paths", [])),
        already_published_paths=tuple(value.get("already_published_paths", [])),
        request_bundle=value.get("request_bundle"),
    )


def _verify_item(root: Path, item: PublicationItem) -> Path:
    path = checked_path(root, item.path, area="master")
    identity = FileIdentity.from_path(path)
    if identity.size != item.size or sha256_file(path, expected=identity) != item.source_sha256:
        raise ValueError(f"content changed since publication was planned: {item.path}")
    if _metadata(inspect_media(path)) != item.metadata:
        raise ValueError(f"metadata changed since publication was planned: {item.path}")
    return path


def _stage_item(source: Path, destination: Path, conversion: str) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.tmp")
    if temporary.exists():
        temporary.unlink()
    try:
        if conversion == "copy":
            with source.open("rb") as input_file, temporary.open("xb") as output:
                shutil.copyfileobj(input_file, output)
                output.flush()
                os.fsync(output.fileno())
        else:
            result = subprocess.run(
                [
                    "ffmpeg",
                    "-nostdin",
                    "-v",
                    "error",
                    "-xerror",
                    "-i",
                    str(source),
                    "-map",
                    "0:a:0",
                    "-map_metadata",
                    "0",
                    "-vn",
                    "-c:a",
                    "alac",
                    "-f",
                    "ipod",
                    str(temporary),
                ],
                capture_output=True,
                check=False,
            )
            if result.returncode:
                detail = result.stderr.decode(errors="replace").strip().splitlines()
                raise ValueError(
                    f"ALAC staging conversion failed: {detail[-1] if detail else 'unknown error'}"
                )
        os.replace(temporary, destination)
        sync_directory(destination.parent)
    finally:
        if temporary.exists():
            temporary.unlink()


def _archive_completed(root: Path, label: str) -> None:
    path = plan_path(root)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    archive_record(path, root / ".muse" / "audit", f"publish-apple-{label}-{stamp}")


def apply_plan(root: Path) -> PublicationPlan:
    """Apply decisions and export a durable request bundle for manual transfer."""
    root = root.absolute()
    if not root.is_dir():
        raise ValueError(f"library root does not exist: {root}")
    with mutation_lock(root):
        plan = load_plan(root)
        if plan.state not in {"ready", "exported"}:
            raise ValueError(f"Apple publication plan cannot be applied from state {plan.state}")
        if plan.state == "exported":
            assert plan.request_bundle is not None
            load_request_bundle(root / plan.request_bundle)
            return plan

        database = _database(root)
        if plan.mode == "exclude":
            with sqlite3.connect(database) as connection:
                for item in plan.items:
                    apple_ledger.set_publication_decision(connection, item.item_id, "exclude")
            completed = replace(plan, state="completed")
            atomic_json(plan_path(root), completed.to_dict())
            _archive_completed(root, "exclude")
            return completed
        if not plan.items:
            raise ValueError("publication plan contains no new items to export")

        sources: dict[str, Path] = {}
        operations: list[AppleOperation] = []
        staging_root = root / ".muse" / "apple-staging" / plan.batch_id
        for item in plan.items:
            source = _verify_item(root, item)
            suffix = source.suffix.lower() if item.conversion == "copy" else ".m4a"
            relative = f"media/{item.item_id}{suffix}"
            staged = staging_root / relative
            if not staged.exists():
                _stage_item(source, staged, item.conversion)
            identity = FileIdentity.from_path(staged)
            staging_hash = sha256_file(staged, expected=identity)
            description = StagedFile(relative, identity.size, staging_hash)
            operations.append(
                AppleOperation(
                    operation_id=item.operation_id,
                    action="add-track",
                    item_id=item.item_id,
                    source_sha256=item.source_sha256,
                    staged_file=description,
                    metadata=TrackMetadata(**item.metadata),
                    playlist_memberships=("Muse — Master",),
                )
            )
            sources[relative] = staged
        request = AppleRequest(plan.batch_id, plan.created_at, tuple(operations))
        bundle_relative = f".muse/apple-exchange/{plan.batch_id}-request"
        bundle = root / bundle_relative
        with sqlite3.connect(database) as connection:
            for item in plan.items:
                apple_ledger.set_publication_decision(connection, item.item_id, "include")
            apple_ledger.create_publication_run(connection, request)
        export_request_bundle(bundle, request, sources)
        exported = replace(plan, state="exported", request_bundle=bundle_relative)
        atomic_json(plan_path(root), exported.to_dict())
        return exported


def record_result(root: Path, result_bundle: Path) -> PublicationPlan:
    """Accept a returned result for the current exported plan."""
    root = root.absolute()
    if not root.is_dir():
        raise ValueError(f"library root does not exist: {root}")
    with mutation_lock(root):
        plan = load_plan(root)
        if plan.state != "exported" or plan.request_bundle is None:
            raise ValueError("current Apple publication plan has no exported request")
        request = load_request_bundle(root / plan.request_bundle)
        result = import_result_bundle(result_bundle.absolute(), request)
        with sqlite3.connect(_database(root)) as connection:
            apple_ledger.record_publication_result(connection, request, result)
        statuses = {outcome.status for outcome in result.operations}
        if statuses == {"succeeded"}:
            completed = replace(plan, state="completed")
            atomic_json(plan_path(root), completed.to_dict())
            # Keep the request bundle and staging media until a later bridge capability
            # proves that Music.app copied every file into its managed media directory.
            _archive_completed(root, "result")
            return completed
        returned = replace(
            plan,
            state="uncertain" if "uncertain" in statuses else "failed",
        )
        atomic_json(plan_path(root), returned.to_dict())
        return returned
