"""Read-only filename search across vault content areas."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from muse.config import CONTENT_AREAS
from muse.filesystem import WalkError, walk


@dataclass(frozen=True)
class SearchMatch:
    path: str
    kind: str

    def to_dict(self) -> dict[str, str]:
        return {"path": self.path, "type": self.kind}


@dataclass(frozen=True)
class SearchError:
    path: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {"path": self.path, "message": self.message}


def search_vault(root: Path, terms: list[str]) -> tuple[list[SearchMatch], list[SearchError]]:
    """Find names containing positive terms, excluding paths containing ``-term``."""
    exclusions = [
        term
        for term in terms
        if len(term) > 1 and term.startswith("-") and not term.startswith("--")
    ]
    included_terms = [term.casefold() for term in terms if term not in exclusions]
    excluded_terms = [term[1:].casefold() for term in exclusions]
    matches: list[SearchMatch] = []
    errors: list[SearchError] = []

    if not root.is_dir():
        return [], [SearchError(str(root), "library root does not exist or is not a directory")]

    for area in CONTENT_AREAS:
        area_path = root / area
        if not area_path.is_dir():
            continue
        for item in walk(area_path):
            if isinstance(item, WalkError):
                errors.append(SearchError(str(item.path), item.message))
                continue
            if item.path == area_path:
                continue
            relative_path = item.path.relative_to(root).as_posix()
            name = item.path.name.casefold()
            if all(term in name for term in included_terms) and not any(
                term in relative_path.casefold() for term in excluded_terms
            ):
                matches.append(SearchMatch(relative_path, item.kind))

    matches.sort(key=lambda match: (match.path.casefold(), match.path))
    errors.sort(key=lambda error: error.path)
    return matches, errors
