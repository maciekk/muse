import os
import sqlite3
from pathlib import Path

from muse.duplicates import find_duplicates


def test_find_duplicates_reports_exact_matches_and_reuses_hashes(tmp_path: Path) -> None:
    (tmp_path / "album").mkdir()
    (tmp_path / "album" / "one.flac").write_bytes(b"same audio")
    (tmp_path / "copy.flac").write_bytes(b"same audio")
    (tmp_path / "unique.flac").write_bytes(b"different")
    database = tmp_path / ".muse" / "muse.db"
    updates = []

    first = find_duplicates(tmp_path, database, progress=updates.append)

    assert first.files == 3
    assert first.hash_candidate_files == 2
    assert first.hashed_files == 2
    assert first.hashed_bytes == len(b"same audio") * 2
    assert first.bytes_read == first.hashed_bytes
    assert first.cached_files == 0
    assert [update.phase for update in updates] == [
        "inventory",
        "inventory",
        "inventory",
        "inventory",
        "hashing",
        "hashing",
        "hashing",
        "hashing",
        "hashing",
        "analysis",
        "analysis",
        "complete",
    ]
    assert len(first.groups) == 1
    assert first.duplicate_occurrences == 2
    assert first.redundant_occurrences == 1
    assert first.logical_repeated_bytes == len(b"same audio")
    assert first.groups[0].paths == ("album/one.flac", "copy.flac")
    assert first.errors == []

    second = find_duplicates(tmp_path, database)

    assert second.hashed_files == 0
    assert second.cached_files == 2
    assert second.cached_bytes == second.hash_candidate_bytes
    assert second.cache_hit_rate == 1.0
    assert second.byte_cache_hit_rate == 1.0
    assert second.bytes_read == 0
    assert len(second.groups) == 1


def test_changed_file_is_rehashed(tmp_path: Path) -> None:
    first_path = tmp_path / "first.mp3"
    second_path = tmp_path / "second.mp3"
    first_path.write_bytes(b"abc")
    second_path.write_bytes(b"abc")
    database = tmp_path / ".muse" / "muse.db"
    find_duplicates(tmp_path, database)

    first_path.write_bytes(b"xyz")
    stat = first_path.stat()
    os.utime(first_path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
    report = find_duplicates(tmp_path, database)

    assert report.hashed_files == 1
    assert report.cached_files == 1
    assert report.groups == []


def test_hash_cache_records_file_metadata(tmp_path: Path) -> None:
    song = tmp_path / "song.flac"
    copy = tmp_path / "copy.flac"
    song.write_bytes(b"audio")
    copy.write_bytes(b"audio")
    database = tmp_path / ".muse" / "muse.db"

    find_duplicates(tmp_path, database)

    with sqlite3.connect(database) as connection:
        rows = connection.execute(
            "SELECT path, size, mtime_ns, length(sha256) FROM file_hashes ORDER BY path"
        ).fetchall()
    assert rows == [
        (str(copy), 5, copy.stat().st_mtime_ns, 64),
        (str(song), 5, song.stat().st_mtime_ns, 64),
    ]
