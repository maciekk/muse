from pathlib import Path

from muse.scanning import PullProgress, ScanProgress, compare_with_vault, pull_new_directories


def test_quick_scan_compares_name_case_insensitively_and_size(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    target = tmp_path / "drive"
    (root / "master" / "album").mkdir(parents=True)
    (root / "backlog").mkdir()
    target.mkdir()
    (root / "master" / "album" / "Song.FLAC").write_bytes(b"same-size")
    (root / "backlog" / "notes.txt").write_bytes(b"known elsewhere")
    (target / "renamed").mkdir()
    (target / "renamed" / "song.flac").write_bytes(b"different")
    (target / "new.mp3").write_bytes(b"new audio")

    report = compare_with_vault(root, target)

    assert report.mode == "quick"
    assert report.target_files == 2
    assert report.present_files == 1
    assert [item.relative for item in report.new_files] == ["new.mp3"]
    assert report.new_audio_files == 1
    assert set(report.vault_areas) == {"master", "backlog"}


def test_thorough_scan_matches_content_regardless_of_name(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    target = tmp_path / "drive"
    (root / "incoming").mkdir(parents=True)
    target.mkdir()
    (root / "incoming" / "known.bin").write_bytes(b"same bytes")
    (target / "renamed.flac").write_bytes(b"same bytes")
    (target / "known.mp3").write_bytes(b"different!")
    (target / "empty.wav").write_bytes(b"")
    (root / "incoming" / "also-empty").write_bytes(b"")

    report = compare_with_vault(root, target, thorough=True, max_threads=2)

    assert report.present_files == 2
    assert [item.relative for item in report.new_files] == ["known.mp3"]
    assert report.hashed_files == 5
    assert not report.errors


def test_scan_reports_target_inventory_progress_and_extension_totals(
    tmp_path: Path,
) -> None:
    root = tmp_path / "vault"
    target = tmp_path / "drive"
    (root / "master").mkdir(parents=True)
    target.mkdir()
    (target / "song.mp3").write_bytes(b"audio")
    (target / "movie.mp4").write_bytes(b"home movie")
    updates: list[ScanProgress] = []

    report = compare_with_vault(root, target, progress=updates.append)

    assert updates[0] == ScanProgress()
    assert updates[-1] == ScanProgress(2, 15, complete=True)
    assert report.inventory_files == 2
    assert report.target_files == 1
    assert report.target_audio_files == 1
    assert [item.relative for item in report.new_files] == ["song.mp3"]
    assert report.target_extensions == {".mp3": 1}
    assert report.target_extension_bytes == {".mp3": 5}
    assert all(item.extension != ".mp4" for item in report.new_files)


def test_pull_reports_progress_for_every_file_in_containing_directory(
    tmp_path: Path,
) -> None:
    root = tmp_path / "vault"
    target = tmp_path / "drive"
    album = target / "album"
    (root / "backlog").mkdir(parents=True)
    album.mkdir(parents=True)
    (album / "lost.flac").write_bytes(b"audio")
    (album / "cover.jpg").write_bytes(b"art")
    report = compare_with_vault(root, target)
    updates: list[PullProgress] = []

    pull_new_directories(
        root,
        target,
        report.new_files,
        root / "backlog" / "drive",
        updates.append,
    )

    assert updates[0] == PullProgress(total_files=2, total_bytes=8)
    assert updates[-1] == PullProgress(
        total_files=2,
        total_bytes=8,
        completed_files=2,
        completed_bytes=8,
        complete=True,
    )
    assert [update.completed_files for update in updates[1:-1]] == [1, 2]


def test_scan_rejects_target_inside_vault(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    target = root / "backlog"
    target.mkdir(parents=True)

    report = compare_with_vault(root, target)

    assert report.errors
    assert "overlaps vault content area" in report.errors[0].message
