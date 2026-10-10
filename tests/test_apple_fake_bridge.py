from dataclasses import replace

import pytest

from muse.apple_fake_bridge import FakeAppleBridge
from muse.apple_protocol import (
    AppleOperation,
    AppleRequest,
    ProtocolValidationError,
    StagedFile,
    TrackMetadata,
    validate_result,
)

BRIDGE_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
INSPECT_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
ADD_ID = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
SECOND_ADD_ID = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"


def bridge() -> FakeAppleBridge:
    return FakeAppleBridge(
        bridge_id=BRIDGE_ID,
        native_tracks=7,
        clock=lambda: "2026-03-20T12:00:00+00:00",
    )


def inspect_operation() -> AppleOperation:
    return AppleOperation(operation_id=INSPECT_ID, action="inspect-library")


def add_operation(operation_id: str = ADD_ID, item_id: int = 42) -> AppleOperation:
    return AppleOperation(
        operation_id=operation_id,
        action="add-track",
        item_id=item_id,
        source_sha256="a" * 64,
        staged_file=StagedFile(f"media/{item_id}.m4a", 1234, "b" * 64),
        metadata=TrackMetadata(
            title=f"Track {item_id}",
            artist="Artist",
            album="Album",
            album_artist="Album Artist",
            track_number=1,
            track_total=1,
            disc_number=1,
            disc_total=1,
        ),
        playlist_memberships=("Muse — Master",),
    )


def request(*operations: AppleOperation) -> AppleRequest:
    return AppleRequest(
        batch_id="11111111-1111-4111-8111-111111111111",
        created_at="2026-03-20T11:00:00+00:00",
        operations=operations,
    )


def test_fake_bridge_reports_capabilities_and_library_without_mutation() -> None:
    fake = bridge()
    sent = request(inspect_operation())

    result = fake.handle(sent)

    validate_result(sent, result)
    assert result.bridge_id == BRIDGE_ID
    assert result.capabilities.supported_actions == ("inspect-library", "add-track")
    assert result.library is not None
    assert result.library.total_tracks == 7
    assert result.library.managed_tracks == 0
    assert result.operations[0].status == "succeeded"


def test_fake_bridge_adds_track_and_returns_stable_persistent_id() -> None:
    fake = bridge()
    operation = add_operation()
    sent = request(operation)

    result = fake.handle(sent)

    validate_result(sent, result)
    outcome = result.operations[0]
    assert outcome.status == "succeeded"
    assert outcome.apple_persistent_id is not None
    assert outcome.cloud_status == "waiting"
    assert fake.managed_items == {42: outcome.apple_persistent_id}
    assert result.library is not None
    assert result.library.total_tracks == 8
    assert result.library.managed_tracks == 1


def test_exact_batch_retry_returns_recorded_result_without_reexecution() -> None:
    fake = bridge()
    sent = request(add_operation())

    first = fake.handle(sent)
    second = fake.handle(sent)

    assert second is first
    assert fake.execution_count(ADD_ID) == 1
    assert len(fake.managed_items) == 1


def test_retry_after_interruption_replays_recorded_operation_and_continues() -> None:
    fake = bridge()
    first_add = add_operation()
    second_add = add_operation(SECOND_ADD_ID, 43)
    sent = request(first_add, second_add)
    fake.interrupt_after_operations = 1

    with pytest.raises(RuntimeError, match="after recording operation"):
        fake.handle(sent)
    assert fake.execution_count(ADD_ID) == 1
    assert fake.execution_count(SECOND_ADD_ID) == 0

    result = fake.handle(sent)

    validate_result(sent, result)
    assert [item.status for item in result.operations] == ["succeeded", "succeeded"]
    assert fake.execution_count(ADD_ID) == 1
    assert fake.execution_count(SECOND_ADD_ID) == 1
    assert len(fake.managed_items) == 2


def test_operation_replay_across_a_new_batch_does_not_execute_twice() -> None:
    fake = bridge()
    operation = add_operation()
    first_request = request(operation)
    first = fake.handle(first_request)
    second_request = replace(
        first_request,
        batch_id="22222222-2222-4222-8222-222222222222",
        created_at="2026-03-20T11:05:00+00:00",
    )

    second = fake.handle(second_request)

    validate_result(second_request, second)
    assert second.operations == first.operations
    assert second.request_sha256 != first.request_sha256
    assert fake.execution_count(ADD_ID) == 1


def test_reused_batch_or_operation_identity_cannot_change_meaning() -> None:
    fake = bridge()
    sent = request(add_operation())
    fake.handle(sent)

    changed_batch = replace(sent, created_at="2026-03-20T11:06:00+00:00")
    with pytest.raises(ProtocolValidationError, match="batch_id"):
        fake.handle(changed_batch)

    changed_operation = replace(add_operation(), source_sha256="c" * 64)
    changed_request = AppleRequest(
        batch_id="22222222-2222-4222-8222-222222222222",
        created_at="2026-03-20T11:06:00+00:00",
        operations=(changed_operation,),
    )
    with pytest.raises(ProtocolValidationError, match="operation ID"):
        fake.handle(changed_request)

    corrected_request = replace(changed_request, operations=(add_operation(),))
    corrected = fake.handle(corrected_request)
    validate_result(corrected_request, corrected)
    assert fake.execution_count(ADD_ID) == 1


def test_forced_failures_and_uncertainty_are_explicit_and_replayed() -> None:
    fake = bridge()
    failed = add_operation()
    uncertain = add_operation(SECOND_ADD_ID, 43)
    sent = request(failed, uncertain)
    fake.force_failure(ADD_ID, "music-app-rejected", "Music.app rejected the file")
    fake.force_uncertain(SECOND_ADD_ID, "import may have completed before interruption")

    first = fake.handle(sent)
    second = fake.handle(sent)

    assert first.operations[0].status == "failed"
    assert first.operations[0].error_code == "music-app-rejected"
    assert first.operations[1].status == "uncertain"
    assert first.operations[1].message == "import may have completed before interruption"
    assert second == first
    assert fake.managed_items == {}
    assert fake.execution_count(ADD_ID) == 1
    assert fake.execution_count(SECOND_ADD_ID) == 1


def test_fake_bridge_revalidates_directly_constructed_requests() -> None:
    fake = bridge()
    invalid = replace(add_operation(), playlist_memberships=("Personal",))

    with pytest.raises(ProtocolValidationError, match="unmanaged playlists"):
        fake.handle(request(invalid))
