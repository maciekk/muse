import os
from pathlib import Path

from muse.filesystem import Entry, WalkError, walk
from muse.importing import ImportValidationError, _inventory
from muse.repository import scan_path
from muse.tree_diff import compare_trees, fingerprint_metadata_trees, fingerprint_trees


def test_inventory_reports_entry_kinds_without_following_links(tmp_path: Path) -> None:
    root = tmp_path / "tree"
    (root / "empty").mkdir(parents=True)
    (root / "zero.flac").touch()
    (root / "linked").symlink_to(root / "empty", target_is_directory=True)
    os.mkfifo(root / "pipe")

    entries = list(walk(root))
    kinds = {item.path.name: item.kind for item in entries if isinstance(item, Entry)}
    assert kinds == {
        "tree": "directory",
        "empty": "directory",
        "zero.flac": "file",
        "linked": "symlink",
        "pipe": "other",
    }
    assert [
        (item.kind, item.stat.st_size)
        for item in walk(root / "zero.flac")
        if isinstance(item, Entry)
    ] == [("file", 0)]
    assert [item.kind for item in walk(root / "linked") if isinstance(item, Entry)] == ["symlink"]
    assert scan_path(root).to_dict()["other_entries"] == 1


def test_unreadable_subtree_blocks_fingerprints(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "tree"
    hidden = root / "hidden"
    hidden.mkdir(parents=True)
    (hidden / "song.flac").write_bytes(b"audio")
    original_scandir = os.scandir

    def fail_hidden(path):
        if Path(path) == hidden:
            raise PermissionError("unreadable subtree")
        return original_scandir(path)

    monkeypatch.setattr("muse.filesystem.os.scandir", fail_hidden)
    events = list(walk(root))
    assert any(isinstance(item, WalkError) and item.path == hidden for item in events)

    content, content_errors = fingerprint_trees([root], tmp_path / ".muse" / "muse.db")
    metadata, metadata_errors = fingerprint_metadata_trees([root])
    assert root.absolute() not in content
    assert root.absolute() not in metadata
    assert content_errors and metadata_errors
    assert compare_trees(root, root, tmp_path / ".muse" / "muse.db").errors


def test_import_rejects_symlinks_and_special_entries(tmp_path: Path) -> None:
    source = tmp_path / "release"
    source.mkdir()
    (source / "link").symlink_to(tmp_path)
    os.mkfifo(source / "pipe")

    try:
        _inventory(source)
    except ImportValidationError as error:
        assert any("symbolic links are not supported: link" in item for item in error.blockers)
        assert any("special files are not supported: pipe" in item for item in error.blockers)
    else:
        raise AssertionError("import accepted unsafe entries")
