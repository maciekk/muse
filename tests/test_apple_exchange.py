import json
from dataclasses import replace
from hashlib import sha256
from pathlib import Path

import pytest

from muse.apple_exchange import (
    REQUEST_MANIFEST,
    RESULT_MANIFEST,
    export_request_bundle,
    export_result_bundle,
    import_result_bundle,
    load_request_bundle,
)
from muse.apple_fake_bridge import FakeAppleBridge
from muse.apple_protocol import (
    AppleOperation,
    AppleRequest,
    ProtocolValidationError,
    StagedFile,
    TrackMetadata,
)

BATCH_ID = "11111111-1111-4111-8111-111111111111"
OPERATION_ID = "22222222-2222-4222-8222-222222222222"
BRIDGE_ID = "33333333-3333-4333-8333-333333333333"


def inspect_request() -> AppleRequest:
    return AppleRequest(
        batch_id=BATCH_ID,
        created_at="2026-03-21T10:00:00+00:00",
        operations=(AppleOperation(OPERATION_ID, "inspect-library"),),
    )


def add_request(content: bytes) -> AppleRequest:
    return AppleRequest(
        batch_id=BATCH_ID,
        created_at="2026-03-21T10:00:00+00:00",
        operations=(
            AppleOperation(
                operation_id=OPERATION_ID,
                action="add-track",
                item_id=42,
                source_sha256="a" * 64,
                staged_file=StagedFile(
                    path="media/42.m4a",
                    size=len(content),
                    sha256=sha256(content).hexdigest(),
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


def fake_bridge() -> FakeAppleBridge:
    return FakeAppleBridge(
        bridge_id=BRIDGE_ID,
        clock=lambda: "2026-03-21T10:05:00+00:00",
    )


def test_disconnected_inspection_round_trip(tmp_path: Path) -> None:
    request = inspect_request()
    request_bundle = tmp_path / "to-mac" / request.batch_id
    result_bundle = tmp_path / "from-mac" / request.batch_id

    exported = export_request_bundle(request_bundle, request)
    received = load_request_bundle(request_bundle)
    result = fake_bridge().handle(received)
    export_result_bundle(result_bundle, received, result)
    imported = import_result_bundle(result_bundle, request)

    assert exported.created
    assert exported.media_files == 0
    assert imported == result
    assert imported.library is not None
    assert imported.library.identity == "fake-library"


def test_request_bundle_copies_and_verifies_staged_media(tmp_path: Path) -> None:
    content = b"independent staged audio"
    source = tmp_path / "staging.m4a"
    source.write_bytes(content)
    request = add_request(content)
    bundle = tmp_path / "batch"

    report = export_request_bundle(bundle, request, {"media/42.m4a": source})
    loaded = load_request_bundle(bundle)

    assert loaded == request
    assert report.media_files == 1
    assert report.media_bytes == len(content)
    assert (bundle / "media" / "42.m4a").read_bytes() == content
    assert (bundle / REQUEST_MANIFEST).is_file()


def test_identical_bundle_exports_are_idempotent(tmp_path: Path) -> None:
    request = inspect_request()
    request_bundle = tmp_path / "request"
    first = export_request_bundle(request_bundle, request)
    second = export_request_bundle(request_bundle, request)
    result = fake_bridge().handle(request)
    result_bundle = tmp_path / "result"
    first_result = export_result_bundle(result_bundle, request, result)
    second_result = export_result_bundle(result_bundle, request, result)

    assert first.created and not second.created
    assert first_result.created and not second_result.created


def test_request_export_requires_exact_media_source_set(tmp_path: Path) -> None:
    request = add_request(b"audio")

    with pytest.raises(ValueError, match="missing media sources"):
        export_request_bundle(tmp_path / "missing", request)
    with pytest.raises(ValueError, match="unexpected media sources"):
        export_request_bundle(
            tmp_path / "extra",
            request,
            {
                "media/42.m4a": tmp_path / "absent",
                "media/extra.m4a": tmp_path / "absent",
            },
        )


def test_request_bundle_rejects_tampering_and_unexpected_content(tmp_path: Path) -> None:
    content = b"audio"
    source = tmp_path / "source.m4a"
    source.write_bytes(content)
    request = add_request(content)
    bundle = tmp_path / "batch"
    export_request_bundle(bundle, request, {"media/42.m4a": source})

    (bundle / "media" / "42.m4a").write_bytes(b"other")
    with pytest.raises(ValueError, match="does not match request"):
        load_request_bundle(bundle)

    (bundle / "media" / "42.m4a").write_bytes(content)
    (bundle / "notes.txt").write_text("unexpected")
    with pytest.raises(ValueError, match="unexpected files: notes.txt"):
        load_request_bundle(bundle)


def test_bundle_rejects_symlinks_and_reserved_media_paths(tmp_path: Path) -> None:
    request = inspect_request()
    bundle = tmp_path / "batch"
    export_request_bundle(bundle, request)
    (bundle / "link").symlink_to(bundle / REQUEST_MANIFEST)
    with pytest.raises(ValueError, match="unsupported symlink"):
        load_request_bundle(bundle)

    content = b"audio"
    invalid = add_request(content)
    operation = replace(
        invalid.operations[0],
        staged_file=replace(invalid.operations[0].staged_file, path=RESULT_MANIFEST),
    )
    invalid = replace(invalid, operations=(operation,))
    source = tmp_path / "source.m4a"
    source.write_bytes(content)
    with pytest.raises(ProtocolValidationError, match="reserved"):
        export_request_bundle(tmp_path / "reserved", invalid, {RESULT_MANIFEST: source})


def test_result_import_rejects_another_request_or_modified_result(tmp_path: Path) -> None:
    request = inspect_request()
    result = fake_bridge().handle(request)
    bundle = tmp_path / "result"
    export_result_bundle(bundle, request, result)

    other_request = replace(
        request,
        batch_id="44444444-4444-4444-8444-444444444444",
    )
    with pytest.raises(ProtocolValidationError, match="batch_id"):
        import_result_bundle(bundle, other_request)

    value = json.loads((bundle / RESULT_MANIFEST).read_text())
    value["request_sha256"] = "0" * 64
    (bundle / RESULT_MANIFEST).write_text(json.dumps(value))
    with pytest.raises(ProtocolValidationError, match="request content"):
        import_result_bundle(bundle, request)


def test_existing_different_bundle_is_not_overwritten(tmp_path: Path) -> None:
    destination = tmp_path / "bundle"
    first = inspect_request()
    export_request_bundle(destination, first)
    different = replace(
        first,
        batch_id="44444444-4444-4444-8444-444444444444",
    )

    with pytest.raises(ValueError, match="different request"):
        export_request_bundle(destination, different)

    assert load_request_bundle(destination) == first
