import sqlite3
from pathlib import Path

from muse import ledger
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


def test_move_preserves_stable_item_identity_and_version_history(tmp_path: Path) -> None:
    root = tmp_path / "music-vault"
    source = root / "master" / "artists" / "Artist" / "Old Album"
    source.mkdir(parents=True)
    song = source / "song.flac"
    song.write_bytes(b"audio")
    database = root / ".muse" / "muse.db"
    database.parent.mkdir()
    with sqlite3.connect(database) as connection:
        ledger.initialize_database(connection)
        item_id = ledger.admit(
            connection,
            [(song.relative_to(root), song.stat().st_size, "a" * 64)],
        )[0]

    destination = root / "master" / "artists" / "Artist" / "New Album"
    result = move(root, source, destination)

    assert result.item_paths_updated == 1
    with sqlite3.connect(database) as connection:
        item = connection.execute(
            "SELECT id, area, path, current_sha256 FROM library_items"
        ).fetchone()
        versions = connection.execute(
            "SELECT item_id, sha256 FROM item_versions"
        ).fetchall()
    assert item == (item_id, "master", "master/artists/Artist/New Album/song.flac", "a" * 64)
    assert versions == [(item_id, "a" * 64)]


def test_move_refuses_registered_item_out_of_publishable_areas(tmp_path: Path) -> None:
    root = tmp_path / "music-vault"
    source = root / "master" / "song.flac"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"audio")
    (root / "backlog").mkdir()
    database = root / ".muse" / "muse.db"
    database.parent.mkdir()
    with sqlite3.connect(database) as connection:
        ledger.initialize_database(connection)
        ledger.admit(connection, [(Path("master/song.flac"), 5, "a" * 64)])

    try:
        move(root, source, root / "backlog" / "song.flac")
    except ValueError as error:
        assert "master/ or stopgap" in str(error)
    else:
        raise AssertionError("registered item left its managed lifecycle")

    assert source.is_file()
    assert not (root / ".muse" / "move.json").exists()


def test_move_places_source_inside_existing_directory(tmp_path: Path) -> None:
    root = tmp_path / "music-vault"
    source = root / "backlog" / "source"
    destination = root / "backlog" / "destination"
    source.mkdir(parents=True)
    destination.mkdir()

    result = move(root, source, destination)

    assert result.destination == str(destination / "source")
    assert (destination / "source").is_dir()


def test_move_trailing_slash_creates_directory_and_keeps_source_directory(tmp_path: Path) -> None:
    root = tmp_path / "music-vault"
    source = root / "backlog" / "albumA"
    source.mkdir(parents=True)
    (source / "song.flac").write_bytes(b"audio")
    destination = root / "backlog" / "dest" / "dir"

    result = move(root, source, f"{destination}/")

    assert result.destination == str(destination / "albumA")
    assert (destination / "albumA" / "song.flac").read_bytes() == b"audio"
    assert not source.exists()


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
