import sqlite3
from pathlib import Path

from muse.cache import prune_missing
from muse.duplicates import find_duplicates


def test_prune_removes_only_hashes_for_missing_files(tmp_path: Path) -> None:
    root = tmp_path / "music-vault"
    root.mkdir()
    retained = root / "retained.flac"
    removed = root / "removed.flac"
    retained.write_bytes(b"keep")
    removed.write_bytes(b"remove")
    database = root / ".muse" / "muse.db"
    find_duplicates(root, database, trees=True)
    removed.unlink()

    report = prune_missing(database)

    assert report.database_exists is True
    assert report.entries_before == 2
    assert report.entries_removed == 1
    assert report.entries_after == 1
    assert report.represented_bytes_before == 10
    assert report.represented_bytes_removed == 6
    assert report.represented_bytes_after == 4
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT path FROM file_hashes").fetchall() == [(str(retained),)]

    repeated = prune_missing(database)
    assert repeated.entries_before == 1
    assert repeated.entries_removed == 0
    assert repeated.entries_after == 1


def test_prune_does_not_create_a_missing_database(tmp_path: Path) -> None:
    database = tmp_path / ".muse" / "muse.db"

    report = prune_missing(database)

    assert report.database_exists is False
    assert report.entries_before == 0
    assert not database.exists()
