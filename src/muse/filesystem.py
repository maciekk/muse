"""Streaming, non-following inventory of filesystem entries."""

from __future__ import annotations

import os
import stat
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Entry:
    path: Path
    kind: str
    stat: os.stat_result | None = None


@dataclass(frozen=True)
class WalkError:
    path: Path
    message: str


def walk(
    root: Path,
    *,
    exclude: Callable[[Path], bool] | None = None,
    sort: bool = False,
    follow_root: bool = False,
) -> Iterator[Entry | WalkError]:
    """Yield the starting entry and descendants; never follow directory links.

    Exclusion applies to descendants before they are reported or traversed.
    Errors are yielded in place so callers can keep their own reporting policy.
    """
    try:
        root_stat = root.stat() if follow_root else root.lstat()
    except FileNotFoundError:
        yield WalkError(root, "path does not exist")
        return
    except OSError as error:
        yield WalkError(root, str(error))
        return

    def kind_of(mode: int) -> str:
        if stat.S_ISLNK(mode):
            return "symlink"
        if stat.S_ISDIR(mode):
            return "directory"
        if stat.S_ISREG(mode):
            return "file"
        return "other"

    root_kind = kind_of(root_stat.st_mode)
    yield Entry(root, root_kind, root_stat if root_kind == "file" else None)
    if root_kind != "directory":
        return

    pending = [root]
    while pending:
        directory = pending.pop()
        try:
            with os.scandir(directory) as iterator:
                children = list(iterator)
            if sort:
                children.sort(key=lambda child: child.name, reverse=True)
        except OSError as error:
            yield WalkError(directory, str(error))
            continue
        for child in children:
            path = Path(child.path)
            if exclude is not None and exclude(path):
                continue
            try:
                child_stat = child.stat(follow_symlinks=False)
                kind = kind_of(child_stat.st_mode)
            except OSError as error:
                yield WalkError(path, str(error))
                continue
            yield Entry(path, kind, child_stat if kind == "file" else None)
            if kind == "directory":
                pending.append(path)
