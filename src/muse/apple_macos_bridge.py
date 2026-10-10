"""Read-only macOS Music.app bridge and durable local operation journal."""

from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from muse import __version__
from muse.apple_exchange import (
    BundleExport,
    export_request_bundle,
    export_result_bundle,
    load_request_bundle,
)
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

DEFAULT_STATE = (
    Path.home() / "Library" / "Application Support" / "Muse" / "bridge" / "journal.db"
)


class MusicProbeError(RuntimeError):
    """Music.app could not provide a requested read-only observation."""


class MusicProbe(Protocol):
    def inspect(self) -> AppleLibraryObservation: ...


class MusicAppProbe:
    """Minimal read-only Music.app inspection through supported AppleScript."""

    def inspect(self) -> AppleLibraryObservation:
        total = self._integer("count of every track of library playlist 1", "track count")
        master = self._playlist_count("Muse — Master")
        stopgap = self._playlist_count("Muse — Stopgap")
        managed = master + stopgap
        if managed > total:
            raise MusicProbeError(
                "managed playlist counts exceed the library track count; "
                "the managed playlists may overlap"
            )
        return AppleLibraryObservation(
            identity=self._optional("persistent ID of library playlist 1"),
            name=self._optional("name of library playlist 1"),
            total_tracks=total,
            managed_tracks=managed,
            application_version=self._optional("version"),
        )

    def _playlist_count(self, name: str) -> int:
        quoted = name.replace('"', '\\"')
        script = f"""
tell application "Music"
    if exists user playlist "{quoted}" then
        return count of every track of user playlist "{quoted}"
    end if
    return 0
end tell
"""
        return self._integer_script(script, f"track count for {name}")

    def _optional(self, expression: str) -> str | None:
        try:
            return self._run(f'tell application "Music" to get {expression}')
        except MusicProbeError:
            return None

    def _integer(self, expression: str, label: str) -> int:
        return self._integer_script(f'tell application "Music" to get {expression}', label)

    def _integer_script(self, script: str, label: str) -> int:
        value = self._run(script)
        try:
            number = int(value)
        except ValueError as error:
            raise MusicProbeError(f"Music.app returned an invalid {label}: {value!r}") from error
        if number < 0:
            raise MusicProbeError(f"Music.app returned a negative {label}: {number}")
        return number

    def _run(self, script: str) -> str:
        if sys.platform != "darwin":
            raise MusicProbeError("the Music.app bridge must run on macOS")
        try:
            completed = subprocess.run(
                ["osascript", "-e", script],
                check=False,
                capture_output=True,
                text=True,
            )
        except OSError as error:
            raise MusicProbeError(f"cannot run osascript: {error}") from error
        if completed.returncode:
            detail = completed.stderr.strip().splitlines()
            raise MusicProbeError(detail[-1] if detail else "Music.app automation failed")
        return completed.stdout.strip()


