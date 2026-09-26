"""Terminal progress for long-running commands."""

from __future__ import annotations

import sys
from time import monotonic

from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
    TimeRemainingColumn,
)

from muse.compaction import CompactProgress
from muse.duplicates import ProgressUpdate
from muse.reporting import (
    human_bytes,
    human_number,
    make_console,
)
from muse.scanning import PullProgress, ScanProgress


class _ProgressDisplay:
    """Own Rich's stderr lifecycle and the common auto display policy."""

    _DELAY_SECONDS = 2.0

    def __init__(self, mode: str, color: str, *columns: object) -> None:
        self.enabled = mode == "always" or (mode == "auto" and sys.stderr.isatty())
        self.immediate = mode == "always"
        self.started_at = monotonic()
        self.task_id: int | None = None
        self.phase: str | None = None
        self.progress = Progress(*columns, console=make_console(color, stderr=True), transient=True)
        self.running = False

    def _start(self, *, predicted_long: bool = False) -> bool:
        if not self.enabled:
            return False
        if self.running:
            return True
        due = monotonic() - self.started_at >= self._DELAY_SECONDS
        if not (self.immediate or predicted_long or due):
            return False
        self.progress.start()
        self.running = True
        return True

    def stop(self) -> None:
        if self.running:
            self.progress.stop()
            self.running = False


class _ScanProgressDisplay(_ProgressDisplay):
    """Indeterminate progress bar for walking an external target."""

    def __init__(self, mode: str, color: str) -> None:
        super().__init__(
            mode,
            color,
            SpinnerColumn(),
            TextColumn("{task.description}"),
            BarColumn(),
        )

    def update(self, update: ScanProgress) -> None:
        if update.complete:
            self.stop()
            return
        if not self._start():
            return
        description = (
            f"Scanning target — {human_number(update.completed_files)} files, "
            f"{human_bytes(update.completed_bytes)}"
        )
        if self.task_id is None:
            self.task_id = self.progress.add_task(description, total=None)
        else:
            self.progress.update(self.task_id, description=description)


class _PullProgressDisplay(_ProgressDisplay):
    """Determinate progress bar for copying external content into backlog."""

    def __init__(self, mode: str, color: str) -> None:
        super().__init__(
            mode,
            color,
            TextColumn("{task.description}"),
            BarColumn(),
            TaskProgressColumn(),
            TimeRemainingColumn(),
        )

    def update(self, update: PullProgress) -> None:
        if update.complete:
            self.stop()
            return
        if not self._start(predicted_long=update.total_bytes >= 1024**3):
            return
        description = (
            f"Pulling into backlog — {human_number(update.completed_files)}/"
            f"{human_number(update.total_files)} files"
        )
        if self.task_id is None:
            self.task_id = self.progress.add_task(
                description,
                total=max(update.total_bytes, 1),
            )
        else:
            self.progress.update(
                self.task_id,
                completed=update.completed_bytes,
                description=description,
            )


class _DuplicateProgressDisplay(_ProgressDisplay):
    """Delayed terminal progress for duplicate scans."""

    _LONG_BYTES = 1024**3
    _LONG_FILES = 1_000

    def __init__(self, mode: str, color: str, *, trees: bool = False) -> None:
        self.trees = trees
        super().__init__(
            mode,
            color,
            SpinnerColumn(),
            TextColumn("{task.description}"),
            BarColumn(),
            TaskProgressColumn(),
            TimeRemainingColumn(),
        )

    def _should_start(self, update: ProgressUpdate) -> bool:
        predicted_long = update.phase == "hashing" and (
            (update.total_bytes or 0) >= self._LONG_BYTES
            or (update.total_files or 0) >= self._LONG_FILES
        )
        return self._start(predicted_long=predicted_long)

    def _description(self, update: ProgressUpdate) -> str:
        if update.phase == "inventory":
            return (
                f"Inventorying files — {human_number(update.completed_files)} files, "
                f"{human_bytes(update.completed_bytes)}"
            )
        if update.phase == "hashing":
            total_files = human_number(update.total_files or 0)
            cache_total = update.cached_files + (update.total_files or 0)
            cache_rate = update.cached_files / cache_total if cache_total else 0.0
            return (
                f"{'Hashing content' if self.trees else 'Lazy-hashing'} — "
                f"{human_number(update.completed_files)}/{total_files} files, "
                f"{human_bytes(update.completed_bytes)}/{human_bytes(update.total_bytes or 0)}, "
                f"cache {cache_rate:.1%}, using {update.worker_threads} worker "
                f"{'thread' if update.worker_threads == 1 else 'threads'}"
            )
        return (
            f"Analyzing hashes — {human_number(update.completed_files)}/"
            f"{human_number(update.total_files or 0)}"
        )

    def update(self, update: ProgressUpdate) -> None:
        if update.phase == "complete":
            return
        if not self.running and not self._should_start(update):
            return

        if update.phase != self.phase:
            if self.task_id is not None:
                self.progress.remove_task(self.task_id)
            total = update.total_bytes if update.phase == "hashing" else update.total_files
            self.task_id = self.progress.add_task(self._description(update), total=total)
            self.phase = update.phase

        if self.task_id is None:
            return
        completed = update.completed_bytes if update.phase == "hashing" else update.completed_files
        self.progress.update(
            self.task_id,
            description=self._description(update),
            completed=completed,
            total=update.total_bytes if update.phase == "hashing" else update.total_files,
        )


class _CompactProgressDisplay(_ProgressDisplay):
    """Operation-level progress for compaction verification and trash moves."""

    def __init__(self, color: str) -> None:
        super().__init__(
            "auto",
            color,
            SpinnerColumn(),
            TextColumn("{task.description}"),
            BarColumn(),
            TaskProgressColumn(),
        )

    def update(self, update: CompactProgress) -> None:
        if not self._start(predicted_long=update.total_operations >= 1000):
            return
        if update.phase != self.phase:
            if self.task_id is not None:
                self.progress.remove_task(self.task_id)
            description = (
                f"Reverifying duplicate trees — using {update.worker_threads} worker "
                f"{'thread' if update.worker_threads == 1 else 'threads'}"
                if update.phase == "verify"
                else "Moving duplicate trees to trash"
            )
            self.task_id = self.progress.add_task(description, total=update.total_operations)
            self.phase = update.phase
        if self.task_id is not None:
            self.progress.update(
                self.task_id,
                completed=update.completed_operations,
                total=update.total_operations,
            )


class _SlagProgressDisplay(_ProgressDisplay):
    """Byte progress for artifact copies."""

    def __init__(self, color: str, total_bytes: int) -> None:
        super().__init__(
            "auto",
            color,
            SpinnerColumn(),
            TextColumn("{task.description}"),
            BarColumn(),
            TaskProgressColumn(),
            TimeRemainingColumn(),
        )
        self.total_bytes = total_bytes

    def update(self, completed: int, _size: int) -> None:
        if not self._start(predicted_long=self.total_bytes >= 1024**3):
            return
        if self.task_id is None:
            self.task_id = self.progress.add_task(
                "Moving into slag", total=max(self.total_bytes, 1)
            )
        self.progress.update(self.task_id, completed=completed)
