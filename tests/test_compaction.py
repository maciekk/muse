import json
from pathlib import Path

from muse.compaction import apply_plan, load_plan, make_plan, save_plan


def test_compaction_plan_prefers_master_and_removes_backlog_copy(tmp_path: Path) -> None:
    root = tmp_path / "music-vault"
    (root / "master" / "album").mkdir(parents=True)
    (root / "backlog" / "old" / "album").mkdir(parents=True)
    (root / "master" / "album" / "song.flac").write_bytes(b"audio")
    (root / "backlog" / "old" / "album" / "song.flac").write_bytes(b"audio")

    operations, errors = make_plan(root)

    assert errors == []
    assert len(operations) == 1
    assert operations[0].retain == "master"
    assert operations[0].remove == "backlog/old"
    save_plan(root, operations)
    assert load_plan(root) == operations
    removed_inode = (root / "backlog" / "old" / "album" / "song.flac").stat().st_ino

    progress = []
    receipt = apply_plan(root, operations, progress.append)
    assert [(update.phase, update.completed_operations) for update in progress] == [
        ("verify", 0),
        ("verify", 1),
        ("trash", 0),
        ("trash", 1),
    ]
    assert (root / "master" / "album" / "song.flac").is_file()
    assert not (root / "backlog" / "old" / "album").exists()
    assert receipt is not None
    trashed_file = receipt / "backlog" / "old" / "album" / "song.flac"
    assert trashed_file.read_bytes() == b"audio"
    assert trashed_file.stat().st_ino == removed_inode
    assert json.loads((receipt / "receipt.json").read_text())["state"] == "complete"
    assert not (root / ".muse" / "compact-plan.json").exists()
    assert list((root / ".muse" / "audit").glob("compact-*.json"))


def test_compaction_plan_preserves_paths_relative_to_library_root(tmp_path: Path) -> None:
    root = tmp_path / "music-vault"
    for name in ("one", "two"):
        (root / "backlog" / name).mkdir(parents=True)
        (root / "backlog" / name / "song.flac").write_bytes(b"audio")

    operations, errors = make_plan(root, root / "backlog")

    assert not errors
    assert {operations[0].retain, operations[0].remove} == {"backlog/one", "backlog/two"}


def test_compaction_ignores_empty_and_tiny_duplicate_trees(tmp_path: Path) -> None:
    root = tmp_path / "music-vault"
    for size in range(4):
        for copy in ("one", "two"):
            directory = root / "backlog" / f"size-{size}-{copy}"
            directory.mkdir(parents=True)
            (directory / "marker").write_bytes(b"x" * size)

    operations, errors = make_plan(root)

    assert not errors
    assert len(operations) == 1
    assert operations[0].logical_bytes == 3
    assert operations[0].remove.startswith("backlog/size-3-")


def test_compaction_applies_through_symlinked_library_root(tmp_path: Path) -> None:
    actual_root = tmp_path / "actual-music-vault"
    root = tmp_path / "music-vault"
    for name in ("one", "two"):
        (actual_root / "backlog" / name).mkdir(parents=True)
        (actual_root / "backlog" / name / "song.flac").write_bytes(b"audio")
    root.symlink_to(actual_root, target_is_directory=True)

    operations, errors = make_plan(root)

    assert not errors
    save_plan(root, operations)
    receipt = apply_plan(root, operations)
    assert (root / operations[0].retain).is_dir()
    assert not (root / operations[0].remove).exists()
    assert receipt is not None
    assert (receipt / operations[0].remove / "song.flac").is_file()


def test_compaction_refuses_changed_tree(tmp_path: Path) -> None:
    root = tmp_path / "music-vault"
    for name in ("one", "two"):
        (root / "backlog" / name).mkdir(parents=True)
        (root / "backlog" / name / "song.flac").write_bytes(b"audio")
    operations, errors = make_plan(root)
    assert not errors
    (root / "backlog" / "two" / "song.flac").write_bytes(b"changed")

    try:
        apply_plan(root, operations)
    except ValueError as error:
        assert "no longer match" in str(error)
    else:
        raise AssertionError("changed tree was removed")
    assert (root / "backlog" / "two").exists()
