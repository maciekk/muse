"""Strict media validation plus durable import planning and application."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mutagen import MutagenError

from muse.filesystem import WalkError, walk
from muse.hashing import sha256_file
from muse.media import MediaInfo, MediaInspectionError, inspect_media
from muse.media_fixup import embed_cover, is_obvious_cover, repair_tags
from muse.repository import AUDIO_EXTENSIONS

SCHEMA_VERSION = 3
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
class ImportArtifact:
    path: str
    size: int
    sha256: str
    kind: str


@dataclass(frozen=True)
class ImportPlan:
    schema_version: int
    source: str
    destination: str
    profile: str
    state: str
    created_at: str
    files: tuple[ImportFile, ...]
    artifacts: tuple[ImportArtifact, ...] = ()
    fixups: tuple[str, ...] = ()
    accepted_inconsistent_album_artists: bool = False

    @property
    def logical_bytes(self) -> int:
        return sum(item.size for item in (*self.files, *self.artifacts))

    @property
    def duration_seconds(self) -> float:
        return sum(item.media.duration_seconds for item in self.files)

    @property
    def warnings(self) -> tuple[str, ...]:
        technical: dict[tuple[str, int, int | None], int] = {}
        for item in self.files:
            key = (item.media.codec, item.media.sample_rate, item.media.bit_depth)
            technical[key] = technical.get(key, 0) + 1
        warnings: list[str] = []
        if len(technical) > 1:
            details = ", ".join(
                f"{codec}/{rate} Hz/{depth or 'unknown'} bit ({count} "
                f"{'file' if count == 1 else 'files'})"
                for (codec, rate, depth), count in sorted(
                    technical.items(), key=lambda value: str(value[0])
                )
            )
            warnings.append(f"mixed source audio parameters preserved as-is: {details}")
        if self.accepted_inconsistent_album_artists:
            artists = sorted({item.media.album_artist for item in self.files})
            warnings.append(f"accepted inconsistent album artist tags: {', '.join(artists)}")
        return tuple(warnings)

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["files"] = [item.to_dict() for item in self.files]
        value["artifacts"] = [asdict(item) for item in self.artifacts]
        value["fixups"] = list(self.fixups)
        value["logical_bytes"] = self.logical_bytes
        value["duration_seconds"] = self.duration_seconds
        value["warnings"] = list(self.warnings)
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
        raise ValueError("source must be a file or directory below backlog/")
    _reject_symlink_ancestors(root, source)
    return source.relative_to(root).as_posix()


def _plan_key(source: str) -> str:
    digest = hashlib.sha256(source.encode()).hexdigest()[:16]
    return f"{digest}.json"


def _current_plan_paths(root: Path) -> list[Path]:
    directory = root / ".muse" / "imports"
    return sorted(directory.glob("*.json")) if directory.is_dir() else []


def plan_path(root: Path, source: Path | None = None) -> Path:
    if source is not None:
        return root / ".muse" / "imports" / _plan_key(_relative_source(root, source))
    paths = _current_plan_paths(root)
    if not paths:
        raise ValueError("no current import plan exists")
    if len(paths) > 1:
        raise ValueError(
            "multiple import plans exist; specify a source to resolve the legacy plans"
        )
    return paths[0]


def _album_blockers(
    files: list[ImportFile], *, accept_inconsistent_album_artists: bool = False
) -> list[str]:
    blockers: list[str] = []
    for field, label in (("album", "album"), ("album_artist", "album artist")):
        values = {getattr(item.media, field) for item in files}
        if len(values) > 1 and not (field == "album_artist" and accept_inconsistent_album_artists):
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
    return blockers


def _inventory(
    source: Path,
    *,
    validate_release: bool = True,
    accept_inconsistent_album_artists: bool = False,
) -> tuple[tuple[ImportFile, ...], tuple[ImportArtifact, ...]]:
    if not source.exists():
        raise ValueError("source does not exist")
    if source.is_symlink() or not (source.is_file() or source.is_dir()):
        raise ValueError("source must be a real file or directory")

    source_is_file = source.is_file()
    entries = list(walk(source))
    candidates = sorted(
        (item for item in entries if not isinstance(item, WalkError)),
        key=lambda item: item.path,
    )
    files: list[ImportFile] = []
    artifacts: list[ImportArtifact] = []
    blockers: list[str] = [
        f"{item.path}: {item.message}" for item in entries if isinstance(item, WalkError)
    ]
    for entry in candidates:
        path = entry.path
        if path == source and not source_is_file:
            continue
        relative = "." if source_is_file else path.relative_to(source).as_posix()
        display = path.name if source_is_file else relative
        if entry.kind == "symlink":
            blockers.append(f"symbolic links are not supported: {display}")
            continue
        if entry.kind == "directory":
            continue
        if entry.kind != "file":
            blockers.append(f"special files are not supported: {display}")
            continue
        extension = path.suffix.lower()
        if extension not in AUDIO_EXTENSIONS:
            if not source_is_file and is_obvious_cover(path):
                try:
                    stat = path.stat()
                    if stat.st_size == 0:
                        blockers.append(f"empty cover image: {display}")
                    else:
                        artifacts.append(
                            ImportArtifact(relative, stat.st_size, sha256_file(path), "cover")
                        )
                except OSError as error:
                    blockers.append(f"{display}: {error}")
            else:
                blockers.append(f"unsupported non-audio file: {display}")
            continue
        if extension not in SUPPORTED_IMPORT_EXTENSIONS:
            blockers.append(f"unsupported audio format: {display} ({extension})")
            continue
        try:
            stat = path.stat()
            if stat.st_size == 0:
                blockers.append(f"empty audio file: {display}")
                continue
            media = inspect_media(path)
            sha256 = sha256_file(path)
            after = path.stat()
            identity = (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)
            after_identity = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
            if identity != after_identity:
                blockers.append(f"file changed while it was being inspected: {display}")
                continue
            files.append(ImportFile(relative, stat.st_size, sha256, media))
        except (OSError, MediaInspectionError) as error:
            details = error.errors if isinstance(error, MediaInspectionError) else (str(error),)
            blockers.extend(f"{display}: {detail}" for detail in details)
    if not files and not blockers:
        blockers.append("source contains no audio files")
    if files and validate_release:
        blockers.extend(
            _album_blockers(
                files,
                accept_inconsistent_album_artists=accept_inconsistent_album_artists,
            )
        )
    if blockers:
        raise ImportValidationError(blockers)
    return tuple(files), tuple(artifacts)


def _prepare(source: Path, *, single: bool = False) -> tuple[str, ...]:
    """Apply deterministic tag defaults and embed an unambiguous nearby cover."""
    entries = list(walk(source))
    for item in entries:
        if isinstance(item, WalkError):
            raise ValueError(f"automatic fixup failed for {item.path}: {item.message}")
    regular = [
        item.path for item in entries if not isinstance(item, WalkError) and item.kind == "file"
    ]
    audio_paths = (
        [source]
        if source.is_file()
        else sorted(path for path in regular if path.suffix.lower() in SUPPORTED_IMPORT_EXTENSIONS)
    )
    covers = [] if source.is_file() else sorted(path for path in regular if is_obvious_cover(path))
    changes: list[str] = []
    for audio in audio_paths:
        try:
            changes.extend(repair_tags(audio, single=single))
            nearby = [cover for cover in covers if cover.parent == audio.parent]
            if not nearby and source.is_dir():
                nearby = [cover for cover in covers if cover.parent == source]
            if len(nearby) == 1 and embed_cover(audio, nearby[0]):
                changes.append(f"{audio.name}: embedded cover from {nearby[0].name}")
        except (OSError, MutagenError) as error:
            raise ValueError(f"automatic fixup failed for {audio.name}: {error}") from error
    return tuple(changes)


def _destination(root: Path, source: Path, value: str | Path) -> Path:
    raw = Path(value)
    if raw.is_absolute() or ".." in raw.parts:
        raise ValueError("destination must be a relative path beneath master/")
    if raw.parts[:1] == ("master",):
        raw = Path(*raw.parts[1:])
    elif raw.parts and raw.parts[0] in {
        "backlog",
        "incoming",
        "stopgap",
        "slag",
        "trash",
        ".muse",
    }:
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
    elif source.is_file() and destination.suffix.lower() != source.suffix.lower():
        # A non-audio destination for a file is naturally a collection directory.
        destination /= source.name
    if destination.exists():
        raise ValueError("final destination already exists")
    if source.is_file() and destination.suffix.lower() != source.suffix.lower():
        raise ValueError("a file destination must preserve the source extension")
    existing_parent = destination.parent
    while not existing_parent.exists() and existing_parent != root:
        existing_parent = existing_parent.parent
    if not existing_parent.is_dir():
        raise ValueError("destination has a non-directory parent")
    _reject_symlink_ancestors(root, existing_parent)
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


def _path_component(value: str, label: str) -> str:
    """Keep tag-derived suggestions to one safe, visible path component."""
    component = "".join(" " if ord(character) < 32 else character for character in value)
    component = component.replace("/", "／").strip()
    if component in {"", ".", ".."}:
        raise ValueError(f"cannot suggest a destination from the {label} tag {value!r}")
    return component


def _suggested_destination(source: Path, files: tuple[ImportFile, ...], profile: str) -> Path:
    album_artists = {item.media.album_artist for item in files}
    if len(album_artists) > 1:
        raise ValueError(
            "cannot suggest an artists/ destination with inconsistent album artist tags; "
            "provide an explicit destination"
        )
    media = files[0].media
    artist = _path_component(media.album_artist, "album artist")
    album = _path_component(media.album, "album")
    if profile == "release":
        suggestion = Path("artists", artist, album)
    elif profile == "standalone-single":
        suggestion = Path("artists", artist, "singles", album)
    else:
        suggestion = Path("artists", artist, "selections", album)
    # Spell out the filename so an album name ending in an audio extension cannot
    # accidentally turn a collection directory into an exact file destination.
    return suggestion / source.name if source.is_file() else suggestion


def make_plan(
    root: Path,
    source: Path,
    destination: str | Path | None = None,
    *,
    accept_inconsistent_album_artists: bool = False,
) -> ImportPlan:
    """Validate a release or single and create or replace its one ready import plan."""
    source_text = _relative_source(root, source)
    destination_path = _destination(root, source, destination) if destination is not None else None
    path = root / ".muse" / "imports" / _plan_key(source_text)
    current_paths = _current_plan_paths(root)
    if len(current_paths) > 1:
        raise ValueError("multiple import plans exist; apply or abort them before creating another")
    if current_paths and current_paths[0] != path:
        current = load_plan(root)
        raise ValueError(
            f"an import plan already exists for {current.source}; apply or abort it first"
        )
    if path.exists() and load_plan(root, source).state == "applying":
        raise ValueError("an applying import plan cannot be replaced")
    importing_into_singles = destination_path is not None and "singles" in (
        destination_path.relative_to(root / "master").parts
    )
    fixups = _prepare(source, single=importing_into_singles)
    files, artifacts = _inventory(
        source,
        validate_release=source.is_dir(),
        accept_inconsistent_album_artists=accept_inconsistent_album_artists,
    )
    accepted_inconsistent_album_artists = (
        accept_inconsistent_album_artists and len({item.media.album_artist for item in files}) > 1
    )
    media = files[0].media if len(files) == 1 else None
    standalone = media is not None and (
        media.track_number == 1
        and media.track_total in {None, 1}
        and media.disc_number == 1
        and media.disc_total in {None, 1}
    )
    if standalone:
        profile = "standalone-single"
    elif source.is_file():
        profile = "selection"
    else:
        profile = "release"
    if destination_path is None:
        suggestion = _suggested_destination(source, files, profile)
        destination_path = _destination(root, source, suggestion)
    plan = ImportPlan(
        schema_version=SCHEMA_VERSION,
        source=source_text,
        destination=destination_path.relative_to(root).as_posix(),
        profile=profile,
        state="ready",
        created_at=datetime.now(UTC).isoformat(),
        files=files,
        artifacts=artifacts,
        fixups=fixups,
        accepted_inconsistent_album_artists=accepted_inconsistent_album_artists,
    )
    _write(path, plan)
    return plan


def load_plan(root: Path, source: Path | None = None) -> ImportPlan:
    path = plan_path(root, source)
    if not path.is_file():
        assert source is not None
        relative = _relative_source(root, source)
        raise ValueError(
            f"no import plan exists for {relative}; provide a destination to create one"
        )
    value = json.loads(path.read_text())
    if value.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("unsupported import plan version")
    return ImportPlan(
        schema_version=value["schema_version"],
        source=value["source"],
        destination=value["destination"],
        profile=value["profile"],
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
        artifacts=tuple(ImportArtifact(**item) for item in value.get("artifacts", [])),
        fixups=tuple(value.get("fixups", [])),
        accepted_inconsistent_album_artists=value.get("accepted_inconsistent_album_artists", False),
    )


def _verify(
    path: Path,
    expected: tuple[ImportFile, ...],
    expected_artifacts: tuple[ImportArtifact, ...],
    profile: str,
    accepted_inconsistent_album_artists: bool,
) -> None:
    try:
        actual, actual_artifacts = _inventory(
            path,
            validate_release=profile != "selection",
            accept_inconsistent_album_artists=accepted_inconsistent_album_artists,
        )
    except ImportValidationError as error:
        raise ValueError(f"content has changed since the import was planned: {error}") from error
    if actual != expected or actual_artifacts != expected_artifacts:
        raise ValueError("content has changed since the import was planned")


def apply_plan(root: Path, source: Path | None = None) -> ImportPlan:
    """Revalidate and atomically rename the ready release or single into master."""
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
        _verify(
            source_path,
            plan.files,
            plan.artifacts,
            plan.profile,
            plan.accepted_inconsistent_album_artists,
        )
        applying = ImportPlan(**{**plan.__dict__, "state": "applying"})
        _write(path, applying)
        _reject_symlink_ancestors(root, destination.parent)
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise ValueError(f"cannot create destination directories: {error}") from error
        source_path.rename(destination)
        plan = applying
    _verify(
        destination,
        plan.files,
        plan.artifacts,
        plan.profile,
        plan.accepted_inconsistent_album_artists,
    )

    completed = ImportPlan(**{**plan.__dict__, "state": "completed"})
    _write(path, completed)
    audit = root / ".muse" / "audit"
    audit.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path.replace(audit / f"import-{stamp}-{_plan_key(plan.source)}")
    return completed


def abort_plan(root: Path, source: Path | None = None) -> ImportPlan:
    path = plan_path(root, source)
    plan = load_plan(root, source)
    if plan.state != "ready":
        raise ValueError("only a ready import plan can be aborted")
    path.unlink()
    return plan
