"""Read-only album inventory for metadata review."""

from __future__ import annotations

import argparse
import math
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from mutagen import File as AudioFile
from mutagen import MutagenError
from mutagen.flac import FLAC
from mutagen.id3 import ID3
from mutagen.mp4 import MP4
from rich import box
from rich.markup import escape
from rich.table import Table

from muse.config import resolve_target
from muse.filesystem import Entry, WalkError, walk
from muse.reporting import emit_json, human_duration, make_console
from muse.repository import AUDIO_EXTENSIONS

_ID3_NAMES = {
    "TIT2": "title",
    "TPE1": "artist",
    "TALB": "album",
    "TPE2": "album_artist",
    "TRCK": "track_number",
    "TPOS": "disc_number",
    "TDRC": "date",
    "TYER": "date",
    "TCON": "genre",
    "TCOM": "composer",
    "TPUB": "publisher",
    "TSRC": "isrc",
    "TCMP": "compilation",
    "TBPM": "bpm",
    "TCOP": "copyright",
    "TIT1": "grouping",
    "TIT3": "subtitle",
    "TPE3": "conductor",
    "TPE4": "remixer",
    "TEXT": "lyricist",
}
_MP4_NAMES = {
    "\xa9nam": "title",
    "\xa9ART": "artist",
    "\xa9alb": "album",
    "aART": "album_artist",
    "trkn": "track_number",
    "disk": "disc_number",
    "\xa9day": "date",
    "\xa9gen": "genre",
    "\xa9wrt": "composer",
    "\xa9cmt": "comment",
    "cpil": "compilation",
    "tmpo": "bpm",
    "cprt": "copyright",
    "\xa9grp": "grouping",
    "\xa9too": "encoder",
    "----:com.apple.iTunes:ISRC": "isrc",
}
_ALIASES = {
    "albumartist": "album_artist",
    "album_artist": "album_artist",
    "track": "track_number",
    "tracknumber": "track_number",
    "disc": "disc_number",
    "discnumber": "disc_number",
    "year": "date",
    "description": "comment",
    "musicbrainz_albumid": "musicbrainz_release_id",
    "musicbrainz_album_id": "musicbrainz_release_id",
    "musicbrainz_releaseid": "musicbrainz_release_id",
    "musicbrainz_release_id": "musicbrainz_release_id",
    "musicbrainz_releasegroupid": "musicbrainz_release_group_id",
    "musicbrainz_release_group_id": "musicbrainz_release_group_id",
    "musicbrainz_trackid": "musicbrainz_recording_id",
    "musicbrainz_track_id": "musicbrainz_recording_id",
    "musicbrainz_releasetrackid": "musicbrainz_release_track_id",
    "musicbrainz_release_track_id": "musicbrainz_release_track_id",
}
_KEY_FIELDS = (
    "title",
    "artist",
    "album",
    "album_artist",
    "track_number",
    "disc_number",
    "date",
    "genre",
    "composer",
    "comment",
    "musicbrainz_release_id",
    "musicbrainz_release_group_id",
    "musicbrainz_recording_id",
    "isrc",
)


def _values(value: Any) -> list[str]:
    if hasattr(value, "text"):
        value = value.text
    if isinstance(value, tuple) and all(isinstance(part, int) for part in value):
        return ["/".join(str(part) for part in value if part)]
    if isinstance(value, (list, tuple)):
        return [text for item in value for text in _values(item)]
    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8")
        except UnicodeDecodeError:
            return []
    if not isinstance(value, (str, int, float)):
        return []
    text = str(value).strip()
    return [text] if text else []


def _tags(audio: Any) -> dict[str, list[str]]:
    tags = audio.tags
    if tags is None:
        return {}
    result: dict[str, list[str]] = {}
    if isinstance(tags, ID3):
        for frame in tags.values():
            if frame.FrameID == "TXXX":
                key = frame.desc.strip().lower().replace(" ", "_")
            elif frame.FrameID == "COMM":
                key = "comment"
            else:
                key = _ID3_NAMES.get(frame.FrameID, frame.FrameID.lower())
            values = _values(frame)
            if values:
                result.setdefault(_ALIASES.get(key, key), []).extend(values)
    else:
        for raw_key, value in tags.items():
            key = str(raw_key)
            if key in {"covr", "metadata_block_picture", "coverart", "coverartmime"}:
                continue
            if isinstance(audio, MP4):
                key = _MP4_NAMES.get(key, key.split(":")[-1] if key.startswith("----:") else key)
            key = key.strip().lower().replace(" ", "_")
            values = _values(value)
            if values:
                result.setdefault(_ALIASES.get(key, key), []).extend(values)
    return dict(sorted(result.items()))


