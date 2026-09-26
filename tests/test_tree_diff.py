from pathlib import Path

from muse.duplicates import find_duplicates
from muse.tree_diff import compare_trees, fingerprint_trees


def test_compare_trees_reports_structural_and_content_differences(tmp_path: Path) -> None:
    left = tmp_path / "left"
    right = tmp_path / "right"
    (left / "shared").mkdir(parents=True)
    (right / "shared").mkdir(parents=True)
    (left / "shared" / "same.flac").write_bytes(b"same")
    (right / "shared" / "same.flac").write_bytes(b"same")
    (left / "changed.flac").write_bytes(b"left")
    (right / "changed.flac").write_bytes(b"right")
    (left / "left-only").mkdir()
    (right / "right-only.flac").write_bytes(b"right")

    report = compare_trees(left, right, tmp_path / ".muse" / "muse.db")

    assert report.left_files == 2
    assert report.right_files == 3
    assert report.hashed_files == 5
    assert [(item.kind, item.path) for item in report.differences] == [
        ("content-mismatch", "changed.flac"),
        ("only-left", "left-only"),
        ("only-right", "right-only.flac"),
    ]

    cached = compare_trees(left, right, tmp_path / ".muse" / "muse.db")
    assert cached.hashed_files == 0
    assert cached.cached_files == 5


def test_duplicate_discovery_and_tree_verification_share_fingerprint(tmp_path: Path) -> None:
    for name in ("one", "two"):
        tree = tmp_path / name
        (tree / "empty").mkdir(parents=True)
        (tree / "zero.flac").touch()
        (tree / "song.flac").write_bytes(b"audio")
    database = tmp_path / ".muse" / "muse.db"

    report = find_duplicates(tmp_path, database, trees=True)
    verified, errors = fingerprint_trees([tmp_path / "one", tmp_path / "two"], database)

    assert not report.errors and not errors
    assert len(report.tree_groups) == 1
    group = report.tree_groups[0]
    assert group.files == 2
    assert group.logical_bytes == 5
    assert group.sha256 == verified[(tmp_path / "one").absolute()].sha256
    assert group.sha256 == verified[(tmp_path / "two").absolute()].sha256
