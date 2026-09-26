import sqlite3
from pathlib import Path

from muse.duplicates import find_duplicates
from muse.moves import move


def test_move_updates_cached_file_paths(tmp_path: Path) -> None:
    root = tmp_path / "music-vault"
    source = root / "backlog" / "old"
    source.mkdir(parents=True)
    song = source / "song.flac"
    song.write_bytes(b"audio")
    database = root / ".muse" / "muse.db"
    find_duplicates(root, database, trees=True)

    destination = root / "backlog" / "renamed"
    result = move(root, source, destination)

    assert result.cached_paths_updated == 1
    with sqlite3.connect(database) as connection:
        paths = connection.execute("SELECT path FROM file_hashes").fetchall()
    assert paths == [(str(destination / "song.flac"),)]

    report = find_duplicates(root, database, trees=True)
    assert report.hashed_files == 0
    assert report.cached_files == 1


def test_move_places_source_inside_existing_directory(tmp_path: Path) -> None:
    root = tmp_path / "music-vault"
    source = root / "backlog" / "source"
    destination = root / "backlog" / "destination"
    source.mkdir(parents=True)
    destination.mkdir()

    result = move(root, source, destination)

    assert result.destination == str(destination / "source")
    assert (destination / "source").is_dir()


def test_move_refuses_outside_root_or_existing_file(tmp_path: Path) -> None:
    root = tmp_path / "music-vault"
    source = root / "backlog" / "source"
    source.mkdir(parents=True)
    existing_file = root / "backlog" / "destination"
    existing_file.write_text("not a directory")

    for invalid_destination in (existing_file, tmp_path / "outside"):
        try:
            move(root, source, invalid_destination)
        except ValueError:
            pass
        else:
            raise AssertionError("unsafe move was accepted")
