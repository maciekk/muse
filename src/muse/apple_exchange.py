"""Durable request/result bundles for transport-independent Apple exchange."""

from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from muse.apple_protocol import (
    AppleRequest,
    AppleResult,
    ProtocolValidationError,
    StagedFile,
    validate_result,
)
from muse.filesystem import WalkError, walk
from muse.hashing import FileIdentity, sha256_file
from muse.mutation import occupied, rename_exact, sync_directory

REQUEST_MANIFEST = "request.json"
RESULT_MANIFEST = "result.json"
MAX_MANIFEST_BYTES = 4 * 1024 * 1024


@dataclass(frozen=True)
class BundleExport:
    path: Path
    created: bool
    media_files: int
    media_bytes: int


def export_request_bundle(
    destination: Path,
    request: AppleRequest,
    media_sources: Mapping[str, Path] | None = None,
) -> BundleExport:
    """Publish a complete request bundle, accepting an identical existing bundle."""
    request = AppleRequest.from_dict(request.to_dict())
    staged = _staged_files(request)
    sources = dict(media_sources or {})
    expected_paths = set(staged)
    if set(sources) != expected_paths:
        missing = sorted(expected_paths - set(sources))
        extra = sorted(set(sources) - expected_paths)
        details = []
        if missing:
            details.append(f"missing media sources: {', '.join(missing)}")
        if extra:
            details.append(f"unexpected media sources: {', '.join(extra)}")
        raise ValueError("; ".join(details))

    destination = destination.absolute()
    if occupied(destination):
        existing = load_request_bundle(destination)
        if existing != request:
            raise ValueError(
                f"request bundle destination contains a different request: {destination}"
            )
        return _export_report(destination, False, staged)

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(
        tempfile.mkdtemp(prefix=f".{destination.name}-", suffix=".tmp", dir=destination.parent)
    )
    try:
        _write_text(temporary / REQUEST_MANIFEST, request.to_json())
        for relative, description in sorted(staged.items()):
            _copy_media(sources[relative], temporary / relative, description)
        _sync_tree_directories(temporary)
        if occupied(destination):
            raise ValueError(f"request bundle destination already exists: {destination}")
        rename_exact(temporary, destination)
        return _export_report(destination, True, staged)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)


def load_request_bundle(bundle: Path) -> AppleRequest:
    """Load a request bundle and verify its complete, exact inventory."""
    bundle = _bundle_directory(bundle)
    document = _read_manifest(bundle / REQUEST_MANIFEST)
    request = AppleRequest.from_json(document)
    staged = _staged_files(request)
    _verify_inventory(bundle, {REQUEST_MANIFEST, *staged})
    for relative, description in staged.items():
        _verify_media(bundle / relative, description)
    return request


def export_result_bundle(
    destination: Path, request: AppleRequest, result: AppleResult
) -> BundleExport:
    """Publish a correlated result bundle, accepting an identical existing bundle."""
    request = AppleRequest.from_dict(request.to_dict())
    result = AppleResult.from_dict(result.to_dict())
    validate_result(request, result)
    destination = destination.absolute()
    if occupied(destination):
        existing = import_result_bundle(destination, request)
        if existing != result:
            raise ValueError(
                f"result bundle destination contains a different result: {destination}"
            )
        return BundleExport(destination, False, 0, 0)

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(
        tempfile.mkdtemp(prefix=f".{destination.name}-", suffix=".tmp", dir=destination.parent)
    )
    try:
        _write_text(temporary / RESULT_MANIFEST, result.to_json())
        sync_directory(temporary)
        if occupied(destination):
            raise ValueError(f"result bundle destination already exists: {destination}")
        rename_exact(temporary, destination)
        return BundleExport(destination, True, 0, 0)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)


def import_result_bundle(bundle: Path, request: AppleRequest) -> AppleResult:
    """Load a result only when it belongs to the exact supplied request."""
    request = AppleRequest.from_dict(request.to_dict())
    bundle = _bundle_directory(bundle)
    _verify_inventory(bundle, {RESULT_MANIFEST})
    result = AppleResult.from_json(_read_manifest(bundle / RESULT_MANIFEST))
    validate_result(request, result)
    return result


