"""Copy non-audio backlog artifacts into provenance-preserving slag."""

from __future__ import annotations

import hashlib
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from muse.repository import AUDIO_EXTENSIONS, scan_path

AUDIO_ADJACENT_EXTENSIONS = frozenset(
    {
        ".bmp",
        ".cue",
        ".gif",
        ".jpeg",
        ".jpg",
        ".log",
        ".m3u",
        ".m3u8",
        ".md5",
        ".nfo",
        ".pdf",
        ".pls",
        ".png",
        ".sfv",
        ".sha1",
        ".sha256",
        ".sha512",
        ".tif",
        ".tiff",
        ".txt",
        ".webp",
    }
)


@dataclass(frozen=True)
class SlagCopy:
    source: Path
    destination: Path
    size: int


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(4 * 1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def candidates(root: Path, sources: list[Path], *, thorough: bool = False) -> list[SlagCopy]:
    """List non-audio files beneath backlog sources and their slag destinations."""
    backlog = root / "backlog"
    result = []
    for source in sources:
        source = source.absolute()
        try:
            source.relative_to(backlog)
        except ValueError as error:
            raise ValueError("--from paths must be beneath backlog/") from error
        paths = (
            [source]
            if source.is_file()
            else sorted(path for path in source.rglob("*") if path.is_file())
        )
        for path in paths:
            if path.is_symlink() or path.suffix.lower() in AUDIO_EXTENSIONS:
                continue
            if not thorough and path.suffix.lower() in AUDIO_ADJACENT_EXTENSIONS:
                continue
            destination = root / "slag" / path.relative_to(backlog)
            result.append(SlagCopy(path, destination, path.stat().st_size))
    return result


def apply(
    copies: list[SlagCopy], progress: Callable[[int, int], None] | None = None
) -> tuple[int, int]:
    """Move candidates after a verified copy, preserving exact existing destinations."""
    moved = skipped = completed_bytes = 0
    for item in copies:
        item.destination.parent.mkdir(parents=True, exist_ok=True)
        if item.destination.exists():
            if _sha256(item.source) != _sha256(item.destination):
                raise ValueError(f"destination differs: {item.destination}")
            item.source.unlink()
            skipped += 1
            completed_bytes += item.size
            if progress:
                progress(completed_bytes, item.size)
            continue
        shutil.copy2(item.source, item.destination)
        if _sha256(item.source) != _sha256(item.destination):
            item.destination.unlink(missing_ok=True)
            raise ValueError(f"move verification failed: {item.source}")
        item.source.unlink()
        moved += 1
        completed_bytes += item.size
        if progress:
            progress(completed_bytes, item.size)
    return moved, skipped


def inventory(root: Path) -> list[SlagCopy]:
    """List files already preserved in slag."""
    slag = root / "slag"
    if not slag.exists():
        return []
    return [
        SlagCopy(path, path, path.stat().st_size)
        for path in sorted(slag.rglob("*"))
        if path.is_file() and not path.is_symlink()
    ]


def stats(root: Path):
    return scan_path(root / "slag")
