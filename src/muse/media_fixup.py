"""Conservative, lossless metadata and artwork repairs used while preparing imports."""

from __future__ import annotations

import base64
import os
import re
import stat
from pathlib import Path

from mutagen.flac import FLAC, Picture
from mutagen.id3 import APIC, ID3, TPE2, TPOS, TRCK, ID3NoHeaderError
from mutagen.mp4 import MP4, MP4Cover
from mutagen.oggopus import OggOpus
from mutagen.oggvorbis import OggVorbis
from mutagen.wave import WAVE

_TRACK_PREFIX = re.compile(r"^\s*(\d{1,3})(?:\s*[. _-]|$)")
_COVER_STEMS = frozenset({"cover", "folder", "front", "album", "albumart"})
_COVER_SUFFIXES = frozenset({".jpg", ".jpeg", ".png"})


def is_obvious_cover(path: Path) -> bool:
    """Return whether a file has a conventional unambiguous front-cover name."""
    return path.stem.lower() in _COVER_STEMS and path.suffix.lower() in _COVER_SUFFIXES


def _image(path: Path) -> tuple[bytes, str]:
    data = path.read_bytes()
    if data.startswith(b"\xff\xd8\xff"):
        return data, "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return data, "image/png"
    raise ValueError(f"cover image is not a valid JPEG or PNG: {path.name}")


def _text(tags: object, *keys: str) -> str | None:
    for key in keys:
        value = tags.get(key)  # type: ignore[attr-defined]
        if value:
            if isinstance(value, list):
                value = value[0]
            text = str(value).strip()
            if text:
                return text
    return None


def repair_tags(path: Path) -> tuple[str, ...]:
    """Fill only deterministic missing tags, without transcoding audio."""
    suffix = path.suffix.lower()
    changes: list[str] = []
    original_mode = path.stat().st_mode
    if not original_mode & stat.S_IWUSR:
        path.chmod(original_mode | stat.S_IWUSR)
    try:
        if suffix == ".mp3":
            try:
                tags = ID3(path)
            except ID3NoHeaderError:
                tags = ID3()
            artist = _text(tags, "TPE1")
            if not _text(tags, "TPE2") and artist:
                tags.add(TPE2(encoding=3, text=[artist]))
                changes.append(f"{path.name}: album artist = {artist}")
            match = _TRACK_PREFIX.match(path.name)
            if not _text(tags, "TRCK") and match:
                tags.add(TRCK(encoding=3, text=[str(int(match.group(1)))]))
                changes.append(f"{path.name}: track = {int(match.group(1))}")
            if not _text(tags, "TPOS"):
                tags.add(TPOS(encoding=3, text=["1/1"]))
                changes.append(f"{path.name}: disc = 1/1")
            if changes:
                tags.save(path)
            return tuple(changes)

        audio = (
            MP4(path)
            if suffix == ".m4a"
            else (
                FLAC(path)
                if suffix == ".flac"
                else (
                    OggOpus(path)
                    if suffix == ".opus"
                    else (OggVorbis(path) if suffix in {".ogg", ".oga"} else WAVE(path))
                )
            )
        )
        if suffix == ".wav":
            if audio.tags is None:
                audio.add_tags()
            tags = audio.tags
            assert tags is not None
            artist = _text(tags, "TPE1")
            if not _text(tags, "TPE2") and artist:
                tags.add(TPE2(encoding=3, text=[artist]))
                changes.append(f"{path.name}: album artist = {artist}")
            match = _TRACK_PREFIX.match(path.name)
            if not _text(tags, "TRCK") and match:
                tags.add(TRCK(encoding=3, text=[str(int(match.group(1)))]))
                changes.append(f"{path.name}: track = {int(match.group(1))}")
            if not _text(tags, "TPOS"):
                tags.add(TPOS(encoding=3, text=["1/1"]))
                changes.append(f"{path.name}: disc = 1/1")
        elif suffix == ".m4a":
            tags = audio.tags
            if tags is None:
                audio.add_tags()
                tags = audio.tags
            assert tags is not None
            artist = _text(tags, "\xa9ART")
            if not _text(tags, "aART") and artist:
                tags["aART"] = [artist]
                changes.append(f"{path.name}: album artist = {artist}")
            match = _TRACK_PREFIX.match(path.name)
            if not tags.get("trkn") and match:
                tags["trkn"] = [(int(match.group(1)), 0)]
                changes.append(f"{path.name}: track = {int(match.group(1))}")
            if not tags.get("disk"):
                tags["disk"] = [(1, 1)]
                changes.append(f"{path.name}: disc = 1/1")
        else:
            tags = audio.tags
            if tags is None:
                audio.add_tags()
                tags = audio.tags
            assert tags is not None
            artist = _text(tags, "artist")
            if not _text(tags, "albumartist", "album_artist") and artist:
                tags["albumartist"] = [artist]
                changes.append(f"{path.name}: album artist = {artist}")
            match = _TRACK_PREFIX.match(path.name)
            if not _text(tags, "tracknumber", "track") and match:
                tags["tracknumber"] = [str(int(match.group(1)))]
                changes.append(f"{path.name}: track = {int(match.group(1))}")
            if not _text(tags, "discnumber", "disc"):
                tags["discnumber"] = ["1/1"]
                changes.append(f"{path.name}: disc = 1/1")
        if changes:
            audio.save()
        return tuple(changes)
    finally:
        if not original_mode & stat.S_IWUSR:
            os.chmod(path, stat.S_IMODE(original_mode))


