"""Find release artwork and plan conservative embedded-cover upgrades."""

from __future__ import annotations

import base64
import json
import os
import shutil
import struct
import tempfile
import time
import uuid
from collections.abc import Callable
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from mutagen.flac import FLAC
from mutagen.id3 import ID3
from mutagen.mp4 import MP4
from mutagen.oggopus import OggOpus
from mutagen.oggvorbis import OggVorbis
from mutagen.wave import WAVE

from muse import __version__
from muse.filesystem import Entry, WalkError, walk
from muse.media_fixup import embed_cover
from muse.mutation import checked_path, mutation_lock, sync_directory

SUPPORTED = frozenset({".mp3", ".flac", ".m4a", ".ogg", ".oga", ".opus", ".wav"})
_USER_AGENT = f"muse-ctl/{__version__} (https://github.com/maciekk/muse)"
_last_mb_request = 0.0


@dataclass(frozen=True)
class Artwork:
    data: bytes
    width: int
    height: int
    suffix: str

    @property
    def pixels(self) -> int:
        return self.width * self.height


@dataclass(frozen=True)
class Track:
    path: Path
    artist: str
    album: str
    release_id: str | None
    release_group_id: str | None
    existing: Artwork | None


class ArtworkUnavailable(ValueError):
    """No confidently matched, usable front cover was found."""


def image_info(data: bytes) -> Artwork:
    """Inspect JPEG/PNG dimensions without decoding or trusting HTTP content type."""
    if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24:
        width, height = struct.unpack(">II", data[16:24])
        suffix = ".png"
    elif data.startswith(b"\xff\xd8"):
        offset = 2
        width = height = 0
        while offset + 4 <= len(data):
            if data[offset] != 0xFF:
                break
            marker = data[offset + 1]
            offset += 2
            if marker in {0xD8, 0xD9}:
                continue
            if offset + 2 > len(data):
                break
            size = int.from_bytes(data[offset : offset + 2], "big")
            if size < 2 or offset + size > len(data):
                break
            if marker in {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB}:
                if size >= 7:
                    height, width = struct.unpack(">HH", data[offset + 3 : offset + 7])
                break
            offset += size
        suffix = ".jpg"
    else:
        raise ValueError("artwork is not a JPEG or PNG")
    if not (1 <= width <= 30000 and 1 <= height <= 30000):
        raise ValueError("artwork has invalid dimensions")
    return Artwork(data, width, height, suffix)


def _value(tags: object, *names: str) -> str:
    for name in names:
        value = tags.get(name)  # type: ignore[attr-defined]
        if value:
            if isinstance(value, list):
                value = value[0]
            if isinstance(value, bytes):
                value = value.decode("utf-8", errors="replace")
            text = str(value).strip()
            if text:
                return text
    return ""


def _identifier(value: str) -> str | None:
    try:
        return str(uuid.UUID(value))
    except ValueError:
        return None


