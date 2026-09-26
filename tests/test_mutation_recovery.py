import json
import sqlite3
from pathlib import Path

import pytest

from muse import cache, compaction, importing, moves, mutation, slag
from muse.duplicates import find_duplicates
from muse.hashing import FileIdentity, sha256_file
from muse.slag import SlagCopy


def test_compaction_resumes_second_move_into_same_receipt(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "vault"
    for name in ("one", "two", "three"):
        directory = root / "backlog" / name
        directory.mkdir(parents=True)
        (directory / "song.flac").write_bytes(b"audio")
    operations, errors = compaction.make_plan(root)
    assert not errors and len(operations) == 2
    compaction.save_plan(root, operations)
    original = compaction.rename_exact
    count = 0

    def interrupted(source, destination):
        nonlocal count
        count += 1
        if count == 2:
            raise RuntimeError("interrupted")
        original(source, destination)

    monkeypatch.setattr(compaction, "rename_exact", interrupted)
    with pytest.raises(RuntimeError, match="interrupted"):
        compaction.apply_plan(root, operations)
    state = json.loads(compaction.plan_path(root).read_text())
    receipt = root / state["receipt"]
    assert state["state"] == "moving"
    assert (receipt / operations[0].remove).is_dir()
    monkeypatch.setattr(compaction, "rename_exact", original)
    assert compaction.apply_plan(root, operations) == receipt
    assert (receipt / operations[1].remove).is_dir()
    assert not compaction.plan_path(root).exists()
    assert len(list((root / "trash").glob("**/receipt.json"))) == 1


def test_move_reconciles_cache_after_interruption(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "vault"
    source = root / "backlog" / "old"
    source.mkdir(parents=True)
    (source / "song.flac").write_bytes(b"audio")
    database = root / ".muse" / "muse.db"
    find_duplicates(root, database, trees=True)
    destination = root / "backlog" / "new"
    original = moves.cache.relocate

    def interrupted(*args):
        raise RuntimeError("cache unavailable")

    monkeypatch.setattr(moves.cache, "relocate", interrupted)
    with pytest.raises(RuntimeError, match="cache unavailable"):
        moves.move(root, source, destination)
    assert destination.is_dir()
    assert (root / ".muse" / "move.json").exists()
    monkeypatch.setattr(moves.cache, "relocate", original)
    assert moves.move(root, source, destination).cached_paths_updated == 1
    with sqlite3.connect(database) as connection:
        paths = connection.execute("SELECT path FROM file_hashes").fetchall()
    assert paths == [(str(destination / "song.flac"),)]


def test_slag_failure_keeps_source_and_cleans_staging(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "vault"
    source = root / "backlog" / "note.txt"
    destination = root / "slag" / "note.txt"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"complete")
    original = mutation.os.link

    def interrupted(*args):
        raise RuntimeError("publication interrupted")

    monkeypatch.setattr(mutation.os, "link", interrupted)
    with pytest.raises(RuntimeError, match="publication interrupted"):
        slag.apply([SlagCopy(source, destination, 8)], root=root)
    assert source.read_bytes() == b"complete"
    assert not destination.exists()
    assert list(destination.parent.glob(".*.tmp")) == []
    monkeypatch.setattr(mutation.os, "link", original)
    assert slag.apply([SlagCopy(source, destination, 8)], root=root) == (1, 0)


def test_mutation_lock_rejects_second_owner(tmp_path: Path) -> None:
    with (
        mutation.mutation_lock(tmp_path),
        pytest.raises(ValueError, match="another repository mutation"),
        mutation.mutation_lock(tmp_path),
    ):
        pass


def test_import_recovers_after_cache_reconciliation_failure(tmp_path: Path, monkeypatch) -> None:
    # Exercise the persisted applying state without depending on an external media encoder.
    root = tmp_path / "vault"
    source = root / "backlog" / "song.flac"
    destination = root / "master" / "song.flac"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"audio")
    database = root / ".muse" / "muse.db"
    database.parent.mkdir()
    with sqlite3.connect(database) as connection:
        cache.initialize_database(connection)
        cache.insert(connection, source, FileIdentity.from_path(source), sha256_file(source))
    plan = importing.ImportPlan(
        3, "backlog/song.flac", "master/song.flac", "selection", "ready", "now", ()
    )
    importing._write(root / ".muse" / "imports" / importing._plan_key(plan.source), plan)
    monkeypatch.setattr(importing, "_verify", lambda *args: None)
    original = importing._reconcile_cache
    monkeypatch.setattr(
        importing,
        "_reconcile_cache",
        lambda *args: (_ for _ in ()).throw(RuntimeError("cache unavailable")),
    )
    with pytest.raises(RuntimeError, match="cache unavailable"):
        importing.apply_plan(root)
    assert destination.is_file() and not source.exists()
    monkeypatch.setattr(importing, "_reconcile_cache", original)
    assert importing.apply_plan(root).state == "completed"
    with sqlite3.connect(database) as connection:
        paths = connection.execute("SELECT path FROM file_hashes").fetchall()
    assert paths == [(str(destination),)]


def test_atomic_json_failure_preserves_previous_record(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "plan.json"
    mutation.atomic_json(path, {"state": "ready"})

    def interrupted(*args):
        raise RuntimeError("write interrupted")

    monkeypatch.setattr(mutation.os, "replace", interrupted)
    with pytest.raises(RuntimeError, match="write interrupted"):
        mutation.atomic_json(path, {"state": "moving"})
    assert json.loads(path.read_text()) == {"state": "ready"}
    assert list(tmp_path.glob(".plan.json-*.tmp")) == []


def test_slag_rejects_partial_destination_and_symlink(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    source = root / "backlog" / "note.txt"
    destination = root / "slag" / "note.txt"
    source.parent.mkdir(parents=True)
    destination.parent.mkdir(parents=True)
    source.write_bytes(b"complete")
    destination.write_bytes(b"partial")
    with pytest.raises(ValueError, match="destination differs"):
        slag.apply([SlagCopy(source, destination, 8)], root=root)
    assert source.read_bytes() == b"complete"
    destination.unlink()
    destination.symlink_to(tmp_path / "absent")
    with pytest.raises(ValueError, match="symbolic path"):
        slag.apply([SlagCopy(source, destination, 8)], root=root)
    assert source.read_bytes() == b"complete"


def test_compaction_rejects_conflicting_receipt_destination(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "vault"
    for name in ("one", "two", "three"):
        directory = root / "backlog" / name
        directory.mkdir(parents=True)
        (directory / "song.flac").write_bytes(b"audio")
    operations, errors = compaction.make_plan(root)
    assert not errors
    compaction.save_plan(root, operations)
    original = compaction.rename_exact
    count = 0

    def interrupted(source, destination):
        nonlocal count
        count += 1
        if count == 2:
            raise RuntimeError("interrupted")
        original(source, destination)

    monkeypatch.setattr(compaction, "rename_exact", interrupted)
    with pytest.raises(RuntimeError):
        compaction.apply_plan(root, operations)
    receipt = root / json.loads(compaction.plan_path(root).read_text())["receipt"]
    conflicting = receipt / operations[1].remove
    conflicting.mkdir(parents=True)
    with pytest.raises(ValueError, match="both source and trash destination exist"):
        compaction.apply_plan(root, operations)
    assert (root / operations[1].remove).is_dir()


def test_slag_resumes_after_source_removal_failure(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "vault"
    source = root / "backlog" / "note.txt"
    destination = root / "slag" / "note.txt"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"complete")
    original = Path.unlink

    def interrupted(path, *args, **kwargs):
        if path == source:
            raise RuntimeError("removal interrupted")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", interrupted)
    with pytest.raises(RuntimeError, match="removal interrupted"):
        slag.apply([SlagCopy(source, destination, 8)], root=root)
    assert source.is_file() and destination.read_bytes() == b"complete"
    monkeypatch.setattr(Path, "unlink", original)
    assert slag.apply([SlagCopy(source, destination, 8)], root=root) == (0, 1)
    assert not source.exists()


def test_compaction_resumes_after_audit_failure(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "vault"
    for name in ("one", "two"):
        directory = root / "backlog" / name
        directory.mkdir(parents=True)
        (directory / "song.flac").write_bytes(b"audio")
    operations, errors = compaction.make_plan(root)
    assert not errors
    compaction.save_plan(root, operations)
    original = compaction.archive_record

    def interrupted(*args):
        raise RuntimeError("audit interrupted")

    monkeypatch.setattr(compaction, "archive_record", interrupted)
    with pytest.raises(RuntimeError, match="audit interrupted"):
        compaction.apply_plan(root, operations)
    state = json.loads(compaction.plan_path(root).read_text())
    receipt = root / state["receipt"]
    assert (receipt / operations[0].remove).is_dir()
    monkeypatch.setattr(compaction, "archive_record", original)
    assert compaction.apply_plan(root, operations) == receipt
    assert len(list((root / "trash").glob("**/receipt.json"))) == 1


def test_slag_retries_cache_failure_before_source_removal(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "vault"
    source = root / "backlog" / "note.txt"
    destination = root / "slag" / "note.txt"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"complete")
    database = root / ".muse" / "muse.db"
    database.parent.mkdir()
    with sqlite3.connect(database) as connection:
        cache.initialize_database(connection)
        cache.insert(connection, source, FileIdentity.from_path(source), sha256_file(source))
    original = slag.cache.relocate

    def interrupted(*args):
        raise RuntimeError("cache unavailable")

    monkeypatch.setattr(slag.cache, "relocate", interrupted)
    with pytest.raises(RuntimeError, match="cache unavailable"):
        slag.apply([SlagCopy(source, destination, 8)], root=root)
    assert source.is_file() and destination.is_file()
    monkeypatch.setattr(slag.cache, "relocate", original)
    assert slag.apply([SlagCopy(source, destination, 8)], root=root) == (0, 1)
    with sqlite3.connect(database) as connection:
        paths = connection.execute("SELECT path FROM file_hashes").fetchall()
    assert paths == [(str(destination),)]


def test_cache_relocation_handles_preexisting_destination_entry(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    source = root / "backlog" / "song.flac"
    destination = root / "slag" / "song.flac"
    source.parent.mkdir(parents=True)
    destination.parent.mkdir(parents=True)
    source.write_bytes(b"same")
    destination.write_bytes(b"same")
    database = tmp_path / "cache.db"
    with sqlite3.connect(database) as connection:
        cache.initialize_database(connection)
        digest = sha256_file(source)
        cache.insert(connection, source, FileIdentity.from_path(source), digest)
        cache.insert(connection, destination, FileIdentity.from_path(destination), digest)
        assert cache.relocate(connection, source, destination) == 1
        paths = connection.execute("SELECT path FROM file_hashes").fetchall()
    assert paths == [(str(destination),)]


def test_legacy_ready_compaction_plan_still_applies(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    for name in ("one", "two"):
        directory = root / "backlog" / name
        directory.mkdir(parents=True)
        (directory / "song.flac").write_bytes(b"audio")
    operations, errors = compaction.make_plan(root)
    assert not errors
    path = compaction.save_plan(root, operations)
    legacy = json.loads(path.read_text())
    for field in ("schema_version", "state", "receipt"):
        legacy.pop(field)
    mutation.atomic_json(path, legacy)
    assert compaction.load_plan(root) == operations
    receipt = compaction.apply_plan(root, operations)
    assert receipt is not None and (receipt / operations[0].remove).is_dir()
