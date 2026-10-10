import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from muse import apple_ledger, ledger
from muse.apple_protocol import (
    AppleLibraryObservation,
    AppleOperation,
    AppleRequest,
    AppleResult,
    BridgeCapabilities,
    OperationResult,
    StagedFile,
    TrackMetadata,
)


def documents() -> tuple[AppleRequest, AppleResult]:
    operation = AppleOperation(
        operation_id="22222222-2222-4222-8222-222222222222",
        action="inspect-library",
    )
    request = AppleRequest(
        batch_id="11111111-1111-4111-8111-111111111111",
        created_at="2026-10-10T18:24:08+00:00",
        operations=(operation,),
    )
    result = AppleResult(
        batch_id=request.batch_id,
        request_sha256=request.sha256,
        bridge_id="33333333-3333-4333-8333-333333333333",
        observed_at="2026-10-10T18:44:49+00:00",
        capabilities=BridgeCapabilities("0.1.0", (1,), ("inspect-library",)),
        library=AppleLibraryObservation("library-id", "Library", 66, 3, "1.6.6"),
        operations=(OperationResult(operation.operation_id, "succeeded"),),
    )
    return request, result


def test_records_observation_and_accepts_exact_retry() -> None:
    request, result = documents()
    connection = sqlite3.connect(":memory:")

    first = apple_ledger.record_observation(
        connection, request, result, recorded_at="2026-10-10T19:00:00+00:00"
    )
    retry = apple_ledger.record_observation(connection, request, result)

    assert first.created is True
    assert retry == apple_ledger.RecordedObservation(first.observation_id, False)
    row = connection.execute(
        """
        SELECT batch_id, library_identity, total_tracks, managed_tracks,
               application_version, recorded_at
        FROM apple_library_observations
        """
    ).fetchone()
    assert row == (
        request.batch_id,
        "library-id",
        66,
        3,
        "1.6.6",
        "2026-10-10T19:00:00+00:00",
    )


def test_preserves_observation_history_across_batches() -> None:
    request, result = documents()
    second_request = replace(request, batch_id="44444444-4444-4444-8444-444444444444")
    second_result = replace(
        result,
        batch_id=second_request.batch_id,
        request_sha256=second_request.sha256,
        observed_at="2026-10-11T18:44:49+00:00",
        library=replace(result.library, total_tracks=67),
    )
    connection = sqlite3.connect(":memory:")

    apple_ledger.record_observation(connection, request, result)
    apple_ledger.record_observation(connection, second_request, second_result)

    assert connection.execute("SELECT count(*) FROM apple_library_observations").fetchone()[0] == 2


def publication_documents(
    item_id: int,
    *,
    status: str = "succeeded",
) -> tuple[AppleRequest, AppleResult]:
    operation = AppleOperation(
        operation_id="66666666-6666-4666-8666-666666666666",
        action="add-track",
        item_id=item_id,
        source_sha256="a" * 64,
        staged_file=StagedFile("media/track.m4a", 123, "b" * 64),
        metadata=TrackMetadata(
            title="Track",
            artist="Artist",
            album="Album",
            album_artist="Artist",
            track_number=1,
            track_total=1,
            disc_number=1,
            disc_total=1,
        ),
        playlist_memberships=("Muse — Master",),
    )
    request = AppleRequest(
        batch_id="55555555-5555-4555-8555-555555555555",
        created_at="2026-10-11T12:00:00+00:00",
        operations=(operation,),
    )
    outcome = (
        OperationResult(
            operation.operation_id,
            "succeeded",
            item_id=item_id,
            apple_persistent_id="APPLE-ID",
        )
        if status == "succeeded"
        else OperationResult(
            operation.operation_id,
            "failed",
            item_id=item_id,
            error_code="import-failed",
            message="Music.app rejected the file",
        )
    )
    result = AppleResult(
        batch_id=request.batch_id,
        request_sha256=request.sha256,
        bridge_id="33333333-3333-4333-8333-333333333333",
        observed_at="2026-10-11T12:05:00+00:00",
        capabilities=BridgeCapabilities("0.1.0", (1,), ("add-track",)),
        library=AppleLibraryObservation("library-id", "Library", 67, 1, "1.6.6"),
        operations=(outcome,),
    )
    return request, result


def admitted_item(connection: sqlite3.Connection) -> int:
    ledger.initialize_database(connection)
    return ledger.admit(
        connection,
        [(Path("master/Artist/Album/track.flac"), 456, "a" * 64)],
        observed_at="2026-10-11T11:00:00+00:00",
    )[0]