def inspect_track(path: Path) -> Track:
    suffix = path.suffix.lower()
    if suffix in {".mp3", ".wav"}:
        tags = ID3(path) if suffix == ".mp3" else WAVE(path).tags
        if tags is None:
            raise ValueError("no tags")
        artist = _value(tags, "TPE2", "TPE1")
        album = _value(tags, "TALB")
        identifiers = {
            frame.desc.casefold(): str(frame.text[0]) for frame in tags.getall("TXXX") if frame.text
        }
        images = [frame.data for frame in tags.getall("APIC") if frame.type == 3]
        if not images:
            images = [frame.data for frame in tags.getall("APIC")]
    elif suffix == ".flac":
        audio = FLAC(path)
        tags = audio.tags or {}
        artist = _value(tags, "albumartist", "artist")
        album = _value(tags, "album")
        identifiers = {key.casefold(): _value(tags, key) for key in tags}
        images = [picture.data for picture in audio.pictures if picture.type == 3]
        if not images:
            images = [picture.data for picture in audio.pictures]
    elif suffix == ".m4a":
        audio = MP4(path)
        tags = audio.tags or {}
        artist = _value(tags, "aART", "\xa9ART")
        album = _value(tags, "\xa9alb")
        identifiers = {
            key.split(":")[-1].casefold(): _value(tags, key)
            for key in tags
            if key.startswith("----:")
        }
        images = list(tags.get("covr", []))
    else:
        audio = OggOpus(path) if suffix == ".opus" else OggVorbis(path)
        tags = audio.tags or {}
        artist = _value(tags, "albumartist", "artist")
        album = _value(tags, "album")
        identifiers = {key.casefold(): _value(tags, key) for key in tags}
        images = []
        for encoded in tags.get("metadata_block_picture", []):
            from mutagen.flac import Picture

            picture = Picture(base64.b64decode(encoded))
            if picture.type == 3:
                images.append(picture.data)
    identifiers = {
        key.replace(" ", "").replace("_", "").casefold(): value
        for key, value in identifiers.items()
    }
    existing = []
    for data in images:
        try:
            existing.append(image_info(bytes(data)))
        except ValueError:
            continue
    return Track(
        path,
        artist,
        album,
        _identifier(
            identifiers.get("musicbrainzalbumid", "") or identifiers.get("musicbrainzreleaseid", "")
        ),
        _identifier(identifiers.get("musicbrainzreleasegroupid", "")),
        max(existing, key=lambda item: item.pixels, default=None),
    )


def select_tracks(root: Path, targets: list[str]) -> tuple[list[Track], list[str]]:
    paths: set[Path] = set()
    errors: list[str] = []
    for target in targets:
        source = checked_path(root, target)
        for entry in walk(source, sort=True):
            if isinstance(entry, WalkError):
                errors.append(f"{entry.path}: {entry.message}")
            elif (
                isinstance(entry, Entry)
                and entry.kind == "file"
                and entry.path.suffix.lower() in SUPPORTED
            ):
                paths.add(entry.path)
    tracks = []
    for path in sorted(paths):
        try:
            tracks.append(inspect_track(path))
        except Exception as error:
            errors.append(f"{path}: {error}")
    if not paths:
        errors.append("no supported audio files found in selected paths")
    return tracks, errors


def _get(url: str, *, musicbrainz: bool = False, max_bytes: int = 20_000_000) -> bytes:
    global _last_mb_request
    if musicbrainz:
        remaining = 1.1 - (time.monotonic() - _last_mb_request)
        if remaining > 0:
            time.sleep(remaining)
        _last_mb_request = time.monotonic()
    request = Request(
        url,
        headers={
            "User-Agent": _USER_AGENT,
            "Accept": "application/json" if musicbrainz else "image/jpeg,image/png",
        },
    )
    try:
        with urlopen(request, timeout=20) as response:
            data = response.read(max_bytes + 1)
    except HTTPError as error:
        if not musicbrainz and error.code == 404:
            raise ArtworkUnavailable("Cover Art Archive has no front cover") from error
        raise ValueError(f"download failed: {error}") from error
    except (URLError, TimeoutError) as error:
        raise ValueError(f"download failed: {error}") from error
    if len(data) > max_bytes:
        raise ValueError("artwork exceeds 20 MB")
    return data


