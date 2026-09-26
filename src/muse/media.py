"""Strict, read-only inspection of audio accepted by import."""

from __future__ import annotations

import json
import math
import shutil
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class MediaInfo:
    container: str
    codec: str
    duration_seconds: float
    sample_rate: int
    channels: int
    bit_depth: int | None
    title: str
    artist: str
    album: str
    album_artist: str
    track_number: int
    track_total: int | None
    disc_number: int
    disc_total: int | None
    year: str | None
    release_id: str | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class MediaInspectionError(ValueError):
    """One or more independently useful readiness failures for a file."""

    def __init__(self, errors: list[str]) -> None:
        self.errors = tuple(errors)
        super().__init__("; ".join(errors))


def _run(command: list[str]) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(command, capture_output=True, check=False)
    except OSError as error:
        raise MediaInspectionError([f"cannot run {command[0]}: {error}"]) from error


def _value(tags: dict[str, str], *names: str) -> str | None:
    for name in names:
        value = tags.get(name)
        if value is not None and value.strip():
            return value.strip()
    return None


def _number(value: str | None, label: str, errors: list[str]) -> tuple[int, int | None]:
    if value is None:
        errors.append(f"missing {label} number")
        return 0, None
    pieces = value.strip().split("/", 1)
    try:
        number = int(pieces[0].strip())
        total = int(pieces[1].strip()) if len(pieces) == 2 and pieces[1].strip() else None
    except ValueError:
        errors.append(f"invalid {label} number {value!r}")
        return 0, None
    maximum = 99 if label == "disc" else 999
    if number < 1 or number > maximum:
        errors.append(f"implausible {label} number {value!r}")
    if total is not None and (total < number or total > maximum):
        errors.append(f"invalid {label} total {value!r}")
    return number, total


def _container_and_codec(path: Path, format_name: str, codec: str, errors: list[str]) -> str:
    formats = set(format_name.split(","))
    extension = path.suffix.lower()
    container = ""
    valid = False
    if formats == {"flac"} or "flac" in formats:
        container, valid = "flac", extension == ".flac" and codec == "flac"
    elif "mp3" in formats:
        container, valid = "mp3", extension == ".mp3" and codec == "mp3"
    elif "ogg" in formats:
        container = "ogg"
        valid = codec in {"vorbis", "opus"} and (
            extension in {".ogg", ".oga"} or (extension == ".opus" and codec == "opus")
        )
    elif formats & {"mov", "mp4", "m4a"}:
        container = "m4a"
        valid = extension == ".m4a" and codec in {"aac", "alac"}
    elif "wav" in formats:
        container = "wav"
        pcm_codecs = {
            "pcm_u8",
            "pcm_s16le",
            "pcm_s24le",
            "pcm_s32le",
            "pcm_f32le",
            "pcm_f64le",
        }
        valid = extension == ".wav" and codec in pcm_codecs
    if not container:
        description = f"{format_name or 'unknown'} with codec {codec or 'unknown'}"
        errors.append(f"unsupported container {description}")
    elif not valid:
        errors.append(
            f"extension/container/codec mismatch: {extension or '(none)'}, {container}, {codec}"
        )
    return container or format_name


def inspect_media(path: Path) -> MediaInfo:
    """Probe, fully decode, and validate one supported audio file and its tags."""
    probe = _run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_error",
            "-show_format",
            "-show_streams",
            "-of",
            "json",
            str(path),
        ]
    )
    if probe.returncode:
        detail = probe.stderr.decode(errors="replace").strip().splitlines()
        message = detail[-1] if detail else "unknown error"
        raise MediaInspectionError([f"ffprobe could not read media: {message}"])
    try:
        data = json.loads(probe.stdout)
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise MediaInspectionError([f"ffprobe returned invalid output: {error}"]) from error

    errors: list[str] = []
    streams = [stream for stream in data.get("streams", []) if stream.get("codec_type") == "audio"]
    if len(streams) != 1:
        raise MediaInspectionError([f"expected exactly one audio stream, found {len(streams)}"])
    stream = streams[0]
    codec = str(stream.get("codec_name", ""))
    format_data = data.get("format", {})
    container = _container_and_codec(path, str(format_data.get("format_name", "")), codec, errors)

    try:
        duration = float(stream.get("duration") or format_data.get("duration"))
        if not math.isfinite(duration) or duration <= 0:
            raise ValueError
    except (TypeError, ValueError):
        duration = 0.0
        errors.append("missing or invalid duration")
    try:
        sample_rate = int(stream.get("sample_rate"))
        if sample_rate <= 0:
            raise ValueError
    except (TypeError, ValueError):
        sample_rate = 0
        errors.append("missing or invalid sample rate")
    try:
        channels = int(stream.get("channels"))
        if channels <= 0:
            raise ValueError
    except (TypeError, ValueError):
        channels = 0
        errors.append("missing or invalid channel count")

    raw_depth = stream.get("bits_per_raw_sample") or stream.get("bits_per_sample")
    try:
        bit_depth = int(raw_depth) if raw_depth and int(raw_depth) > 0 else None
    except (TypeError, ValueError):
        bit_depth = None
        errors.append(f"invalid bit depth {raw_depth!r}")

    tags: dict[str, str] = {}
    for source in (format_data.get("tags", {}), stream.get("tags", {})):
        tags.update(
            {str(key).lower().replace(" ", "_"): str(value) for key, value in source.items()}
        )
    required = {
        "title": _value(tags, "title"),
        "artist": _value(tags, "artist"),
        "album": _value(tags, "album"),
        "album_artist": _value(tags, "album_artist", "albumartist"),
    }
    for name, value in required.items():
        if value is None:
            errors.append(f"missing {name.replace('_', ' ')} tag")
    track, track_total = _number(_value(tags, "track", "tracknumber"), "track", errors)
    disc, disc_total = _number(_value(tags, "disc", "discnumber"), "disc", errors)

    decode = _run(
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-xerror",
            "-i",
            str(path),
            "-map",
            "0:a:0",
            "-f",
            "null",
            "-",
        ]
    )
    if decode.returncode:
        detail = decode.stderr.decode(errors="replace").strip().splitlines()
        errors.append(f"complete decode failed: {detail[-1] if detail else 'unknown error'}")

    # The reference FLAC decoder verifies the stream MD5, which is stronger than a
    # successful ffmpeg decode.  Use it when installed, but do not make it a second dependency.
    flac = shutil.which("flac")
    if container == "flac" and codec == "flac" and flac:
        check = _run([flac, "--test", "--silent", "--", str(path)])
        if check.returncode:
            detail = check.stderr.decode(errors="replace").strip().splitlines()
            message = detail[-1] if detail else "invalid stream"
            errors.append(f"FLAC integrity check failed: {message}")

    if errors:
        raise MediaInspectionError(errors)
    return MediaInfo(
        container=container,
        codec=codec,
        duration_seconds=duration,
        sample_rate=sample_rate,
        channels=channels,
        bit_depth=bit_depth,
        title=required["title"] or "",
        artist=required["artist"] or "",
        album=required["album"] or "",
        album_artist=required["album_artist"] or "",
        track_number=track,
        track_total=track_total,
        disc_number=disc,
        disc_total=disc_total,
        year=_value(tags, "date", "year"),
        release_id=_value(
            tags,
            "musicbrainz_albumid",
            "musicbrainz_releaseid",
            "musicbrainz_releasegroupid",
        ),
    )