def embed_cover(path: Path, cover: Path) -> bool:
    """Embed a front cover if the audio file does not already contain artwork."""
    data, mime = _image(cover)
    suffix = path.suffix.lower()
    original_mode = path.stat().st_mode
    if not original_mode & stat.S_IWUSR:
        path.chmod(original_mode | stat.S_IWUSR)
    try:
        if suffix == ".mp3":
            try:
                tags = ID3(path)
            except ID3NoHeaderError:
                tags = ID3()
            if tags.getall("APIC"):
                return False
            tags.add(APIC(encoding=3, mime=mime, type=3, desc="Cover", data=data))
            tags.save(path)
        elif suffix == ".flac":
            audio = FLAC(path)
            if audio.pictures:
                return False
            picture = Picture()
            picture.type, picture.mime, picture.desc, picture.data = 3, mime, "Cover", data
            audio.add_picture(picture)
            audio.save()
        elif suffix == ".m4a":
            audio = MP4(path)
            if audio.tags is None:
                audio.add_tags()
            assert audio.tags is not None
            if audio.tags.get("covr"):
                return False
            image_format = MP4Cover.FORMAT_PNG if mime == "image/png" else MP4Cover.FORMAT_JPEG
            audio.tags["covr"] = [MP4Cover(data, imageformat=image_format)]
            audio.save()
        elif suffix in {".ogg", ".oga", ".opus"}:
            audio = OggOpus(path) if suffix == ".opus" else OggVorbis(path)
            if audio.get("metadata_block_picture"):
                return False
            picture = Picture()
            picture.type, picture.mime, picture.desc, picture.data = 3, mime, "Cover", data
            audio["metadata_block_picture"] = [base64.b64encode(picture.write()).decode("ascii")]
            audio.save()
        elif suffix == ".wav":
            audio = WAVE(path)
            if audio.tags is None:
                audio.add_tags()
            assert audio.tags is not None
            if audio.tags.getall("APIC"):
                return False
            audio.tags.add(APIC(encoding=3, mime=mime, type=3, desc="Cover", data=data))
            audio.save()
        else:
            return False
        return True
    finally:
        if not original_mode & stat.S_IWUSR:
            os.chmod(path, stat.S_IMODE(original_mode))
