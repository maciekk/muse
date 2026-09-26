import os
from pathlib import Path

import pytest

from muse.hashing import FileIdentity, sha256_file


def test_hash_rejects_file_changed_after_inventory(tmp_path: Path) -> None:
    path = tmp_path / "song.flac"
    path.write_bytes(b"original")
    identity = FileIdentity.from_path(path)
    path.write_bytes(b"modified")

    with pytest.raises(OSError, match="changed after inventory"):
        sha256_file(path, expected=identity)


def test_hash_rejects_change_during_read_even_when_size_and_mtime_match(tmp_path: Path) -> None:
    path = tmp_path / "song.flac"
    path.write_bytes(b"original")
    identity = FileIdentity.from_path(path)

    def change_file(_count: int) -> None:
        path.write_bytes(b"modified")
        os.utime(path, ns=(identity.mtime_ns, identity.mtime_ns))

    with pytest.raises(OSError, match="changed while being hashed"):
        sha256_file(path, expected=identity, on_bytes_read=change_file)
