import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from muse import ledger, registration


def make_master(root: Path) -> tuple[Path, Path]:
    album = root / "master" / "artists" / "Artist" / "Album"
    album.mkdir(parents=True)
    first = album / "01.flac"
    second = album / "02.mp3"
    first.write_bytes(b"first audio")
    second.write_bytes(b"second audio")
    (album / "cover.jpg").write_bytes(b"art")
    return first, second


def test_registration_plans_then_admits_existing_master_files(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "vault"
    first, second = make_master(root)
    monkeypatch.setattr(registration, "inspect_media", lambda _path: None)

    plan = registration.make_plan(root, root / "master")

    assert plan.state == "ready"
    assert [item.path for item in plan.files] == [
        "master/artists/Artist/Album/01.flac",
        "master/artists/Artist/Album/02.mp3",
    ]
    assert plan.already_registered == ()
    assert plan.ignored_files == 1
    assert first.is_file() and second.is_file()
    assert not (root / ".muse" / "muse.db").exists()

    completed = registration.apply_plan(root)

    assert completed.state == "completed"
    assert not registration.plan_path(root).exists()
    assert list((root / ".muse" / "audit").glob("register-*.json"))
    with sqlite3.connect(root / ".muse" / "muse.db") as connection:
        items = connection.execute(
            "SELECT id, path, current_sha256 FROM library_items ORDER BY path"
        ).fetchall()
        versions = connection.execute(
            "SELECT item_id, sha256, reason FROM item_versions ORDER BY item_id"
        ).fetchall()
    assert [row[1] for row in items] == [item.path for item in plan.files]
    assert versions == [
        (items[0][0], items[0][2], "existing-master-registration"),
        (items[1][0], items[1][2], "existing-master-registration"),
    ]


def test_registration_skips_existing_items_and_registers_only_missing(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "vault"
    first, second = make_master(root)
    monkeypatch.setattr(registration, "inspect_media", lambda _path: None)
    database = root / ".muse" / "muse.db"
    database.parent.mkdir()
    with sqlite3.connect(database) as connection:
        ledger.initialize_database(connection)
        ledger.admit(connection, [(first.relative_to(root), first.stat().st_size, "a" * 64)])

    plan = registration.make_plan(root, root / "master")

    assert plan.already_registered == (first.relative_to(root).as_posix(),)
    assert [item.path for item in plan.files] == [second.relative_to(root).as_posix()]


def test_registration_apply_refuses_changed_or_added_audio(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "vault"
    first, _second = make_master(root)
    monkeypatch.setattr(registration, "inspect_media", lambda _path: None)
    registration.make_plan(root, root / "master")
    first.write_bytes(b"changed")

    with pytest.raises(ValueError, match="content has changed"):
        registration.apply_plan(root)

    first.write_bytes(b"first audio")
    (first.parent / "03.flac").write_bytes(b"new")
    with pytest.raises(ValueError, match="paths have changed"):
        registration.apply_plan(root)


def test_registration_resumes_after_item_rows_were_committed(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "vault"
    make_master(root)
    monkeypatch.setattr(registration, "inspect_media", lambda _path: None)
    plan = registration.make_plan(root, root / "master")
    database = root / ".muse" / "muse.db"
    with sqlite3.connect(database) as connection:
        ledger.initialize_database(connection)
        ids = ledger.admit(
            connection,
            ((Path(item.path), item.size, item.sha256) for item in plan.files),
            reason="existing-master-registration",
            observed_at=plan.created_at,
        )
    registration._write(registration.plan_path(root), replace(plan, state="applying"))

    completed = registration.apply_plan(root)

    assert completed.state == "completed"
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT count(*) FROM library_items").fetchone()[0] == 2
        assert connection.execute("SELECT count(*) FROM item_versions").fetchone()[0] == 2
        rows = connection.execute("SELECT id FROM library_items ORDER BY id")
        assert [row[0] for row in rows] == list(ids)


def test_registration_rejects_paths_outside_master_and_symlinks(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "vault"
    first, _second = make_master(root)
    monkeypatch.setattr(registration, "inspect_media", lambda _path: None)
    outside = root / "backlog"
    outside.mkdir()

    with pytest.raises(ValueError, match="beneath"):
        registration.make_plan(root, outside)

    (first.parent / "linked.flac").symlink_to(first)
    with pytest.raises(registration.RegistrationValidationError, match="symbolic links"):
        registration.make_plan(root, root / "master")
    assert not registration.plan_path(root).exists()
