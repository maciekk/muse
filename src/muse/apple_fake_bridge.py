"""Deterministic in-process stand-in for the future macOS Apple bridge."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from muse.apple_protocol import (
    PROTOCOL_VERSION,
    AppleLibraryObservation,
    AppleOperation,
    AppleRequest,
    AppleResult,
    BridgeCapabilities,
    OperationResult,
    ProtocolValidationError,
    new_id,
    validate_result,
)


@dataclass(frozen=True)
class ForcedOutcome:
    status: str
    error_code: str | None
    message: str


class FakeAppleBridge:
    """Exercise bridge callers without macOS, Music.app, or a transport.

    Batch and operation journals model the duplicate-avoidance contract expected
    from the real bridge. They are intentionally in memory; persistence belongs to
    the macOS implementation.
    """

    def __init__(
        self,
        *,
        bridge_id: str | None = None,
        library_identity: str = "fake-library",
        library_name: str = "Fake Music Library",
        native_tracks: int = 0,
        clock: Callable[[], str] | None = None,
    ) -> None:
        if native_tracks < 0:
            raise ValueError("native_tracks must be non-negative")
        self.bridge_id = bridge_id or new_id()
        self.library_identity = library_identity
        self.library_name = library_name
        self.native_tracks = native_tracks
        self.clock = clock or (lambda: datetime.now(UTC).isoformat())
        self.capabilities = BridgeCapabilities(
            bridge_version="fake-1",
            supported_protocol_versions=(PROTOCOL_VERSION,),
            supported_actions=("inspect-library", "add-track"),
            readable_properties=(
                "library-identity",
                "persistent-id",
                "file-location",
            ),
            writable_properties=("managed-playlist-membership",),
        )
        self._batch_requests: dict[str, str] = {}
        self._batch_results: dict[str, AppleResult] = {}
        self._operation_requests: dict[str, AppleOperation] = {}
        self._operation_results: dict[str, OperationResult] = {}
        self._forced: dict[str, ForcedOutcome] = {}
        self._managed_items: dict[int, str] = {}
        self._execution_counts: dict[str, int] = {}
        self.interrupt_after_operations: int | None = None

    @property
    def managed_items(self) -> dict[int, str]:
        return dict(self._managed_items)

    def execution_count(self, operation_id: str) -> int:
        return self._execution_counts.get(operation_id, 0)

    def force_failure(self, operation_id: str, error_code: str, message: str) -> None:
        self._force(operation_id, ForcedOutcome("failed", error_code, message))

    def force_uncertain(self, operation_id: str, message: str) -> None:
        self._force(operation_id, ForcedOutcome("uncertain", None, message))

    def _force(self, operation_id: str, outcome: ForcedOutcome) -> None:
        if operation_id in self._operation_results:
            raise ValueError("cannot force an outcome after an operation has executed")
        self._forced[operation_id] = outcome

    def handle(self, request: AppleRequest) -> AppleResult:
        """Apply or replay one validated request and return a correlated result."""
        # Callers can construct dataclasses directly, so parse the emitted document
        # just as an external bridge would at its trust boundary.
        request = AppleRequest.from_dict(request.to_dict())
        previous_digest = self._batch_requests.get(request.batch_id)
        if previous_digest is not None and previous_digest != request.sha256:
            raise ProtocolValidationError(
                "request.batch_id: was already used for different request content"
            )
        previous = self._batch_results.get(request.batch_id)
        if previous is not None:
            return previous
        for operation in request.operations:
            recorded_operation = self._operation_requests.get(operation.operation_id)
            if recorded_operation is not None and recorded_operation != operation:
                raise ProtocolValidationError(
                    "request.operations: operation ID was already used for different content"
                )
        self._batch_requests[request.batch_id] = request.sha256

        outcomes: list[OperationResult] = []
        executed_this_call = 0
        for operation in request.operations:
            outcome = self._operation_results.get(operation.operation_id)
            if outcome is None:
                outcome = self._execute(operation)
                self._operation_requests[operation.operation_id] = operation
                self._operation_results[operation.operation_id] = outcome
                self._execution_counts[operation.operation_id] = 1
                executed_this_call += 1
                if self.interrupt_after_operations == executed_this_call:
                    self.interrupt_after_operations = None
                    raise RuntimeError("fake bridge interrupted after recording operation")
            outcomes.append(outcome)

        result = AppleResult(
            batch_id=request.batch_id,
            request_sha256=request.sha256,
            bridge_id=self.bridge_id,
            observed_at=self.clock(),
            capabilities=self.capabilities,
            library=self._library_observation(),
            operations=tuple(outcomes),
        )
        # Validate what the fake emits so tests cannot rely on behavior forbidden to
        # a real bridge.
        result = AppleResult.from_dict(result.to_dict())
        validate_result(request, result)
        self._batch_results[request.batch_id] = result
        return result

    def _execute(self, operation: AppleOperation) -> OperationResult:
        forced = self._forced.get(operation.operation_id)
        if forced is not None:
            return OperationResult(
                operation_id=operation.operation_id,
                status=forced.status,  # type: ignore[arg-type]
                item_id=operation.item_id,
                error_code=forced.error_code,
                message=forced.message,
            )
        if operation.action == "inspect-library":
            return OperationResult(operation.operation_id, "succeeded")
        if operation.action == "add-track":
            assert operation.item_id is not None
            persistent_id = self._managed_items.get(operation.item_id)
            if persistent_id is None:
                persistent_id = hashlib.sha256(
                    f"{self.bridge_id}:{operation.operation_id}".encode()
                ).hexdigest()[:16].upper()
                self._managed_items[operation.item_id] = persistent_id
            return OperationResult(
                operation_id=operation.operation_id,
                status="succeeded",
                item_id=operation.item_id,
                apple_persistent_id=persistent_id,
            )
        raise AssertionError(f"validated unsupported operation: {operation.action}")

    def _library_observation(self) -> AppleLibraryObservation:
        managed = len(self._managed_items)
        return AppleLibraryObservation(
            identity=self.library_identity,
            name=self.library_name,
            total_tracks=self.native_tracks + managed,
            managed_tracks=managed,
        )
