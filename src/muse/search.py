"""Read-only filename search across vault content areas."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from muse.config import CONTENT_AREAS


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

    pending = [root / area for area in CONTENT_AREAS if (root / area).is_dir()]
    while pending:
        directory = pending.pop()
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    path = Path(entry.path)
                    try:
                        is_symlink = entry.is_symlink()
                        is_directory = entry.is_dir(follow_symlinks=False)
                        if is_directory:
                            pending.append(path)

                        relative_path = path.relative_to(root).as_posix()
                        name = entry.name.casefold()
                        if not all(term in name for term in included_terms) or any(
                            term in relative_path.casefold() for term in excluded_terms
                        ):
                            continue
                        if is_symlink:
                            kind = "symlink"
                        elif is_directory:
                            kind = "directory"
                        elif entry.is_file(follow_symlinks=False):
                            kind = "file"
                        else:
                            kind = "other"
                        matches.append(SearchMatch(relative_path, kind))
                    except OSError as error:
                        errors.append(SearchError(str(path), str(error)))
        except OSError as error:
            errors.append(SearchError(str(directory), str(error)))

    matches.sort(key=lambda match: (match.path.casefold(), match.path))
    errors.sort(key=lambda error: error.path)
    return matches, errors
