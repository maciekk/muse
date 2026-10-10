import hashlib
import json
import sqlite3
from pathlib import Path

from muse import apple_ledger, apple_publication, ledger
from muse.apple_exchange import export_result_bundle, load_request_bundle
from muse.apple_fake_bridge import FakeAppleBridge
from muse.media import MediaInfo


def library(tmp_path: Path, monkeypatch) -> tuple[Path, Path, int]:
    root = tmp_path / "vault"
    track = root / "master" / "Artist" / "Album" / "track.m4a"
    track.parent.mkdir(parents=True)
    track.write_bytes(b"canonical audio")
    digest = hashlib.sha256(track.read_bytes()).hexdigest()
    database = root / ".muse" / "muse.db"
    database.parent.mkdir(parents=True)
    with sqlite3.connect(database) as connection:
        ledger.initialize_database(connection)
        item_id = ledger.admit(
            connection,
            [(Path("master/Artist/Album/track.m4a"), track.stat().st_size, digest)],
            observed_at="2026-10-12T10:00:00+00:00",
        )[0]
    info = MediaInfo(
        container="m4a",
        codec="alac",
        duration_seconds=1.0,
        sample_rate=44100,
        channels=2,
        bit_depth=16,
        title="Track",
        artist="Artist",
        album="Album",
        album_artist="Artist",
        track_number=1,
        track_total=1,
        disc_number=1,
        disc_total=1,
        year="2026",
        release_id=None,
    )
    monkeypatch.setattr(apple_publication, "inspect_media", lambda _path: info)
    return root, track, item_id


def test_plans_exports_and_records_disconnected_publication(tmp_path: Path, monkeypatch) -> None:
    root, track, item_id = library(tmp_path, monkeypatch)

    plan = apple_publication.make_plan(root, track)

    assert plan.state == "ready"
    assert plan.items[0].item_id == item_id
    assert plan.items[0].conversion == "copy"
    exported = apple_publication.apply_plan(root)
    assert exported.state == "exported"
    assert exported.request_bundle is not None
    request_bundle = root / exported.request_bundle
    request = load_request_bundle(request_bundle)
    assert request.operations[0].item_id == item_id
    assert request.operations[0].playlist_memberships == ("Muse — Master",)

    bridge = FakeAppleBridge(
        bridge_id="33333333-3333-4333-8333-333333333333",
        library_identity="library-id",
        clock=lambda: "2026-10-12T10:05:00+00:00",
    )
    result = bridge.handle(request)
    result_bundle = tmp_path / "result"
    export_result_bundle(result_bundle, request, result)

    completed = apple_publication.record_result(root, result_bundle)

    assert completed.state == "completed"
    assert not apple_publication.plan_path(root).exists()
    with sqlite3.connect(root / ".muse" / "muse.db") as connection:
        assert apple_ledger.publication_decision(connection, item_id) == "include"
        mapping = connection.execute(
            "SELECT library_identity, source_sha256, local_copy_verified "
            "FROM apple_item_mappings WHERE item_id = ?",
            (item_id,),
        ).fetchone()
    assert mapping == ("library-id", hashlib.sha256(track.read_bytes()).hexdigest(), 0)


def test_uncertain_result_remains_current_for_reconciliation(tmp_path: Path, monkeypatch) -> None:
    root, track, _item_id = library(tmp_path, monkeypatch)
    plan = apple_publication.make_plan(root, track)
    exported = apple_publication.apply_plan(root)
    request = load_request_bundle(root / str(exported.request_bundle))
    bridge = FakeAppleBridge(
        bridge_id="33333333-3333-4333-8333-333333333333",
        library_identity="library-id",
    )
    bridge.force_uncertain(
        plan.items[0].operation_id,
        "import may have completed before interruption",
    )
    result_bundle = tmp_path / "uncertain-result"
    export_result_bundle(result_bundle, request, bridge.handle(request))

    returned = apple_publication.record_result(root, result_bundle)

    assert returned.state == "uncertain"
    assert apple_publication.load_plan(root).state == "uncertain"
    assert (root / str(exported.request_bundle)).is_dir()


def test_explicit_exclusion_survives_broad_publication_plan(tmp_path: Path, monkeypatch) -> None:
    root, track, item_id = library(tmp_path, monkeypatch)

    exclusion = apple_publication.make_plan(root, track, exclude=True)
    assert exclusion.mode == "exclude"
    apple_publication.apply_plan(root)

    broad = apple_publication.make_plan(root, root / "master")

    assert broad.items == ()
    assert broad.excluded_paths == ("master/Artist/Album/track.m4a",)
    with sqlite3.connect(root / ".muse" / "muse.db") as connection:
        assert apple_ledger.publication_decision(connection, item_id) == "exclude"


def test_include_excluded_is_deliberate_override(tmp_path: Path, monkeypatch) -> None:
    root, track, item_id = library(tmp_path, monkeypatch)
    with sqlite3.connect(root / ".muse" / "muse.db") as connection:
        ledger.initialize_database(connection)
        apple_ledger.set_publication_decision(connection, item_id, "exclude")

    plan = apple_publication.make_plan(root, track, include_excluded=True)

    assert [item.item_id for item in plan.items] == [item_id]
    assert plan.excluded_paths == ()


def test_changed_registered_source_blocks_plan(tmp_path: Path, monkeypatch) -> None:
    root, track, _item_id = library(tmp_path, monkeypatch)
    track.write_bytes(b"changed")

    try:
        apple_publication.make_plan(root, track)
    except apple_publication.PublicationValidationError as error:
        assert "does not match its admitted version" in str(error)
    else:
        raise AssertionError("changed source unexpectedly entered a publication plan")


def test_unregistered_master_audio_blocks_selection(tmp_path: Path, monkeypatch) -> None:
    root, track, _item_id = library(tmp_path, monkeypatch)
    extra = track.with_name("extra.m4a")
    extra.write_bytes(b"unregistered")

    try:
        apple_publication.make_plan(root, track.parent)
    except apple_publication.PublicationValidationError as error:
        assert "Master audio is not registered" in str(error)
    else:
        raise AssertionError("unregistered Master audio was silently ignored")


def test_alac_staging_uses_ffmpeg_without_changing_source(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "track.flac"
    destination = tmp_path / "staging" / "track.m4a"
    source.write_bytes(b"source")
    commands: list[list[str]] = []

    class Result:
        returncode = 0
        stderr = b""

    def run(command, **_kwargs):
        commands.append(command)
        Path(command[-1]).write_bytes(b"converted")
        return Result()

    monkeypatch.setattr(apple_publication.subprocess, "run", run)

    apple_publication._stage_item(source, destination, "alac")

    assert source.read_bytes() == b"source"
    assert destination.read_bytes() == b"converted"
    assert "alac" in commands[0]
    assert commands[0][commands[0].index("-f") + 1] == "ipod"


def test_plan_document_is_portable_json(tmp_path: Path, monkeypatch) -> None:
    root, track, _item_id = library(tmp_path, monkeypatch)
    plan = apple_publication.make_plan(root, track)

    document = json.loads(apple_publication.plan_path(root).read_text())

    assert document["batch_id"] == plan.batch_id
    assert document["items"][0]["path"] == "master/Artist/Album/track.m4a"
