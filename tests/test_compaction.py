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

    apply_plan(root, operations)
    assert (root / "master" / "album" / "song.flac").is_file()
    assert not (root / "backlog" / "old" / "album").exists()
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
