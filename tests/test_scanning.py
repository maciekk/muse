from pathlib import Path

from muse.scanning import compare_with_vault


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
    (target / "renamed.dat").write_bytes(b"same bytes")
    (target / "known.bin").write_bytes(b"different!")
    (target / "empty").write_bytes(b"")
    (root / "incoming" / "also-empty").write_bytes(b"")

    report = compare_with_vault(root, target, thorough=True, max_threads=2)

    assert report.present_files == 2
    assert [item.relative for item in report.new_files] == ["known.bin"]
    assert report.hashed_files == 5
    assert not report.errors


def test_scan_rejects_target_inside_vault(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    target = root / "backlog"
    target.mkdir(parents=True)

    report = compare_with_vault(root, target)

    assert report.errors
    assert "overlaps vault content area" in report.errors[0].message