def _staged_files(request: AppleRequest) -> dict[str, StagedFile]:
    staged: dict[str, StagedFile] = {}
    for operation in request.operations:
        description = operation.staged_file
        if description is None:
            continue
        if description.path in {REQUEST_MANIFEST, RESULT_MANIFEST}:
            raise ProtocolValidationError(
                f"operation.staged_file.path: {description.path!r} is reserved"
            )
        existing = staged.get(description.path)
        if existing is not None and existing != description:
            raise ProtocolValidationError(
                f"operation.staged_file.path: conflicting descriptions for {description.path}"
            )
        staged[description.path] = description
    return staged


def _bundle_directory(bundle: Path) -> Path:
    bundle = bundle.absolute()
    if bundle.is_symlink() or not bundle.is_dir():
        raise ValueError(f"bundle is not a real directory: {bundle}")
    return bundle


def _read_manifest(path: Path) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"bundle manifest is not a regular file: {path.name}")
    size = path.stat().st_size
    if size > MAX_MANIFEST_BYTES:
        raise ValueError(f"bundle manifest is too large: {path.name}")
    return path.read_bytes()


def _verify_inventory(bundle: Path, expected_files: set[str]) -> None:
    expected_directories = {"."}
    for name in expected_files:
        parent = Path(name).parent
        while parent != Path("."):
            expected_directories.add(parent.as_posix())
            parent = parent.parent

    actual_files: set[str] = set()
    actual_directories: set[str] = set()
    problems: list[str] = []
    for entry in walk(bundle, sort=True):
        if isinstance(entry, WalkError):
            problems.append(f"{entry.path}: {entry.message}")
            continue
        relative = entry.path.relative_to(bundle).as_posix() or "."
        if entry.kind == "file":
            actual_files.add(relative)
        elif entry.kind == "directory":
            actual_directories.add(relative)
        else:
            problems.append(f"unsupported {entry.kind}: {relative}")
    missing_files = sorted(expected_files - actual_files)
    unexpected_files = sorted(actual_files - expected_files)
    missing_directories = sorted(expected_directories - actual_directories)
    unexpected_directories = sorted(actual_directories - expected_directories)
    if missing_files:
        problems.append(f"missing files: {', '.join(missing_files)}")
    if unexpected_files:
        problems.append(f"unexpected files: {', '.join(unexpected_files)}")
    if missing_directories:
        problems.append(f"missing directories: {', '.join(missing_directories)}")
    if unexpected_directories:
        problems.append(f"unexpected directories: {', '.join(unexpected_directories)}")
    if problems:
        raise ValueError("invalid bundle inventory: " + "; ".join(problems))


def _copy_media(source: Path, destination: Path, expected: StagedFile) -> None:
    if source.is_symlink() or not source.is_file():
        raise ValueError(f"media source is not a regular file: {source}")
    identity = FileIdentity.from_path(source)
    if identity.size != expected.size:
        raise ValueError(f"media source size does not match request: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with source.open("rb") as input_file, destination.open("xb") as output:
        shutil.copyfileobj(input_file, output)
        output.flush()
        os.fsync(output.fileno())
    if FileIdentity.from_path(source) != identity:
        raise ValueError(f"media source changed during export: {source}")
    _verify_media(destination, expected)
    sync_directory(destination.parent)


def _verify_media(path: Path, expected: StagedFile) -> None:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"staged media is not a regular file: {expected.path}")
    identity = FileIdentity.from_path(path)
    if identity.size != expected.size:
        raise ValueError(f"staged media size does not match request: {expected.path}")
    digest = sha256_file(path, expected=identity)
    if digest != expected.sha256:
        raise ValueError(f"staged media SHA-256 does not match request: {expected.path}")


def _write_text(path: Path, value: str) -> None:
    with path.open("x", encoding="utf-8") as stream:
        stream.write(value)
        stream.flush()
        os.fsync(stream.fileno())


def _sync_tree_directories(root: Path) -> None:
    directories: list[Path] = []
    for entry in walk(root):
        if isinstance(entry, WalkError):
            raise ValueError(f"cannot inventory request bundle: {entry.path}: {entry.message}")
        if entry.kind == "directory":
            directories.append(entry.path)
    for directory in sorted(directories, key=lambda item: len(item.parts), reverse=True):
        sync_directory(directory)


def _export_report(
    path: Path, created: bool, staged: Mapping[str, StagedFile]
) -> BundleExport:
    return BundleExport(
        path=path,
        created=created,
        media_files=len(staged),
        media_bytes=sum(item.size for item in staged.values()),
    )
