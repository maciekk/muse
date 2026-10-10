import json
from dataclasses import replace

import pytest

from muse.apple_protocol import (
    PROTOCOL_NAME,
    PROTOCOL_VERSION,
    AppleLibraryObservation,
    AppleOperation,
    AppleRequest,
    AppleResult,
    BridgeCapabilities,
    OperationResult,
    ProtocolValidationError,
    StagedFile,
    TrackMetadata,
    new_id,
    validate_result,
)


def inspect_request() -> AppleRequest:
    return AppleRequest(
        batch_id="11111111-1111-4111-8111-111111111111",
        created_at="2026-03-20T10:00:00+00:00",
        operations=(
            AppleOperation(
                operation_id="22222222-2222-4222-8222-222222222222",
                action="inspect-library",
            ),
        ),
    )


def add_request() -> AppleRequest:
    return AppleRequest(
        batch_id="33333333-3333-4333-8333-333333333333",
        created_at="2026-03-20T10:01:00Z",
        operations=(
            AppleOperation(
                operation_id="44444444-4444-4444-8444-444444444444",
                action="add-track",
                item_id=42,
                source_sha256="a" * 64,
                staged_file=StagedFile("media/42.m4a", 1234, "b" * 64),
                metadata=TrackMetadata(
                    title="Track",
                    artist="Artist",
                    album="Album",
                    album_artist="Album Artist",
                    track_number=2,
                    track_total=10,
                    disc_number=1,
                    disc_total=1,
                    genre="Game",
                    year="2001",
                ),
                playlist_memberships=("Muse — Master",),
            ),
        ),
    )


def capabilities() -> BridgeCapabilities:
    return BridgeCapabilities(
        bridge_version="0.1.0",
        supported_protocol_versions=(1,),
        supported_actions=("inspect-library", "add-track"),
        readable_properties=("persistent-id", "cloud-status"),
        writable_properties=("title",),
    )


def test_request_round_trip_has_stable_canonical_digest() -> None:
    request = add_request()

    loaded = AppleRequest.from_json(request.to_json())
    reordered = json.dumps(request.to_dict(), separators=(",", ":"))

    assert loaded == request
    assert AppleRequest.from_json(reordered).sha256 == request.sha256
    assert len(request.sha256) == 64
    assert request.to_dict()["protocol"] == PROTOCOL_NAME
    assert request.to_dict()["protocol_version"] == PROTOCOL_VERSION


def test_request_rejects_unsafe_or_incomplete_add_operations() -> None:
    value = add_request().to_dict()
    value["operations"][0]["staged_file"]["path"] = "../outside.m4a"
    with pytest.raises(ProtocolValidationError, match="normalized relative POSIX path"):
        AppleRequest.from_dict(value)

    value = add_request().to_dict()
    value["operations"][0]["metadata"] = None
    with pytest.raises(ProtocolValidationError, match="add-track requires"):
        AppleRequest.from_dict(value)

    value = add_request().to_dict()
    value["operations"][0]["source_sha256"] = "A" * 64
    with pytest.raises(ProtocolValidationError, match="lowercase SHA-256"):
        AppleRequest.from_dict(value)

    value = add_request().to_dict()
    value["operations"][0]["playlist_memberships"] = ["Personal playlist"]
    with pytest.raises(ProtocolValidationError, match="unmanaged playlists"):
        AppleRequest.from_dict(value)


def test_request_rejects_unknown_fields_versions_and_duplicate_operation_ids() -> None:
    value = inspect_request().to_dict()
    value["surprise"] = True
    with pytest.raises(ProtocolValidationError, match="unknown fields: surprise"):
        AppleRequest.from_dict(value)

    value = inspect_request().to_dict()
    value["protocol_version"] = 2
    with pytest.raises(ProtocolValidationError, match="unsupported version"):
        AppleRequest.from_dict(value)

    value = inspect_request().to_dict()
    value["operations"].append(dict(value["operations"][0]))
    with pytest.raises(ProtocolValidationError, match="operation IDs must be unique"):
        AppleRequest.from_dict(value)


def test_result_round_trip_and_request_correlation() -> None:
    request = add_request()
    result = AppleResult(
        batch_id=request.batch_id,
        request_sha256=request.sha256,
        bridge_id="55555555-5555-4555-8555-555555555555",
        observed_at="2026-03-20T10:02:00+00:00",
        capabilities=capabilities(),
        library=AppleLibraryObservation("library-1", "Music", 100, 1),
        operations=(
            OperationResult(
                operation_id=request.operations[0].operation_id,
                status="succeeded",
                item_id=42,
                apple_persistent_id="ABCDEF1234567890",
                cloud_status="waiting",
            ),
        ),
    )

    loaded = AppleResult.from_json(result.to_json())

    assert loaded == result
    validate_result(request, loaded)


def test_result_accepts_explicit_uncertainty() -> None:
    request = add_request()
    result = AppleResult(
        batch_id=request.batch_id,
        request_sha256=request.sha256,
        bridge_id="55555555-5555-4555-8555-555555555555",
        observed_at="2026-03-20T10:02:00+00:00",
        capabilities=capabilities(),
        library=None,
        operations=(
            OperationResult(
                operation_id=request.operations[0].operation_id,
                status="uncertain",
                item_id=42,
                message="Music.app returned before the persistent ID was recorded",
            ),
        ),
    )

    loaded = AppleResult.from_json(result.to_json())
    validate_result(request, loaded)
    assert loaded.operations[0].status == "uncertain"


def test_result_rejects_unexplained_failure_and_mismatched_request() -> None:
    request = inspect_request()
    value = {
        "operation_id": request.operations[0].operation_id,
        "status": "failed",
        "item_id": None,
        "apple_persistent_id": None,
        "cloud_status": None,
        "error_code": None,
        "message": None,
    }
    with pytest.raises(ProtocolValidationError, match="require a message"):
        OperationResult.from_dict(value)

    result = AppleResult(
        batch_id=request.batch_id,
        request_sha256=request.sha256,
        bridge_id="55555555-5555-4555-8555-555555555555",
        observed_at="2026-03-20T10:02:00+00:00",
        capabilities=capabilities(),
        library=AppleLibraryObservation(None, None, 0, 0),
        operations=(
            OperationResult(
                operation_id=request.operations[0].operation_id,
                status="succeeded",
            ),
        ),
    )
    with pytest.raises(ProtocolValidationError, match="request content"):
        validate_result(request, replace(result, request_sha256="0" * 64))


def test_result_requires_persistent_id_after_successful_add() -> None:
    request = add_request()
    result = AppleResult(
        batch_id=request.batch_id,
        request_sha256=request.sha256,
        bridge_id="55555555-5555-4555-8555-555555555555",
        observed_at="2026-03-20T10:02:00+00:00",
        capabilities=capabilities(),
        library=None,
        operations=(
            OperationResult(
                operation_id=request.operations[0].operation_id,
                status="succeeded",
                item_id=42,
            ),
        ),
    )
    with pytest.raises(ProtocolValidationError, match="required after a successful add-track"):
        validate_result(request, result)


def test_new_id_is_accepted_as_canonical_identity() -> None:
    value = inspect_request().to_dict()
    value["batch_id"] = new_id()
    value["operations"][0]["operation_id"] = new_id()

    assert AppleRequest.from_dict(value).batch_id == value["batch_id"]
