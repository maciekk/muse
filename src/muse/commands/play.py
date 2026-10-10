"""Local audio playback through mpv."""

from __future__ import annotations

import argparse
import shutil
import subprocess
from pathlib import Path

from rich.text import Text

from muse.config import resolve_target
from muse.filesystem import WalkError, walk
from muse.reporting import make_console
from muse.repository import AUDIO_EXTENSIONS


def _audio_paths(root: Path, targets: list[str]) -> tuple[list[Path], list[str]]:
    """Resolve targets and return unique audio files in deterministic order."""
    selected: set[Path] = set()
    errors: list[str] = []

    for value in targets:
        target = resolve_target(root, value)
        target_audio: list[Path] = []
        target_errors: list[str] = []
        for item in walk(target, sort=True):
            if isinstance(item, WalkError):
                target_errors.append(f"{item.path}: {item.message}")
            elif item.kind == "symlink":
                target_errors.append(f"{item.path}: symlink skipped")
            elif item.kind == "file" and item.path.suffix.lower() in AUDIO_EXTENSIONS:
                target_audio.append(item.path.absolute())

        if not target_audio and not target_errors:
            target_errors.append(f"{target}: no recognized audio files found")
        selected.update(target_audio)
        errors.extend(target_errors)

    return sorted(selected, key=lambda path: (str(path).casefold(), str(path))), errors


def _print_controls(color: str, file_count: int) -> None:
    noun = "FILE" if file_count == 1 else "FILES"
    controls = Text(f"{file_count} AUDIO {noun} SELECTED", style="bold green")
    controls.append("  •  ", style="dim")
    controls.append("MPV KEYS", style="bold cyan")
    for key, action in (
        ("SPACE", "pause/resume"),
        ("←/→", "seek"),
        ("9/0", "volume"),
        ("</>", "track"),
        ("q", "quit"),
    ):
        controls.append("  •  ", style="dim")
        controls.append(key, style="bold cyan")
        controls.append(f" {action}")
    make_console(color, stderr=True).print(controls)


def _play(args: argparse.Namespace, root: Path) -> int:
    executable = shutil.which("mpv")
    if executable is None:
        console = make_console(args.color, stderr=True)
        console.print("[red]Error:[/red] mpv is required for local playback but was not found")
        return 1

    paths, errors = _audio_paths(root, args.targets)
    if errors:
        console = make_console(args.color, stderr=True)
        for error in errors:
            console.print("[red]Error:[/red]", error)
        return 1

    _print_controls(args.color, len(paths))
    try:
        result = subprocess.run(
            [
                executable,
                "--audio-display=no",
                "--autocreate-playlist=no",
                "--msg-level=all=warn,statusline=status",
                f"--term-status-msg=[${{playlist-pos-1}}/{len(paths)}] "
                "${media-title}  ${playback-time} / ${duration}",
                "--",
                *(str(path) for path in paths),
            ],
            check=False,
        )
    except KeyboardInterrupt:
        return 130
    except OSError as error:
        console = make_console(args.color, stderr=True)
        console.print(f"[red]Error:[/red] could not start mpv: {error}")
        return 1
    return result.returncode if result.returncode >= 0 else 128 - result.returncode
