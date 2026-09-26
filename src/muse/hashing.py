"""Fresh SHA-256 hashing with file-change detection."""

from __future__ import annotations

import hashlib
import os
import stat
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class FileIdentity:
    size: int
    mtime_ns: int
    ctime_ns: int
    device: int
    inode: int

    @classmethod
    def from_path(cls, path: Path) -> FileIdentity:
        result = path.stat(follow_symlinks=False)
        if not stat.S_ISREG(result.st_mode):
            raise OSError(f"not a regular file: {path}")
        return cls(
            result.st_size, result.st_mtime_ns, result.st_ctime_ns, result.st_dev, result.st_ino
        )


def sha256_file(
    path: Path,
    *,
    expected: FileIdentity | None = None,
    on_bytes_read: Callable[[int], None] | None = None,
) -> str:
    """Hash bytes freshly, rejecting a changed file before or during the read."""
    before = FileIdentity.from_path(path)
    if expected is not None and before != expected:
        raise OSError("file changed after inventory")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        opened_stat = os.fstat(stream.fileno())
        if (opened_stat.st_dev, opened_stat.st_ino) != (before.device, before.inode):
            raise OSError("file changed while being hashed")
        while block := stream.read(4 * 1024 * 1024):
            digest.update(block)
            if on_bytes_read is not None:
                on_bytes_read(len(block))
    if FileIdentity.from_path(path) != before:
        raise OSError("file changed while being hashed")
    return digest.hexdigest()