def test_rejects_failed_inspection_and_conflicting_batch() -> None:
    request, result = documents()
    connection = sqlite3.connect(":memory:")
    apple_ledger.record_observation(connection, request, result)

    conflict = replace(result, observed_at="2026-10-10T19:44:49+00:00")
    with pytest.raises(ValueError, match="conflicts with durable state"):
        apple_ledger.record_observation(connection, request, conflict)

    failed = replace(
        result,
        library=None,
        operations=(
            OperationResult(
                request.operations[0].operation_id,
                "failed",
                error_code="inspection-failed",
                message="permission denied",
            ),
        ),
    )
    with pytest.raises(ValueError, match="no successful"):
        apple_ledger.record_observation(connection, request, failed)


def test_publication_decisions_are_explicit_and_replaceable() -> None:
    connection = sqlite3.connect(":memory:")
    item_id = admitted_item(connection)

    assert apple_ledger.publication_decision(connection, item_id) is None
    apple_ledger.set_publication_decision(
        connection, item_id, "exclude", decided_at="2026-10-11T11:10:00+00:00"
    )
    assert apple_ledger.publication_decision(connection, item_id) == "exclude"
    apple_ledger.set_publication_decision(
        connection, item_id, "include", decided_at="2026-10-11T11:20:00+00:00"
    )

    assert connection.execute(
        "SELECT decision, decided_at FROM apple_publication_decisions WHERE item_id = ?",
        (item_id,),
    ).fetchone() == ("include", "2026-10-11T11:20:00+00:00")
    with pytest.raises(ValueError, match="unknown library item"):
        apple_ledger.set_publication_decision(connection, 999, "include")


def test_publication_run_requires_inclusion_and_current_source() -> None:
    connection = sqlite3.connect(":memory:")
    item_id = admitted_item(connection)
    request, _result = publication_documents(item_id)

    with pytest.raises(ValueError, match="not explicitly included"):
        apple_ledger.create_publication_run(connection, request)
    apple_ledger.set_publication_decision(connection, item_id, "exclude")
    with pytest.raises(ValueError, match="explicitly excluded"):
        apple_ledger.create_publication_run(connection, request)
    apple_ledger.set_publication_decision(connection, item_id, "include")
    stale = replace(
        request,
        operations=(replace(request.operations[0], source_sha256="c" * 64),),
    )
    with pytest.raises(ValueError, match="source version is stale"):
        apple_ledger.create_publication_run(connection, stale)


def test_records_publication_run_outcome_and_mapping_idempotently() -> None:
    connection = sqlite3.connect(":memory:")
    item_id = admitted_item(connection)
    apple_ledger.set_publication_decision(connection, item_id, "include")
    request, result = publication_documents(item_id)

    first = apple_ledger.create_publication_run(
        connection, request, recorded_at="2026-10-11T12:01:00+00:00"
    )
    retry = apple_ledger.create_publication_run(connection, request)
    result_run_id = apple_ledger.record_publication_result(
        connection, request, result, completed_at="2026-10-11T12:06:00+00:00"
    )
    result_retry = apple_ledger.record_publication_result(connection, request, result)

    assert first.created is True
    assert retry == apple_ledger.RecordedPublicationRun(first.run_id, False)
    assert result_run_id == result_retry == first.run_id
    assert connection.execute(
        """
        SELECT status, apple_persistent_id, source_sha256, staging_sha256
        FROM apple_publication_outcomes
        """
    ).fetchone() == ("succeeded", "APPLE-ID", "a" * 64, "b" * 64)
    assert connection.execute(
        """
        SELECT item_id, library_identity, apple_persistent_id, source_sha256,
               staging_sha256, first_published_at, last_verified_at,
               local_file_location, local_copy_verified
        FROM apple_item_mappings
        """
    ).fetchone() == (
        item_id,
        "library-id",
        "APPLE-ID",
        "a" * 64,
        "b" * 64,
        result.observed_at,
        result.observed_at,
        None,
        0,
    )


def test_failed_publication_is_retained_without_mapping() -> None:
    connection = sqlite3.connect(":memory:")
    item_id = admitted_item(connection)
    apple_ledger.set_publication_decision(connection, item_id, "include")
    request, result = publication_documents(item_id, status="failed")
    apple_ledger.create_publication_run(connection, request)

    apple_ledger.record_publication_result(connection, request, result)

    assert connection.execute(
        "SELECT status, error_code, message FROM apple_publication_outcomes"
    ).fetchone() == ("failed", "import-failed", "Music.app rejected the file")
    assert connection.execute("SELECT count(*) FROM apple_item_mappings").fetchone()[0] == 0


def test_publication_result_requires_planned_run_and_library_identity() -> None:
    connection = sqlite3.connect(":memory:")
    item_id = admitted_item(connection)
    apple_ledger.set_publication_decision(connection, item_id, "include")
    request, result = publication_documents(item_id)

    with pytest.raises(ValueError, match="no durable run"):
        apple_ledger.record_publication_result(connection, request, result)
    apple_ledger.create_publication_run(connection, request)
    no_identity = replace(result, library=replace(result.library, identity=None))
    with pytest.raises(ValueError, match="library identity"):
        apple_ledger.record_publication_result(connection, request, no_identity)
