"""Versioned request and result protocol for the macOS Music.app bridge.

The protocol is deliberately transport-independent.  The same JSON documents can
be exchanged manually or carried by a future SSH transport.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import PurePosixPath
from typing import Any, Literal, TypeAlias

PROTOCOL_NAME = "muse-apple"
PROTOCOL_VERSION = 1
MANAGED_PLAYLISTS = frozenset({"Muse — Master", "Muse — Stopgap"})

AppleAction: TypeAlias = Literal["inspect-library", "add-track"]
OperationStatus: TypeAlias = Literal["succeeded", "failed", "uncertain"]

_ACTIONS = frozenset({"inspect-library", "add-track"})
_STATUSES = frozenset({"succeeded", "failed", "uncertain"})
_SHA256 = re.compile(r"[0-9a-f]{64}")
_CAPABILITY_NAME = re.compile(r"[a-z][a-z0-9-]*")


class ProtocolValidationError(ValueError):
    """A bridge document does not conform to the supported protocol."""


def new_id() -> str:
    """Return a new canonical opaque identity for a batch or operation."""
    return str(uuid.uuid4())


def _error(field: str, message: str) -> ProtocolValidationError:
    return ProtocolValidationError(f"{field}: {message}")


def _mapping(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise _error(field, "must be an object")
    return value


def _keys(value: dict[str, Any], field: str, expected: set[str]) -> None:
    unknown = sorted(set(value) - expected)
    missing = sorted(expected - set(value))
    if unknown:
        raise _error(field, f"unknown fields: {', '.join(unknown)}")
    if missing:
        raise _error(field, f"missing fields: {', '.join(missing)}")


def _string(value: Any, field: str, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or not value:
        raise _error(field, "must be a non-empty string")
    return value


def _integer(value: Any, field: str, *, minimum: int = 0, optional: bool = False) -> int | None:
    if value is None and optional:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        qualifier = "positive" if minimum == 1 else "non-negative"
        raise _error(field, f"must be a {qualifier} integer")
    return value


def _identifier(value: Any, field: str) -> str:
    text = _string(value, field)
    assert text is not None
    try:
        parsed = uuid.UUID(text)
    except (ValueError, AttributeError) as error:
        raise _error(field, "must be a UUID") from error
    if str(parsed) != text:
        raise _error(field, "must use canonical lowercase UUID spelling")
    return text


def _timestamp(value: Any, field: str) -> str:
    text = _string(value, field)
    assert text is not None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as error:
        raise _error(field, "must be an ISO 8601 timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise _error(field, "must include a UTC offset")
    return text


def _sha256(value: Any, field: str, *, optional: bool = False) -> str | None:
    text = _string(value, field, optional=optional)
    if text is not None and _SHA256.fullmatch(text) is None:
        raise _error(field, "must be a lowercase SHA-256 digest")
    return text


def _safe_relative_path(value: Any, field: str) -> str:
    text = _string(value, field)
    assert text is not None
    path = PurePosixPath(text)
    if (
        path.is_absolute()
        or text != path.as_posix()
        or not path.parts
        or any(part in {"", ".", ".."} for part in path.parts)
        or "\\" in text
    ):
        raise _error(field, "must be a normalized relative POSIX path")
    return text


def _strings(value: Any, field: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise _error(field, "must be an array")
    result = tuple(_string(item, f"{field}[{index}]") for index, item in enumerate(value))
    strings = tuple(item for item in result if item is not None)
    if len(set(strings)) != len(strings):
        raise _error(field, "must not contain duplicates")
    return strings


@dataclass(frozen=True)
class StagedFile:
    path: str
    size: int
    sha256: str

    @classmethod
    def from_dict(cls, raw: Any, field: str = "staged_file") -> StagedFile:
        value = _mapping(raw, field)
        _keys(value, field, {"path", "size", "sha256"})
        path = _safe_relative_path(value["path"], f"{field}.path")
        size = _integer(value["size"], f"{field}.size")
        digest = _sha256(value["sha256"], f"{field}.sha256")
        assert size is not None and digest is not None
        return cls(path, size, digest)


@dataclass(frozen=True)
class TrackMetadata:
    title: str
    artist: str
    album: str
    album_artist: str
    track_number: int
    track_total: int | None
    disc_number: int
    disc_total: int | None
    composer: str | None = None
    genre: str | None = None
    grouping: str | None = None
    year: str | None = None

    @classmethod
    def from_dict(cls, raw: Any, field: str = "metadata") -> TrackMetadata:
        value = _mapping(raw, field)
        names = {
            "title",
            "artist",
            "album",
            "album_artist",
            "track_number",
            "track_total",
            "disc_number",
            "disc_total",
            "composer",
            "genre",
            "grouping",
            "year",
        }
        _keys(value, field, names)
        required = {
            name: _string(value[name], f"{field}.{name}")
            for name in ("title", "artist", "album", "album_artist")
        }
        track_number = _integer(value["track_number"], f"{field}.track_number", minimum=1)
        track_total = _integer(
            value["track_total"], f"{field}.track_total", minimum=1, optional=True
        )
        disc_number = _integer(value["disc_number"], f"{field}.disc_number", minimum=1)
        disc_total = _integer(
            value["disc_total"], f"{field}.disc_total", minimum=1, optional=True
        )
        if track_total is not None and track_number is not None and track_total < track_number:
            raise _error(f"{field}.track_total", "must not be less than track_number")
        if disc_total is not None and disc_number is not None and disc_total < disc_number:
            raise _error(f"{field}.disc_total", "must not be less than disc_number")
        assert all(item is not None for item in required.values())
        assert track_number is not None and disc_number is not None
        return cls(
            title=required["title"] or "",
            artist=required["artist"] or "",
            album=required["album"] or "",
            album_artist=required["album_artist"] or "",
            track_number=track_number,
            track_total=track_total,
            disc_number=disc_number,
            disc_total=disc_total,
            composer=_string(value["composer"], f"{field}.composer", optional=True),
            genre=_string(value["genre"], f"{field}.genre", optional=True),
            grouping=_string(value["grouping"], f"{field}.grouping", optional=True),
            year=_string(value["year"], f"{field}.year", optional=True),
        )


@dataclass(frozen=True)
class AppleOperation:
    operation_id: str
    action: AppleAction
    item_id: int | None = None
    source_sha256: str | None = None
    staged_file: StagedFile | None = None
    metadata: TrackMetadata | None = None
    playlist_memberships: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, raw: Any, field: str = "operation") -> AppleOperation:
        value = _mapping(raw, field)
        names = {
            "operation_id",
            "action",
            "item_id",
            "source_sha256",
            "staged_file",
            "metadata",
            "playlist_memberships",
        }
        _keys(value, field, names)
        operation_id = _identifier(value["operation_id"], f"{field}.operation_id")
        action_value = _string(value["action"], f"{field}.action")
        if action_value not in _ACTIONS:
            raise _error(f"{field}.action", f"unsupported action {action_value!r}")
        item_id = _integer(value["item_id"], f"{field}.item_id", minimum=1, optional=True)
        source_sha256 = _sha256(
            value["source_sha256"], f"{field}.source_sha256", optional=True
        )
        staged_file = (
            StagedFile.from_dict(value["staged_file"], f"{field}.staged_file")
            if value["staged_file"] is not None
            else None
        )
        metadata = (
            TrackMetadata.from_dict(value["metadata"], f"{field}.metadata")
            if value["metadata"] is not None
            else None
        )
        memberships = _strings(value["playlist_memberships"], f"{field}.playlist_memberships")
        unknown_memberships = sorted(set(memberships) - MANAGED_PLAYLISTS)
        if unknown_memberships:
            raise _error(
                f"{field}.playlist_memberships",
                f"unmanaged playlists: {', '.join(unknown_memberships)}",
            )
        operation = cls(
            operation_id=operation_id,
            action=action_value,  # type: ignore[arg-type]
            item_id=item_id,
            source_sha256=source_sha256,
            staged_file=staged_file,
            metadata=metadata,
            playlist_memberships=memberships,
        )
        operation.validate(field)
        return operation

    def validate(self, field: str = "operation") -> None:
        supplied = (self.item_id, self.source_sha256, self.staged_file, self.metadata)
        if self.action == "inspect-library":
            if any(value is not None for value in supplied) or self.playlist_memberships:
                raise _error(field, "inspect-library must not contain track fields")
        elif self.action == "add-track":
            if any(value is None for value in supplied):
                raise _error(field, "add-track requires item, source, staged file, and metadata")
            if not self.playlist_memberships:
                raise _error(field, "add-track requires at least one playlist membership")

    def to_dict(self) -> dict[str, Any]:
        return {
            "operation_id": self.operation_id,
            "action": self.action,
            "item_id": self.item_id,
            "source_sha256": self.source_sha256,
            "staged_file": asdict(self.staged_file) if self.staged_file else None,
            "metadata": asdict(self.metadata) if self.metadata else None,
            "playlist_memberships": list(self.playlist_memberships),
        }


@dataclass(frozen=True)
class AppleRequest:
    batch_id: str
    created_at: str
    operations: tuple[AppleOperation, ...]
    protocol: str = PROTOCOL_NAME
    protocol_version: int = PROTOCOL_VERSION
    kind: str = "request"

    @classmethod
    def from_dict(cls, raw: Any) -> AppleRequest:
        value = _mapping(raw, "request")
        names = {"protocol", "protocol_version", "kind", "batch_id", "created_at", "operations"}
        _keys(value, "request", names)
        _header(value, "request", "request")
        batch_id = _identifier(value["batch_id"], "request.batch_id")
        created_at = _timestamp(value["created_at"], "request.created_at")
        raw_operations = value["operations"]
        if not isinstance(raw_operations, list) or not raw_operations:
            raise _error("request.operations", "must be a non-empty array")
        operations = tuple(
            AppleOperation.from_dict(item, f"request.operations[{index}]")
            for index, item in enumerate(raw_operations)
        )
        identifiers = [item.operation_id for item in operations]
        if len(set(identifiers)) != len(identifiers):
            raise _error("request.operations", "operation IDs must be unique")
        return cls(batch_id, created_at, operations)

    @classmethod
    def from_json(cls, document: str | bytes) -> AppleRequest:
        return cls.from_dict(_load_json(document, "request"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol": self.protocol,
            "protocol_version": self.protocol_version,
            "kind": self.kind,
            "batch_id": self.batch_id,
            "created_at": self.created_at,
            "operations": [item.to_dict() for item in self.operations],
        }

    def to_json(self) -> str:
        return _pretty_json(self.to_dict())

    @property
    def sha256(self) -> str:
        return hashlib.sha256(_canonical_json(self.to_dict())).hexdigest()


@dataclass(frozen=True)
class BridgeCapabilities:
    bridge_version: str
    supported_protocol_versions: tuple[int, ...]
    supported_actions: tuple[str, ...]
    readable_properties: tuple[str, ...] = ()
    writable_properties: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, raw: Any, field: str = "capabilities") -> BridgeCapabilities:
        value = _mapping(raw, field)
        names = {
            "bridge_version",
            "supported_protocol_versions",
            "supported_actions",
            "readable_properties",
            "writable_properties",
        }
        _keys(value, field, names)
        versions_raw = value["supported_protocol_versions"]
        if not isinstance(versions_raw, list) or not versions_raw:
            raise _error(f"{field}.supported_protocol_versions", "must be a non-empty array")
        versions = tuple(
            _integer(item, f"{field}.supported_protocol_versions[{index}]", minimum=1)
            for index, item in enumerate(versions_raw)
        )
        clean_versions = tuple(item for item in versions if item is not None)
        if len(set(clean_versions)) != len(clean_versions):
            raise _error(f"{field}.supported_protocol_versions", "must not contain duplicates")
        actions = _strings(value["supported_actions"], f"{field}.supported_actions")
        invalid_actions = [item for item in actions if _CAPABILITY_NAME.fullmatch(item) is None]
        if invalid_actions:
            raise _error(
                f"{field}.supported_actions",
                f"invalid action names: {', '.join(invalid_actions)}",
            )
        bridge_version = _string(value["bridge_version"], f"{field}.bridge_version")
        assert bridge_version is not None
        return cls(
            bridge_version=bridge_version,
            supported_protocol_versions=clean_versions,
            supported_actions=actions,
            readable_properties=_strings(
                value["readable_properties"], f"{field}.readable_properties"
            ),
            writable_properties=_strings(
                value["writable_properties"], f"{field}.writable_properties"
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "bridge_version": self.bridge_version,
            "supported_protocol_versions": list(self.supported_protocol_versions),
            "supported_actions": list(self.supported_actions),
            "readable_properties": list(self.readable_properties),
            "writable_properties": list(self.writable_properties),
        }


@dataclass(frozen=True)
class AppleLibraryObservation:
    identity: str | None
    name: str | None
    total_tracks: int
    managed_tracks: int
    application_version: str | None = None

    @classmethod
    def from_dict(cls, raw: Any, field: str = "library") -> AppleLibraryObservation:
        value = _mapping(raw, field)
        _keys(
            value,
            field,
            {"identity", "name", "total_tracks", "managed_tracks", "application_version"},
        )
        total = _integer(value["total_tracks"], f"{field}.total_tracks")
        managed = _integer(value["managed_tracks"], f"{field}.managed_tracks")
        assert total is not None and managed is not None
        if managed > total:
            raise _error(f"{field}.managed_tracks", "must not exceed total_tracks")
        return cls(
            identity=_string(value["identity"], f"{field}.identity", optional=True),
            name=_string(value["name"], f"{field}.name", optional=True),
            total_tracks=total,
            managed_tracks=managed,
            application_version=_string(
                value["application_version"], f"{field}.application_version", optional=True
            ),
        )


@dataclass(frozen=True)
class OperationResult:
    operation_id: str
    status: OperationStatus
    item_id: int | None = None
    apple_persistent_id: str | None = None
    # Retained in protocol v1 for compatibility with already exchanged probe
    # documents. Subscription-free Finder sync does not populate this field.
    cloud_status: str | None = None
    error_code: str | None = None
    message: str | None = None

    @classmethod
    def from_dict(cls, raw: Any, field: str = "result") -> OperationResult:
        value = _mapping(raw, field)
        names = {
            "operation_id",
            "status",
            "item_id",
            "apple_persistent_id",
            "cloud_status",
            "error_code",
            "message",
        }
        _keys(value, field, names)
        status_value = _string(value["status"], f"{field}.status")
        if status_value not in _STATUSES:
            raise _error(f"{field}.status", f"unsupported status {status_value!r}")
        result = cls(
            operation_id=_identifier(value["operation_id"], f"{field}.operation_id"),
            status=status_value,  # type: ignore[arg-type]
            item_id=_integer(value["item_id"], f"{field}.item_id", minimum=1, optional=True),
            apple_persistent_id=_string(
                value["apple_persistent_id"], f"{field}.apple_persistent_id", optional=True
            ),
            cloud_status=_string(
                value["cloud_status"], f"{field}.cloud_status", optional=True
            ),
            error_code=_string(value["error_code"], f"{field}.error_code", optional=True),
            message=_string(value["message"], f"{field}.message", optional=True),
        )
        if result.status in {"failed", "uncertain"} and result.message is None:
            raise _error(field, f"{result.status} results require a message")
        if result.status == "failed" and result.error_code is None:
            raise _error(field, "failed results require an error code")
        if result.status == "succeeded" and result.error_code is not None:
            raise _error(field, "succeeded results must not contain an error code")
        return result


@dataclass(frozen=True)
class AppleResult:
    batch_id: str
    request_sha256: str
    bridge_id: str
    observed_at: str
    capabilities: BridgeCapabilities
    library: AppleLibraryObservation | None
    operations: tuple[OperationResult, ...]
    protocol: str = PROTOCOL_NAME
    protocol_version: int = PROTOCOL_VERSION
    kind: str = "result"

    @classmethod
    def from_dict(cls, raw: Any) -> AppleResult:
        value = _mapping(raw, "result")
        names = {
            "protocol",
            "protocol_version",
            "kind",
            "batch_id",
            "request_sha256",
            "bridge_id",
            "observed_at",
            "capabilities",
            "library",
            "operations",
        }
        _keys(value, "result", names)
        _header(value, "result", "result")
        raw_operations = value["operations"]
        if not isinstance(raw_operations, list) or not raw_operations:
            raise _error("result.operations", "must be a non-empty array")
        operations = tuple(
            OperationResult.from_dict(item, f"result.operations[{index}]")
            for index, item in enumerate(raw_operations)
        )
        identifiers = [item.operation_id for item in operations]
        if len(set(identifiers)) != len(identifiers):
            raise _error("result.operations", "operation IDs must be unique")
        return cls(
            batch_id=_identifier(value["batch_id"], "result.batch_id"),
            request_sha256=_sha256(value["request_sha256"], "result.request_sha256") or "",
            bridge_id=_identifier(value["bridge_id"], "result.bridge_id"),
            observed_at=_timestamp(value["observed_at"], "result.observed_at"),
            capabilities=BridgeCapabilities.from_dict(value["capabilities"]),
            library=(
                AppleLibraryObservation.from_dict(value["library"])
                if value["library"] is not None
                else None
            ),
            operations=operations,
        )

    @classmethod
    def from_json(cls, document: str | bytes) -> AppleResult:
        return cls.from_dict(_load_json(document, "result"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol": self.protocol,
            "protocol_version": self.protocol_version,
            "kind": self.kind,
            "batch_id": self.batch_id,
            "request_sha256": self.request_sha256,
            "bridge_id": self.bridge_id,
            "observed_at": self.observed_at,
            "capabilities": self.capabilities.to_dict(),
            "library": asdict(self.library) if self.library else None,
            "operations": [asdict(item) for item in self.operations],
        }

    def to_json(self) -> str:
        return _pretty_json(self.to_dict())


def validate_result(request: AppleRequest, result: AppleResult) -> None:
    """Verify that a bridge result is a complete response to exactly *request*."""
    if result.batch_id != request.batch_id:
        raise _error("result.batch_id", "does not match request")
    if result.request_sha256 != request.sha256:
        raise _error("result.request_sha256", "does not match request content")
    if request.protocol_version not in result.capabilities.supported_protocol_versions:
        raise _error("result.capabilities", "does not report support for the request protocol")
    requested = {item.operation_id: item for item in request.operations}
    returned = {item.operation_id: item for item in result.operations}
    missing = sorted(set(requested) - set(returned))
    extra = sorted(set(returned) - set(requested))
    if missing or extra:
        details = []
        if missing:
            details.append(f"missing {', '.join(missing)}")
        if extra:
            details.append(f"unexpected {', '.join(extra)}")
        raise _error("result.operations", "; ".join(details))
    for operation_id, operation in requested.items():
        outcome = returned[operation_id]
        if outcome.item_id != operation.item_id:
            raise _error(
                f"result.operations[{operation_id}].item_id", "does not match request"
            )
        if (
            outcome.status == "succeeded"
            and operation.action not in result.capabilities.supported_actions
        ):
            raise _error(
                f"result.operations[{operation_id}].status",
                "succeeded for an action the bridge does not report supporting",
            )
        if (
            operation.action == "inspect-library"
            and outcome.status == "succeeded"
            and result.library is None
        ):
            raise _error("result.library", "is required after a successful inspect-library")
        if (
            operation.action == "add-track"
            and outcome.status == "succeeded"
            and outcome.apple_persistent_id is None
        ):
            raise _error(
                f"result.operations[{operation_id}].apple_persistent_id",
                "is required after a successful add-track",
            )


def _header(value: dict[str, Any], field: str, kind: str) -> None:
    if value["protocol"] != PROTOCOL_NAME:
        raise _error(f"{field}.protocol", f"must be {PROTOCOL_NAME!r}")
    if value["protocol_version"] != PROTOCOL_VERSION:
        raise _error(
            f"{field}.protocol_version",
            f"unsupported version {value['protocol_version']!r}; expected {PROTOCOL_VERSION}",
        )
    if value["kind"] != kind:
        raise _error(f"{field}.kind", f"must be {kind!r}")


def _load_json(document: str | bytes, field: str) -> Any:
    try:
        return json.loads(document)
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise _error(field, f"invalid JSON: {error}") from error


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()


def _pretty_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
