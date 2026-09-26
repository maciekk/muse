from pathlib import Path

from muse.repository import scan_path, scan_root_by_area


def test_scan_path_counts_files_bytes_extensions_and_symlinks(tmp_path: Path) -> None:
    album = tmp_path / "album"
    album.mkdir()
    (album / "song.FLAC").write_bytes(b"audio")
    (album / "cover.jpg").write_bytes(b"image-data")
    (tmp_path / "notes").write_text("hello")
    (tmp_path / "album-link").symlink_to(album, target_is_directory=True)

    stats = scan_path(tmp_path)

    assert stats.files == 3
    assert stats.audio_files == 1
    assert stats.non_audio_files == 2
    assert stats.directories == 1
    assert stats.symlinks == 1
    assert stats.logical_bytes == 5 + 10 + 5
    assert stats.extensions == {".flac": 1, ".jpg": 1, "[no extension]": 1}
    assert stats.errors == []


def test_scan_path_counts_zero_byte_files(tmp_path: Path) -> None:
    (tmp_path / "empty.flac").touch()
    (tmp_path / "nonempty.flac").write_bytes(b"audio")

    stats = scan_path(tmp_path)

    assert stats.files == 2
    assert stats.zero_byte_files == 1
    assert stats.to_dict()["zero_byte_files"] == 1


def test_scan_path_reports_missing_target(tmp_path: Path) -> None:
    stats = scan_path(tmp_path / "missing")

    assert len(stats.errors) == 1
    assert stats.errors[0].message == "path does not exist"


def test_scan_root_by_area_does_not_double_count(tmp_path: Path) -> None:
    master = tmp_path / "master"
    backlog = tmp_path / "backlog"
    master.mkdir()
    backlog.mkdir()
    (master / "one.mp3").write_bytes(b"123")
    (backlog / "two.flac").write_bytes(b"4567")

    total, areas = scan_root_by_area(tmp_path)

    assert set(areas) == {"backlog", "master"}
    assert total.files == 2
    assert total.audio_files == 2
    assert total.directories == 2
    assert total.logical_bytes == 7
