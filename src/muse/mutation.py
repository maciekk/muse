"""Small filesystem primitives for recoverable repository changes."""

from __future__ import annotations

import ctypes
import fcntl
import json
import os
import secrets
import shutil
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Literal

from muse.hashing import FileIdentity, sha256_file


def occupied(path: Path) -> bool:
    """A dangling symlink is occupied too."""
    return path.exists() or path.is_symlink()


def endpoint_state(
    source: Path, destination: Path
) -> Literal["pending", "completed", "conflicting", "missing"]:
    source_exists, destination_exists = occupied(source), occupied(destination)
    if source_exists and destination_exists:
        return "conflicting"
    if source_exists:
        return "pending"
    if destination_exists:
        return "completed"
    return "missing"


def checked_path(root: Path, value: str | Path, *, area: str | None = None) -> Path:
    """Return a lexical repository path without following managed-content links."""
    relative = Path(value)
    if relative.is_absolute() or not relative.parts or ".." in relative.parts:
        raise ValueError(f"unsafe repository path: {value}")
    if area is not None and relative.parts[0] != area:
        raise ValueError(f"path must be beneath {area}/: {value}")
    root = root.absolute()
    path = root / relative
    current = path
    while current != root:
        if current.is_symlink():
            raise ValueError(f"symbolic path components are not supported: {current}")
        current = current.parent
    return path


def sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}-", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w") as stream:
            json.dump(value, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        sync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def mutation_lock(root: Path) -> Iterator[None]:
    state = root / ".muse"
    if state.is_symlink():
        raise ValueError("library operational state must not be a symlink")
    state.mkdir(parents=True, exist_ok=True)
    lock_path = state / "mutation.lock"
    if lock_path.is_symlink():
        raise ValueError("repository mutation lock must not be a symlink")
    with lock_path.open("a+b") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ValueError("another repository mutation is in progress") from error
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def rename_exact(source: Path, destination: Path) -> None:
    if not occupied(source):
        raise ValueError(f"source does not exist: {source}")
    if occupied(destination):
        raise ValueError(f"destination already exists: {destination}")
    if source.stat().st_dev != destination.parent.stat().st_dev:
        raise ValueError("rename crosses filesystems; use a verified copy")
    rename_noreplace = ctypes.CDLL(None, use_errno=True).renameat2
    result = rename_noreplace(
        ctypes.c_int(-100),
        ctypes.c_char_p(os.fsencode(source)),
        ctypes.c_int(-100),
        ctypes.c_char_p(os.fsencode(destination)),
        ctypes.c_uint(1),
    )
    if result != 0:
        error = ctypes.get_errno()
        if error == 17:
            raise ValueError(f"destination already exists: {destination}")
        raise OSError(error, os.strerror(error), str(source))
    sync_directory(destination.parent)
    if source.parent != destination.parent:
        sync_directory(source.parent)


def archive_record(source: Path, directory: Path, prefix: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    for _ in range(100):
        destination = directory / f"{prefix}-{secrets.token_hex(4)}.json"
        if occupied(destination):
            continue
        rename_exact(source, destination)
        return destination
    raise ValueError("could not allocate a unique audit record")


def verified_copy_remove(
    source: Path, destination: Path, *, before_source_removal: Callable[[], None] | None = None
) -> bool:
    """Publish a verified copy, then remove its source. Return True for a new copy."""
    if source.is_symlink() or not source.is_file():
        raise ValueError(f"source is not a regular file: {source}")
    identity = FileIdentity.from_path(source)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if occupied(destination):
        if destination.is_symlink() or not destination.is_file():
            raise ValueError(f"destination already exists: {destination}")
        if sha256_file(source) != sha256_file(destination):
            raise ValueError(f"destination differs: {destination}")
        if FileIdentity.from_path(source) != identity:
            raise ValueError(f"source changed during copy: {source}")
        if before_source_removal is not None:
            before_source_removal()
        source.unlink()
        sync_directory(source.parent)
        return False
    descriptor, name = tempfile.mkstemp(
        prefix=f".{destination.name}-", suffix=".tmp", dir=destination.parent
    )
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as output, source.open("rb") as input_file:
            shutil.copyfileobj(input_file, output)
            output.flush()
            os.fsync(output.fileno())
        shutil.copystat(source, temporary)
        if sha256_file(source) != sha256_file(temporary):
            raise ValueError(f"move verification failed: {source}")
        if FileIdentity.from_path(source) != identity:
            raise ValueError(f"source changed during copy: {source}")
        if occupied(destination):
            raise ValueError(f"destination already exists: {destination}")
        os.link(temporary, destination)
        sync_directory(destination.parent)
        if FileIdentity.from_path(source) != identity:
            raise ValueError(f"source changed during copy: {source}")
        if before_source_removal is not None:
            before_source_removal()
        source.unlink()
        sync_directory(source.parent)
        return True
    finally:
        temporary.unlink(missing_ok=True)
