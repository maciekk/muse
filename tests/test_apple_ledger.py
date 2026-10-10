import sqlite3
from dataclasses import replace

import pytest

from muse import apple_ledger
from muse.apple_protocol import (
    AppleLibraryObservation,
    AppleOperation,
    AppleRequest,
    AppleResult,
    BridgeCapabilities,
    OperationResult,
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
