import json
import sqlite3
import subprocess
from dataclasses import replace
from hashlib import sha256
from pathlib import Path

import pytest

from muse.apple_exchange import export_request_bundle, import_result_bundle, load_request_bundle
from muse.apple_macos_bridge import (
    BridgeJournal,
    MusicAppProbe,
    MusicProbeError,
    apply_bundle,
    main,
)
from muse.apple_protocol import (
    AppleLibraryObservation,
    AppleOperation,
    AppleRequest,
    ProtocolValidationError,
    StagedFile,
    TrackMetadata,
    validate_result,
)

BATCH_ID = "11111111-1111-4111-8111-111111111111"
OPERATION_ID = "22222222-2222-4222-8222-222222222222"


class StubProbe:
    def __init__(self, *, error: str | None = None) -> None:
        self.calls = 0
        self.error = error

    def inspect(self) -> AppleLibraryObservation:
        self.calls += 1
        if self.error:
            raise MusicProbeError(self.error)
        return AppleLibraryObservation(
            identity="library-id",
            name="Library",
            total_tracks=12,
            managed_tracks=2,
            application_version="1.5",
        )


def inspect_request() -> AppleRequest:
    return AppleRequest(
        batch_id=BATCH_ID,
        created_at="2026-03-21T12:00:00+00:00",
        operations=(AppleOperation(OPERATION_ID, "inspect-library"),),
    )


def add_request(content: bytes) -> AppleRequest:
    return AppleRequest(
        batch_id=BATCH_ID,
        created_at="2026-03-21T12:00:00+00:00",
        operations=(
            AppleOperation(
                operation_id=OPERATION_ID,
                action="add-track",
                item_id=42,
                source_sha256="a" * 64,
                staged_file=StagedFile(
                    "media/42.m4a", len(content), sha256(content).hexdigest()
                ),
                metadata=TrackMetadata(
                    title="Track",
                    artist="Artist",
                    album="Album",
                    album_artist="Album Artist",
                    track_number=1,
                    track_total=1,
                    disc_number=1,
                    disc_total=1,
                ),
                playlist_memberships=("Muse — Master",),
            ),
        ),
    )


def test_local_bundle_probe_writes_correlated_result_and_journal(tmp_path: Path) -> None:
    request = inspect_request()
    request_bundle = tmp_path / "request"
    result_bundle = tmp_path / "result"
    state = tmp_path / "journal.db"
    export_request_bundle(request_bundle, request)
    probe = StubProbe()

    result, exported = apply_bundle(
        request_bundle,
        result_bundle,
        state_path=state,
        probe=probe,
    )
    imported = import_result_bundle(result_bundle, request)

    validate_result(request, result)
    assert imported == result
    assert exported.created
    assert probe.calls == 1
    assert result.library is not None
    assert result.library.application_version == "1.5"
    assert result.capabilities.supported_actions == ("inspect-library",)
    with sqlite3.connect(state) as connection:
        assert connection.execute("SELECT state FROM batches").fetchone() == ("completed",)
        assert connection.execute("SELECT state FROM operations").fetchone() == ("completed",)


def test_completed_request_replays_without_contacting_music_again(tmp_path: Path) -> None:
    request = inspect_request()
    state = tmp_path / "journal.db"
    first_probe = StubProbe()
    with BridgeJournal(state) as journal:
        first = journal.handle(request, first_probe)
        bridge_id = journal.bridge_id

    second_probe = StubProbe(error="must not be called")
    with BridgeJournal(state) as journal:
        second = journal.handle(request, second_probe)
        assert journal.bridge_id == bridge_id

    assert second == first
    assert first_probe.calls == 1
    assert second_probe.calls == 0


def test_interrupted_read_only_operation_is_safely_retried(tmp_path: Path, monkeypatch) -> None:
    request = inspect_request()
    state = tmp_path / "journal.db"
    probe = StubProbe()
    with BridgeJournal(state) as journal:
        original = journal._record_operation

        def interrupted(*_args):
            raise RuntimeError("stopped after Music.app observation")

        monkeypatch.setattr(journal, "_record_operation", interrupted)
        with pytest.raises(RuntimeError, match="stopped"):
            journal.handle(request, probe)
        monkeypatch.setattr(journal, "_record_operation", original)

    with BridgeJournal(state) as journal:
        result = journal.handle(request, probe)

    assert result.operations[0].status == "succeeded"
    assert probe.calls == 2


