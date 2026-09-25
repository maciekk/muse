"""Static repository layout and path resolution."""

from __future__ import annotations

from pathlib import Path

DEFAULT_ROOT = Path.home() / "music-vault"
MANAGED_AREAS = ("master", "stopgap", "incoming", "backlog", "duplicates", ".muse")
MASTER_SHELVES = ("artists", "games", "movies-tv", "classical", "compilations", "misc")


def resolve_root(value: str | Path | None) -> Path:
    """Resolve a CLI root without requiring that it already exist."""
    root = DEFAULT_ROOT if value is None else Path(value).expanduser()
    return root.absolute()


def resolve_target(root: Path, value: str | Path | None) -> Path:
    """Resolve a stats target; relative targets are interpreted beneath the root."""
    if value is None:
        return root
    target = Path(value).expanduser()
    return (root / target).absolute() if not target.is_absolute() else target.absolute()