def _artwork(audio: Any) -> str:
    tags = audio.tags
    if isinstance(audio, FLAC):
        pictures = audio.pictures
        return f"embedded ({len(pictures)})" if pictures else "missing"
    if isinstance(tags, ID3):
        pictures = tags.getall("APIC")
        return f"embedded ({len(pictures)})" if pictures else "missing"
    if isinstance(audio, MP4):
        pictures = tags.get("covr", []) if tags else []
        return f"embedded ({len(pictures)})" if pictures else "missing"
    if tags:
        pictures = tags.get("metadata_block_picture", []) or tags.get("coverart", [])
        # Malformed picture blocks still count as present; this is a metadata inventory.
        return f"embedded ({len(pictures)})" if pictures else "missing"
    return "missing"


def _number(value: str | None) -> tuple[int | None, int | None]:
    if not value:
        return None, None
    parts = value.split("/", 1)
    try:
        number = int(parts[0].strip())
        total = int(parts[1].strip()) if len(parts) == 2 and parts[1].strip() else None
    except ValueError:
        return None, None
    return (number if number > 0 else None), (total if total and total > 0 else None)


def _inspect(path: Path, target: Path) -> dict[str, Any]:
    audio = AudioFile(path)
    if audio is None or not hasattr(audio, "info"):
        raise ValueError("unrecognized audio format")
    tags = _tags(audio)
    fields = {key: (tags.get(key) or [None])[0] for key in _KEY_FIELDS}
    duration = float(getattr(audio.info, "length", 0) or 0)
    if not math.isfinite(duration) or duration <= 0:
        duration = 0.0
    track, track_total = _number(fields["track_number"])
    disc, disc_total = _number(fields["disc_number"])
    missing = [
        key
        for key in (
            "title",
            "artist",
            "album",
            "album_artist",
            "track_number",
            "disc_number",
            "date",
            "genre",
        )
        if not fields[key]
    ]
    if track_total is None:
        missing.append("track total")
    if disc_total is None:
        missing.append("disc total")
    artwork = _artwork(audio)
    if artwork == "missing":
        missing.append("artwork")
    if duration == 0:
        missing.append("duration")
    return {
        "path": str(path.relative_to(target)) if target.is_dir() else path.name,
        "fields": fields,
        "tags": tags,
        "duration_seconds": duration,
        "format": type(audio).__name__,
        "bitrate": getattr(audio.info, "bitrate", None),
        "sample_rate": getattr(audio.info, "sample_rate", None),
        "channels": getattr(audio.info, "channels", None),
        "bit_depth": getattr(audio.info, "bits_per_sample", None),
        "size_bytes": path.stat().st_size,
        "artwork": artwork,
        "track": track,
        "track_total": track_total,
        "disc": disc,
        "disc_total": disc_total,
        "missing": missing,
    }


def _album_findings(tracks: list[dict[str, Any]]) -> list[str]:
    findings: list[str] = []
    for field in ("album", "album_artist", "date", "musicbrainz_release_id"):
        values = {track["fields"][field] for track in tracks if track["fields"][field]}
        if len(values) > 1:
            findings.append(f"inconsistent {field.replace('_', ' ')}: {', '.join(sorted(values))}")
    by_disc: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for track in tracks:
        if track["track"] is None:
            if track["fields"]["track_number"]:
                findings.append(f"{track['path']}: invalid track number")
            continue
        if track["disc"] is None and track["fields"]["disc_number"]:
            findings.append(f"{track['path']}: invalid disc number")
        by_disc[track["disc"] or 1].append(track)
    for disc, members in sorted(by_disc.items()):
        counts = Counter(track["track"] for track in members)
        duplicates = sorted(number for number, count in counts.items() if count > 1)
        if duplicates:
            findings.append(
                f"disc {disc}: duplicate track numbers {', '.join(map(str, duplicates))}"
            )
        totals = {track["track_total"] for track in members if track["track_total"]}
        if len(totals) > 1:
            findings.append(
                f"disc {disc}: inconsistent track totals {', '.join(map(str, sorted(totals)))}"
            )
        expected = max(totals, default=max(counts))
        gaps = sorted(set(range(1, expected + 1)) - counts.keys())
        if gaps:
            findings.append(f"disc {disc}: missing track numbers {', '.join(map(str, gaps))}")
    disc_totals = {track["disc_total"] for track in tracks if track["disc_total"]}
    if len(disc_totals) > 1:
        findings.append(f"inconsistent disc totals: {', '.join(map(str, sorted(disc_totals)))}")
    if disc_totals:
        expected_discs = max(disc_totals)
        gaps = sorted(set(range(1, expected_discs + 1)) - by_disc.keys())
        if gaps:
            findings.append(f"missing disc numbers {', '.join(map(str, gaps))}")
    return findings