def test_recorded_observation_completes_batch_without_requery(tmp_path: Path, monkeypatch) -> None:
    request = inspect_request()
    state = tmp_path / "journal.db"
    first_probe = StubProbe()
    with BridgeJournal(state) as journal:
        original = journal._complete_batch

        def interrupted(*_args):
            raise RuntimeError("stopped before batch completion")

        monkeypatch.setattr(journal, "_complete_batch", interrupted)
        with pytest.raises(RuntimeError, match="batch completion"):
            journal.handle(request, first_probe)
        monkeypatch.setattr(journal, "_complete_batch", original)

    second_probe = StubProbe(error="must not be called")
    with BridgeJournal(state) as journal:
        result = journal.handle(request, second_probe)

    assert result.operations[0].status == "succeeded"
    assert result.library is not None
    assert result.library.identity == "library-id"
    assert first_probe.calls == 1
    assert second_probe.calls == 0


def test_applying_mutation_becomes_uncertain_instead_of_reexecuting(tmp_path: Path) -> None:
    content = b"audio"
    request = add_request(content)
    state = tmp_path / "journal.db"
    with BridgeJournal(state) as journal:
        now = "2026-03-21T12:00:00+00:00"
        operation = request.operations[0]
        journal.connection.execute(
            """
            INSERT INTO operations
                (operation_id, request_json, state, result_json, created_at, updated_at)
            VALUES (?, ?, 'applying', NULL, ?, ?)
            """,
            (
                operation.operation_id,
                json.dumps(
                    operation.to_dict(),
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ),
                now,
                now,
            ),
        )
        journal.connection.commit()
        result = journal.handle(request, StubProbe())

    outcome = result.operations[0]
    assert outcome.status == "uncertain"
    assert "previous bridge process stopped" in (outcome.message or "")


def test_probe_failure_is_a_durable_structured_result(tmp_path: Path) -> None:
    request = inspect_request()
    state = tmp_path / "journal.db"
    probe = StubProbe(error="Automation permission denied")

    with BridgeJournal(state) as journal:
        first = journal.handle(request, probe)
    with BridgeJournal(state) as journal:
        second = journal.handle(request, StubProbe())

    assert first.operations[0].status == "failed"
    assert first.operations[0].error_code == "music-app-inspection-failed"
    assert first.operations[0].message == "Automation permission denied"
    assert second == first


def test_batch_and_operation_identity_reuse_is_rejected(tmp_path: Path) -> None:
    state = tmp_path / "journal.db"
    request = inspect_request()
    with BridgeJournal(state) as journal:
        journal.handle(request, StubProbe())

        changed_batch = replace(request, created_at="2026-03-21T12:01:00+00:00")
        with pytest.raises(ProtocolValidationError, match="batch_id"):
            journal.handle(changed_batch, StubProbe())

        changed_request = replace(
            add_request(b"audio"),
            batch_id="44444444-4444-4444-8444-444444444444",
        )
        with pytest.raises(ProtocolValidationError, match="operation ID"):
            journal.handle(changed_request, StubProbe())


def test_music_app_probe_parses_read_only_osascript_results(monkeypatch) -> None:
    monkeypatch.setattr("muse.apple_macos_bridge.sys.platform", "darwin")

    def run(command, **_kwargs):
        script = command[-1]
        if "count of every track of library playlist" in script:
            output = "10\n"
        elif "Muse — Master" in script:
            output = "2\n"
        elif "Muse — Stopgap" in script:
            output = "1\n"
        elif "persistent ID" in script:
            output = "ABCDEF\n"
        elif "name of library" in script:
            output = "Library\n"
        elif "version" in script:
            output = "1.5\n"
        else:
            raise AssertionError(script)
        return subprocess.CompletedProcess(command, 0, output, "")

    monkeypatch.setattr("muse.apple_macos_bridge.subprocess.run", run)

    observed = MusicAppProbe().inspect()

    assert observed == AppleLibraryObservation("ABCDEF", "Library", 10, 3, "1.5")


def test_bridge_cli_reports_probe_failure_without_crashing(tmp_path: Path) -> None:
    request_bundle = tmp_path / "request"
    result_bundle = tmp_path / "result"
    state = tmp_path / "journal.db"
    export_request_bundle(request_bundle, inspect_request())

    status = main(
        ["apply", str(request_bundle), str(result_bundle), "--state", str(state)]
    )

    assert status == 1
    result = import_result_bundle(result_bundle, inspect_request())
    assert result.operations[0].status == "failed"
    assert "must run on macOS" in (result.operations[0].message or "")


def test_bridge_cli_prepares_fresh_inspection_bundle(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"

    assert main(["prepare-inspection", str(first)]) == 0
    assert main(["prepare-inspection", str(second)]) == 0

    first_request = load_request_bundle(first)
    second_request = load_request_bundle(second)
    assert first_request.batch_id != second_request.batch_id
    assert first_request.operations[0].action == "inspect-library"
    assert first_request.operations[0].operation_id != second_request.operations[0].operation_id
