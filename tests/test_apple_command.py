import json
from dataclasses import replace
from pathlib import Path

from muse.apple_exchange import export_request_bundle, export_result_bundle
from muse.apple_fake_bridge import FakeAppleBridge
from muse.apple_protocol import AppleOperation, AppleRequest
from muse.cli import main


def inspection_request() -> AppleRequest:
    return AppleRequest(
        batch_id="11111111-1111-4111-8111-111111111111",
        created_at="2026-10-10T18:24:08+00:00",
        operations=(
            AppleOperation(
                operation_id="22222222-2222-4222-8222-222222222222",
                action="inspect-library",
            ),
        ),
    )


def make_bundles(
    tmp_path: Path,
    *,
    failed: bool = False,
    batch_id: str = "11111111-1111-4111-8111-111111111111",
) -> tuple[Path, Path]:
    request = replace(inspection_request(), batch_id=batch_id)
    request_bundle = tmp_path / "request"
    result_bundle = tmp_path / "result"
    export_request_bundle(request_bundle, request)
    bridge = FakeAppleBridge(
        bridge_id="33333333-3333-4333-8333-333333333333",
        library_identity="library-id",
        library_name="Library",
        native_tracks=66,
        clock=lambda: "2026-10-10T18:44:49+00:00",
    )
    if failed:
        bridge.force_failure(
            request.operations[0].operation_id,
            "music-app-inspection-failed",
            "Automation permission denied",
        )
    result = bridge.handle(request)
    if failed:
        result = replace(result, library=None)
    export_result_bundle(result_bundle, request, result)
    return request_bundle, result_bundle


def test_apple_status_json_validates_and_reports_observation(tmp_path: Path, capsys) -> None:
    request_bundle, result_bundle = make_bundles(tmp_path)

    status = main(
        [
            "apple-status",
            str(request_bundle),
            str(result_bundle),
            "--json",
        ]
    )

    report = json.loads(capsys.readouterr().out)
    assert status == 0
    assert report["batch_id"] == inspection_request().batch_id
    assert report["bridge_id"] == "33333333-3333-4333-8333-333333333333"
    assert report["library"] == {
        "application_version": None,
        "identity": "library-id",
        "managed_tracks": 0,
        "name": "Library",
        "total_tracks": 66,
    }
    assert report["operations"][0]["status"] == "succeeded"
    assert report["state_created"] is False


def test_apple_status_human_output_uses_relative_external_paths(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    request_bundle, result_bundle = make_bundles(tmp_path)
    monkeypatch.chdir(tmp_path)

    status = main(
        [
            "--color",
            "never",
            "apple-status",
            request_bundle.name,
            result_bundle.name,
        ]
    )

    output = capsys.readouterr().out
    assert status == 0
    assert "Apple bridge observation" in output
    assert "Library tracks" in output
    assert "66" in output
    assert "Muse-managed tracks" in output
    assert "SUCCEEDED" in output
    assert "no Muse state was changed" in output


def test_apple_status_rejects_result_for_another_request(tmp_path: Path, capsys) -> None:
    first_request, first_result = make_bundles(tmp_path / "first")
    second_request, _second_result = make_bundles(
        tmp_path / "second",
        batch_id="44444444-4444-4444-8444-444444444444",
    )

    status = main(
        [
            "--color",
            "never",
            "apple-status",
            str(second_request),
            str(first_result),
        ]
    )

    assert status == 1
    assert "does not match request" in capsys.readouterr().err
    assert first_request.is_dir()


def test_apple_status_returns_failure_for_failed_bridge_operation(
    tmp_path: Path, capsys
) -> None:
    request_bundle, result_bundle = make_bundles(tmp_path, failed=True)

    status = main(
        [
            "--color",
            "never",
            "apple-status",
            str(request_bundle),
            str(result_bundle),
        ]
    )

    output = capsys.readouterr().out
    assert status == 1
    assert "FAILED" in output
    assert "Automation permission denied" in output
    assert "No library observation was returned" in output