def album_info(target: Path) -> dict[str, Any]:
    tracks: list[dict[str, Any]] = []
    other_files: list[str] = []
    errors: list[dict[str, str]] = []
    for item in walk(target, sort=True):
        if isinstance(item, WalkError):
            errors.append({"path": str(item.path), "message": item.message})
        elif item.kind == "file" and item.path.suffix.lower() in AUDIO_EXTENSIONS:
            try:
                tracks.append(_inspect(item.path, target))
            except (OSError, ValueError, TypeError, MutagenError) as error:
                errors.append({"path": str(item.path), "message": str(error)})
        elif isinstance(item, Entry) and item.kind == "file":
            other_files.append(
                str(item.path.relative_to(target)) if target.is_dir() else item.path.name
            )
        elif isinstance(item, Entry) and item.kind == "symlink":
            errors.append({"path": str(item.path), "message": "symlink skipped"})
    tracks.sort(key=lambda track: (track["disc"] or 1, track["track"] or 9999, track["path"]))
    findings = _album_findings(tracks)
    if not tracks:
        findings.append("no readable audio tracks found")

    def shared(field: str) -> str | None:
        values = {track["fields"][field] for track in tracks if track["fields"][field]}
        return next(iter(values)) if len(values) == 1 else None

    return {
        "target": str(target),
        "album": shared("album"),
        "album_artist": shared("album_artist"),
        "date": shared("date"),
        "track_count": len(tracks),
        "duration_seconds": sum(track["duration_seconds"] for track in tracks),
        "formats": dict(sorted(Counter(track["format"] for track in tracks).items())),
        "missing_counts": dict(
            sorted(Counter(field for track in tracks for field in track["missing"]).items())
        ),
        "tracks": tracks,
        "other_files": sorted(other_files),
        "findings": findings,
        "errors": errors,
        "state_created": False,
    }


def _display(value: Any) -> str:
    return str(value) if value is not None else "—"


def _info(args: argparse.Namespace, root: Path) -> int:
    target = resolve_target(root, args.target)
    report = album_info(target)
    if args.json:
        emit_json(report)
    else:
        console = make_console(args.color)
        console.print("[bold]Album info[/bold]", str(target))
        if report["album"] or report["album_artist"]:
            console.print(
                f"[bold]{escape(report['album_artist'] or 'Unknown artist')} — "
                f"{escape(report['album'] or 'Unknown album')}[/bold]"
            )
        console.print(
            f"{report['track_count']} tracks · {human_duration(report['duration_seconds'])}"
        )
        if report["missing_counts"]:
            summary = ", ".join(
                f"{name}: {count}" for name, count in report["missing_counts"].items()
            )
            console.print(f"[yellow]Missing:[/yellow] {summary}")
        table = Table(box=box.SIMPLE_HEAD, show_edge=False, pad_edge=False)
        for header in ("NO.", "FILE", "TITLE", "ARTIST", "LENGTH", "MISSING"):
            table.add_column(header, style="bold dim", no_wrap=True, overflow="ellipsis")
        for track in report["tracks"]:
            fields = track["fields"]
            table.add_row(
                f"{_display(track['disc'])}/{_display(track['track'])}",
                escape(track["path"]),
                escape(_display(fields["title"])),
                escape(_display(fields["artist"])),
                human_duration(track["duration_seconds"]),
                ", ".join(track["missing"]) or "—",
            )
        if report["tracks"]:
            console.print(table)
        if args.all:
            for track in report["tracks"]:
                console.print(f"\n[bold]{escape(track['path'])}[/bold]")
                technical = (
                    f"{track['format']} · {human_duration(track['duration_seconds'])} · "
                    f"{_display(track['sample_rate'])} Hz · {_display(track['channels'])} ch · "
                    f"{_display(track['bit_depth'])} bit · "
                    f"{round(track['bitrate'] / 1000) if track['bitrate'] else '—'} kbps · "
                    f"{track['artwork']}"
                )
                console.print(f"  [dim]{technical}[/dim]")
                for key in _KEY_FIELDS:
                    label = key.replace("_", " ").title()
                    console.print(f"  {label}: {escape(_display(track['fields'][key]))}")
                for key, values in track["tags"].items():
                    if key not in _KEY_FIELDS:
                        label = escape(key.replace("_", " ").title())
                        console.print(f"  {label}: {escape(', '.join(values))}")
        if report["other_files"]:
            console.print("\n[bold]Other files[/bold]", escape(", ".join(report["other_files"])))
        if report["findings"] or report["errors"]:
            console.print("\n[bold]Review[/bold]")
            for finding in report["findings"]:
                console.print(f"  [yellow]•[/yellow] {escape(finding)}")
            for error in report["errors"]:
                console.print(f"  [red]•[/red] {escape(error['path'])}: {escape(error['message'])}")
    return 1 if report["errors"] or not report["tracks"] else 0