class BridgeJournal:
    """SQLite journal providing replay and ambiguous-interruption protection."""

    def __init__(self, path: Path) -> None:
        self.path = path.expanduser().absolute()
        if self.path.is_symlink():
            raise ValueError(f"bridge journal must not be a symlink: {self.path}")
        parent_existed = self.path.parent.exists()
        if self.path.parent.is_symlink():
            raise ValueError(
                f"bridge journal directory must not be a symlink: {self.path.parent}"
            )
        self.path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        if not parent_existed:
            self.path.parent.chmod(0o700)
        self.connection = sqlite3.connect(self.path, timeout=30)
        self.path.chmod(0o600)
        self._initialize()

    def __enter__(self) -> BridgeJournal:
        return self

    def __exit__(self, *_args: object) -> None:
        self.connection.close()

    @property
    def bridge_id(self) -> str:
        row = self.connection.execute(
            "SELECT value FROM bridge_state WHERE key = 'bridge_id'"
        ).fetchone()
        assert row is not None
        return str(row[0])

    def handle(self, request: AppleRequest, probe: MusicProbe) -> AppleResult:
        request = AppleRequest.from_dict(request.to_dict())
        existing = self._reserve_batch(request)
        if existing is not None:
            validate_result(request, existing)
            return existing

        outcomes: list[OperationResult] = []
        library: AppleLibraryObservation | None = None
        for operation in request.operations:
            recorded = self._reserve_operation(operation)
            if recorded is None:
                outcome, observation = self._execute(operation, probe)
                self._record_operation(operation, outcome, observation)
            else:
                outcome, observation = recorded
            if observation is not None:
                library = observation
            outcomes.append(outcome)

        result = AppleResult(
            batch_id=request.batch_id,
            request_sha256=request.sha256,
            bridge_id=self.bridge_id,
            observed_at=_now(),
            capabilities=_capabilities(),
            library=library,
            operations=tuple(outcomes),
        )
        result = AppleResult.from_dict(result.to_dict())
        validate_result(request, result)
        self._complete_batch(request, result)
        return result

    def _initialize(self) -> None:
        self.connection.execute("PRAGMA journal_mode = WAL")
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS bridge_state (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS batches (
                batch_id TEXT PRIMARY KEY,
                request_sha256 TEXT NOT NULL,
                state TEXT NOT NULL CHECK (state IN ('applying', 'completed')),
                result_json TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS operations (
                operation_id TEXT PRIMARY KEY,
                request_json TEXT NOT NULL,
                state TEXT NOT NULL CHECK (state IN ('applying', 'completed')),
                result_json TEXT,
                observation_json TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )
        columns = {
            str(row[1]) for row in self.connection.execute("PRAGMA table_info(operations)")
        }
        if "observation_json" not in columns:
            self.connection.execute("ALTER TABLE operations ADD COLUMN observation_json TEXT")
        self.connection.execute(
            "INSERT OR IGNORE INTO bridge_state (key, value) VALUES ('bridge_id', ?)",
            (new_id(),),
        )
        self.connection.commit()

    def _reserve_batch(self, request: AppleRequest) -> AppleResult | None:
        now = _now()
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            row = self.connection.execute(
                "SELECT request_sha256, state, result_json FROM batches WHERE batch_id = ?",
                (request.batch_id,),
            ).fetchone()
            if row is not None:
                digest, state, result_json = str(row[0]), str(row[1]), row[2]
                if digest != request.sha256:
                    raise ProtocolValidationError(
                        "request.batch_id: was already used for different request content"
                    )
                if state == "completed":
                    if result_json is None:
                        raise ValueError("completed bridge batch has no result")
                    result = AppleResult.from_json(str(result_json))
                    self.connection.commit()
                    return result
            else:
                for operation in request.operations:
                    self._check_operation_identity(operation)
                self.connection.execute(
                    """
                    INSERT INTO batches
                        (batch_id, request_sha256, state, result_json, created_at, updated_at)
                    VALUES (?, ?, 'applying', NULL, ?, ?)
                    """,
                    (request.batch_id, request.sha256, now, now),
                )
            self.connection.commit()
            return None
        except Exception:
            self.connection.rollback()
            raise

    def _check_operation_identity(self, operation: AppleOperation) -> None:
        row = self.connection.execute(
            "SELECT request_json FROM operations WHERE operation_id = ?",
            (operation.operation_id,),
        ).fetchone()
        if row is not None and str(row[0]) != _json(operation.to_dict()):
            raise ProtocolValidationError(
                "request.operations: operation ID was already used for different content"
            )

    def _reserve_operation(
        self, operation: AppleOperation
    ) -> tuple[OperationResult, AppleLibraryObservation | None] | None:
        now = _now()
        document = _json(operation.to_dict())
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            row = self.connection.execute(
                """
                SELECT request_json, state, result_json, observation_json
                FROM operations WHERE operation_id = ?
                """,
                (operation.operation_id,),
            ).fetchone()
            if row is None:
                self.connection.execute(
                    """
                    INSERT INTO operations
                        (operation_id, request_json, state, result_json,
                         observation_json, created_at, updated_at)
                    VALUES (?, ?, 'applying', NULL, NULL, ?, ?)
                    """,
                    (operation.operation_id, document, now, now),
                )
                self.connection.commit()
                return None
            if str(row[0]) != document:
                raise ProtocolValidationError(
                    "request.operations: operation ID was already used for different content"
                )
            if str(row[1]) == "completed":
                if row[2] is None:
                    raise ValueError("completed bridge operation has no result")
                result = OperationResult.from_dict(json.loads(str(row[2])))
                observation = (
                    AppleLibraryObservation.from_dict(json.loads(str(row[3])))
                    if row[3] is not None
                    else None
                )
                self.connection.commit()
                return result, observation
            if operation.action == "inspect-library":
                self.connection.commit()
                return None
            uncertain = OperationResult(
                operation_id=operation.operation_id,
                status="uncertain",
                item_id=operation.item_id,
                message="a previous bridge process stopped while applying this operation",
            )
            self.connection.execute(
                """
                UPDATE operations
                SET state = 'completed', result_json = ?, updated_at = ?
                WHERE operation_id = ?
                """,
                (_json(asdict(uncertain)), now, operation.operation_id),
            )
            self.connection.commit()
            return uncertain, None
        except Exception:
            self.connection.rollback()
            raise

    def _execute(
        self, operation: AppleOperation, probe: MusicProbe
    ) -> tuple[OperationResult, AppleLibraryObservation | None]:
        if operation.action == "inspect-library":
            try:
                observation = probe.inspect()
            except MusicProbeError as error:
                return (
                    OperationResult(
                        operation_id=operation.operation_id,
                        status="failed",
                        error_code="music-app-inspection-failed",
                        message=str(error),
                    ),
                    None,
                )
            return OperationResult(operation.operation_id, "succeeded"), observation
        return (
            OperationResult(
                operation_id=operation.operation_id,
                status="failed",
                item_id=operation.item_id,
                error_code="unsupported-action",
                message=f"bridge does not support {operation.action}",
            ),
            None,
        )

    def _record_operation(
        self,
        operation: AppleOperation,
        result: OperationResult,
        observation: AppleLibraryObservation | None,
    ) -> None:
        with self.connection:
            self.connection.execute(
                """
                UPDATE operations
                SET state = 'completed', result_json = ?, observation_json = ?, updated_at = ?
                WHERE operation_id = ? AND state = 'applying'
                """,
                (
                    _json(asdict(result)),
                    _json(asdict(observation)) if observation is not None else None,
                    _now(),
                    operation.operation_id,
                ),
            )

    def _complete_batch(self, request: AppleRequest, result: AppleResult) -> None:
        with self.connection:
            self.connection.execute(
                """
                UPDATE batches
                SET state = 'completed', result_json = ?, updated_at = ?
                WHERE batch_id = ? AND request_sha256 = ?
                """,
                (result.to_json(), _now(), request.batch_id, request.sha256),
            )


def prepare_inspection_bundle(destination: Path) -> tuple[AppleRequest, BundleExport]:
    """Create a fresh read-only request bundle for manual Mac probing."""
    request = AppleRequest(
        batch_id=new_id(),
        created_at=_now(),
        operations=(AppleOperation(new_id(), "inspect-library"),),
    )
    return request, export_request_bundle(destination, request)


def apply_bundle(
    request_bundle: Path,
    result_bundle: Path,
    *,
    state_path: Path = DEFAULT_STATE,
    probe: MusicProbe | None = None,
) -> tuple[AppleResult, BundleExport]:
    """Apply one request bundle locally and durably publish its result bundle."""
    request = load_request_bundle(request_bundle)
    with BridgeJournal(state_path) as journal:
        result = journal.handle(request, probe or MusicAppProbe())
    exported = export_result_bundle(result_bundle, request, result)
    return result, exported


def _capabilities() -> BridgeCapabilities:
    return BridgeCapabilities(
        bridge_version=f"muse-{__version__}",
        supported_protocol_versions=(PROTOCOL_VERSION,),
        supported_actions=("inspect-library",),
        readable_properties=(
            "application-version",
            "library-identity",
            "library-name",
            "managed-track-count",
            "total-track-count",
        ),
        writable_properties=(),
    )


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="muse-apple-bridge",
        description="Exchange read-only Muse requests with Music.app.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser(
        "prepare-inspection", help="create a fresh read-only request bundle"
    )
    prepare.add_argument("destination", type=Path)
    apply = commands.add_parser("apply", help="apply a request locally through Music.app")
    apply.add_argument("request_bundle", type=Path)
    apply.add_argument("result_bundle", type=Path)
    apply.add_argument("--state", type=Path, default=DEFAULT_STATE)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        if arguments.command == "prepare-inspection":
            _request, exported = prepare_inspection_bundle(arguments.destination)
            action = "wrote" if exported.created else "verified"
            print(f"{action} {exported.path}")
            return 0
        result, exported = apply_bundle(
            arguments.request_bundle,
            arguments.result_bundle,
            state_path=arguments.state,
        )
    except (OSError, sqlite3.Error, ValueError) as error:
        print(f"muse-apple-bridge: {error}", file=sys.stderr)
        return 1
    action = "wrote" if exported.created else "verified"
    print(f"{action} {exported.path}")
    return 1 if any(item.status != "succeeded" for item in result.operations) else 0


if __name__ == "__main__":
    raise SystemExit(main())
