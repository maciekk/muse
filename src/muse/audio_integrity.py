"""Read-only playback integrity checks for audio files."""

from __future__ import annotations

import json
import math
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from muse.filesystem import WalkError, walk
from muse.repository import AUDIO_EXTENSIONS


@dataclass(frozen=True)
class IntegrityFailure:
    path: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {"path": self.path, "message": self.message}


@dataclass
class IntegrityReport:
    target: str
    checked: int = 0
    skipped: int = 0
    failures: list[IntegrityFailure] = field(default_factory=list)
    errors: list[IntegrityFailure] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "target": self.target,
            "checked": self.checked,
            "passed": self.checked - len(self.failures),
            "failed": len(self.failures),
            "skipped": self.skipped,
            "failures": [failure.to_dict() for failure in self.failures],
            "errors": [error.to_dict() for error in self.errors],
        }


def _detail(stderr: bytes) -> str:
    lines = stderr.decode(errors="replace").strip().splitlines()
    return lines[-1] if lines else "invalid or incomplete audio stream"


def _run(command: list[str], *, capture_stdout: bool = False) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        command,
        stdout=subprocess.PIPE if capture_stdout else subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        check=False,
    )


def _reported_duration(path: Path) -> float | None:
    result = _run(
        [
            "ffprobe", "-v", "error", "-select_streams", "a:0",
            "-show_entries", "stream=duration:format=duration", "-of", "json", str(path),
        ],
        capture_stdout=True,
    )
    if result.returncode:
        raise ValueError(f"ffprobe failed: {_detail(result.stderr)}")
    try:
        data = json.loads(result.stdout)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"ffprobe returned invalid output: {error}") from error
    streams = data.get("streams", [])
    values = [streams[0].get("duration")] if streams else []
    values.append(data.get("format", {}).get("duration"))
    for value in values:
        try:
            duration = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(duration) and duration > 0:
            return duration
    return None


def check_file(path: Path, flac_tool: str | None) -> str | None:
    """Return a failure reason if the audio cannot be fully decoded."""
    try:
        result = _run(
            [
                "ffmpeg", "-nostdin", "-hide_banner", "-v", "error", "-xerror",
                "-err_detect", "explode", "-stats_period", "3600", "-i", str(path),
                "-map", "0:a:0", "-f", "null", "-", "-progress", "pipe:1",
            ],
            capture_stdout=True,
        )
        if result.returncode:
            return f"decode failed: {_detail(result.stderr)}"
        durations = [
            int(value)
            for line in result.stdout.splitlines()
            if line.startswith(b"out_time_us=")
            if (value := line.partition(b"=")[2]).isdigit()
        ]
        if not durations or max(durations) <= 0:
            return "no audio samples were decoded"
        decoded = max(durations) / 1_000_000
        reported = _reported_duration(path)
        if reported is not None and abs(reported - decoded) > max(5.0, reported * 0.05):
            return (
                f"duration mismatch: reported {reported:.1f}s, "
                f"decoded {decoded:.1f}s"
            )
        if path.suffix.lower() == ".flac" and flac_tool:
            result = _run([flac_tool, "--test", "--silent", "--", str(path)])
            if result.returncode:
                return f"FLAC integrity check failed: {_detail(result.stderr)}"
    except OSError as error:
        return f"could not read or decode file: {error}"
    except ValueError as error:
        return str(error)
    return None


def check_target(target: Path) -> IntegrityReport:
    """Check one audio file or every known audio file beneath a directory."""
    report = IntegrityReport(str(target))
    flac_tool = shutil.which("flac")
    for item in walk(target, sort=True):
        if isinstance(item, WalkError):
            report.errors.append(IntegrityFailure(str(item.path), item.message))
        elif item.path == target and item.kind not in {"file", "directory"}:
            report.errors.append(
                IntegrityFailure(str(target), "target must be a regular file or directory")
            )
        elif item.kind == "file":
            if item.path.suffix.lower() not in AUDIO_EXTENSIONS:
                if item.path == target:
                    report.errors.append(
                        IntegrityFailure(str(target), "target is not a recognized audio file")
                    )
                else:
                    report.skipped += 1
                continue
            report.checked += 1
            reason = check_file(item.path, flac_tool)
            if reason:
                report.failures.append(IntegrityFailure(str(item.path), reason))
    return report
