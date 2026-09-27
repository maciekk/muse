"""Audio integrity command and output."""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path

from muse.audio_integrity import IntegrityFailure, IntegrityReport, check_target
from muse.reporting import emit_json, human_number, make_console


def _check_audio(args: argparse.Namespace, _root: Path) -> int:
    target = Path(args.target).expanduser().absolute()
    missing = [tool for tool in ("ffmpeg", "ffprobe") if shutil.which(tool) is None]
    if missing:
        report = IntegrityReport(str(target))
        report.errors.append(
            IntegrityFailure(str(target), f"{', '.join(missing)} required for audio checks")
        )
    else:
        report = check_target(target)

    if args.json:
        emit_json(report.to_dict())
    else:
        console = make_console(args.color)
        console.print("[bold]Audio integrity check[/bold]")
        console.print("[dim]Target[/dim]", str(target))
        for failure in report.failures:
            console.print(f"[red]FAIL[/red] {failure.path}: {failure.message}")
        console.print(
            f"Checked {human_number(report.checked)} audio files: "
            f"{human_number(report.checked - len(report.failures))} passed, "
            f"{human_number(len(report.failures))} failed. "
            f"Skipped {human_number(report.skipped)} other files."
        )
        if report.errors:
            error_console = make_console(args.color, stderr=True)
            for error in report.errors:
                error_console.print(f"[red]Error:[/red] {error.path}: {error.message}")
    return 1 if report.failures or report.errors else 0