def _release_group(artist: str, album: str) -> tuple[str, str]:
    def quoted(value: str) -> str:
        return '"' + value.replace('"', "") + '"'

    def search(title: str) -> list[dict]:
        query = f"releasegroup:{quoted(title)} AND artist:{quoted(artist)}"
        url = "https://musicbrainz.org/ws/2/release-group/?" + urlencode(
            {"query": query, "fmt": "json", "limit": 25}
        )
        result = json.loads(_get(url, musicbrainz=True))
        return [
            item
            for item in result.get("release-groups", [])
            if item.get("artist-credit", [{}])[0].get("name", "").casefold() == artist.casefold()
        ]

    matches = [
        item for item in search(album) if item.get("title", "").casefold() == album.casefold()
    ]
    if len(matches) == 1:
        return str(matches[0]["id"]), str(matches[0]["title"])
    if len(matches) > 1:
        raise ArtworkUnavailable("multiple exact MusicBrainz releases matched")

    # Old tags sometimes end midword. Search complete leading words, then require
    # one result with the same artist and a title extending the tagged prefix.
    prefix = album.strip().casefold()
    words = album.split()
    if len(prefix) < 16 or len(words) < 3:
        raise ArtworkUnavailable("no confident MusicBrainz release match")
    query_title = " ".join(words[:-1]).rstrip(" -_:")
    candidates = [
        item
        for item in search(query_title)
        if item.get("title", "").casefold().startswith(prefix)
        and len(item.get("title", "")) - len(album) <= 24
    ]
    if len(candidates) != 1:
        raise ArtworkUnavailable(
            f"no unique MusicBrainz title extending the tag (found {len(candidates)})"
        )
    return str(candidates[0]["id"]), str(candidates[0]["title"])


def find_art(
    artist: str, album: str, release_id: str | None, group_id: str | None
) -> tuple[str, Artwork, str | None]:
    matched_title = None
    if release_id:
        source = f"release/{release_id}"
    elif group_id:
        source = f"release-group/{group_id}"
    else:
        found_id, matched_title = _release_group(artist, album)
        source = f"release-group/{found_id}"
    url = f"https://coverartarchive.org/{source}/front"
    return url, image_info(_get(url)), matched_title


def upgrade(path: Path, artwork: Artwork, backup: Path) -> None:
    """Save the original, then atomically install a modified same-directory copy."""
    descriptor, name = tempfile.mkstemp(
        prefix=f".{path.stem}.art-", suffix=path.suffix, dir=path.parent
    )
    os.close(descriptor)
    temporary = Path(name)
    cover = temporary.with_suffix(artwork.suffix)
    try:
        shutil.copy2(path, temporary)
        cover.write_bytes(artwork.data)
        if not embed_cover(temporary, cover, replace=True):
            raise ValueError("unsupported audio format")
        with temporary.open("rb") as stream:
            os.fsync(stream.fileno())
        backup.parent.mkdir(parents=True, exist_ok=True)
        os.link(path, backup)
        sync_directory(backup.parent)
        os.replace(temporary, path)
        sync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)
        cover.unlink(missing_ok=True)


def plan(
    root: Path,
    targets: list[str],
    *,
    min_size: int = 600,
    force: bool = False,
    apply: bool = False,
    cover: Path | None = None,
    allow_small: bool = False,
    approve_override: Callable[[str], bool] | None = None,
) -> dict:
    with mutation_lock(root) if apply else nullcontext():
        return _plan(
            root,
            targets,
            min_size=min_size,
            force=force,
            apply=apply,
            cover=cover,
            allow_small=allow_small,
            approve_override=approve_override,
        )


