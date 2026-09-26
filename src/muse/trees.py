"""Recursive fingerprints of directory names and file identities."""

from __future__ import annotations

import hashlib
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class TreeFingerprint:
    sha256: str
    files: int
    logical_bytes: int


def fingerprints(
    directories: set[str], files: dict[str, tuple[str, int]]
) -> dict[str, TreeFingerprint]:
    """Return a fingerprint for every relative directory, including the root."""
    children: dict[Path, list[tuple[str, str, str]]] = defaultdict(list)
    for directory in directories | {"."}:
        children[Path(directory)]
    for relative, (identity, _size) in files.items():
        path = Path(relative)
        children[path.parent].append((path.name, "file", identity))

    results: dict[Path, TreeFingerprint] = {}
    for directory in sorted(children, key=lambda path: len(path.parts), reverse=True):
        digest = hashlib.sha256()
        file_count = logical_bytes = 0
        for name, kind, value in sorted(children[directory]):
            digest.update(f"{kind}\0{name}\0{value}\n".encode())
            if kind == "file":
                file_count += 1
                logical_bytes += files[str(directory / name)][1]
            else:
                child = results[directory / name]
                file_count += child.files
                logical_bytes += child.logical_bytes
        result = TreeFingerprint(digest.hexdigest(), file_count, logical_bytes)
        results[directory] = result
        if directory != Path("."):
            children[directory.parent].append((directory.name, "directory", result.sha256))
    return {str(path): result for path, result in results.items()}