def _plan(
    root: Path,
    targets: list[str],
    *,
    min_size: int = 600,
    force: bool = False,
    apply: bool = False,
    cover: Path | None = None,
    allow_small: bool = False,
    approve_override: Callable[[str], bool] | None = None,
) -> dict:
    tracks, errors = select_tracks(root, targets)
    groups: dict[tuple[str, str, str | None, str | None], list[Track]] = {}
    for track in tracks:
        if not track.album or not track.artist:
            errors.append(f"{track.path}: missing album or artist tag")
            continue
        groups.setdefault(
            (track.artist, track.album, track.release_id, track.release_group_id), []
        ).append(track)
    results = []
    skipped = []
    backup_run = uuid.uuid4().hex if apply else None
    supplied_art = None
    if cover is not None:
        if len(groups) != 1:
            raise ValueError("--cover requires paths from exactly one tagged album")
        if not cover.is_file() or cover.stat().st_size > 20_000_000:
            raise ValueError("--cover must be a JPEG or PNG file of at most 20 MB")
        supplied_art = image_info(cover.read_bytes())
    for (artist, album, release_id, group_id), members in groups.items():
        try:
            url, art, matched_title = (
                (str(cover), supplied_art, album)
                if supplied_art is not None
                else find_art(artist, album, release_id, group_id)
            )
            better = {
                track.path: (
                    track.existing is None
                    or (
                        art.width >= track.existing.width
                        and art.height >= track.existing.height
                        and (art.width > track.existing.width or art.height > track.existing.height)
                    )
                )
                for track in members
            }
            below_minimum = min(art.width, art.height) < min_size
            lower_existing = [track for track in members if not better[track.path]]
            needs_approval = (below_minimum and not allow_small) or (
                bool(lower_existing) and not force
            )
            approval = "not-needed"
            if needs_approval:
                details = [f"found artwork is {art.width}x{art.height}"]
                if below_minimum and not allow_small:
                    details.append(f"below the {min_size}px minimum")
                if lower_existing and not force:
                    dimensions = sorted(
                        {
                            f"{track.existing.width}x{track.existing.height}"
                            for track in lower_existing
                            if track.existing is not None
                        }
                    )
                    details.append(
                        f"not a resolution upgrade for {len(lower_existing)} "
                        f"file(s) ({', '.join(dimensions)})"
                    )
                prompt = (
                    f"{artist} — {album}: {'; '.join(details)}.\n"
                    f"Source: {url}\n"
                    "Replace affected artwork anyway? [y/N] "
                )
                if approve_override is None:
                    approval = "required"
                else:
                    approval = "approved" if approve_override(prompt) else "declined"
            files = []
            for track in members:
                existing = track.existing
                blocked_by_minimum = below_minimum and not allow_small and approval != "approved"
                blocked_by_resolution = (
                    not better[track.path] and not force and approval != "approved"
                )
                eligible = not (blocked_by_minimum or blocked_by_resolution)
                status = (
                    "upgrade"
                    if eligible
                    else ("needs-approval" if approval == "required" else "skip")
                )
                reason = None
                if not eligible:
                    reasons = []
                    if blocked_by_minimum:
                        reasons.append(f"below {min_size}px minimum")
                    if blocked_by_resolution:
                        reasons.append("existing cover has equal or higher resolution")
                    reason = "; ".join(reasons)
                    if approval == "declined":
                        reason += "; approval declined"
                    else:
                        options = []
                        if blocked_by_minimum:
                            options.append("--allow-small")
                        if blocked_by_resolution:
                            options.append("--force")
                        reason += f"; rerun interactively or use {' and '.join(options)}"
                if apply and eligible:
                    try:
                        assert backup_run is not None
                        backup = (
                            root
                            / ".muse"
                            / "art-backups"
                            / backup_run
                            / track.path.relative_to(root)
                        )
                        upgrade(track.path, art, backup)
                        status = "updated"
                    except Exception as error:
                        errors.append(f"{track.path}: {error}")
                        status = "error"
                files.append(
                    {
                        "path": str(track.path.relative_to(root)),
                        "existing": f"{existing.width}x{existing.height}" if existing else None,
                        "status": status,
                        "reason": reason,
                        "backup": str(backup.relative_to(root)) if status == "updated" else None,
                    }
                )
            results.append(
                {
                    "artist": artist,
                    "album": album,
                    "source": url,
                    "matched_title": matched_title,
                    "artwork": f"{art.width}x{art.height}",
                    "files": files,
                }
            )
        except ArtworkUnavailable as error:
            skipped.append({"artist": artist, "album": album, "reason": str(error)})
        except Exception as error:
            errors.append(f"{artist} — {album}: {error}")
    return {
        "albums": results,
        "skipped": skipped,
        "errors": errors,
        "files": len(tracks),
        "applied": apply,
    }
