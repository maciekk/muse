import pytest

from muse.commands import progress as terminal_progress
from muse.scanning import ScanProgress


def test_auto_progress_delay_and_forced_stderr(monkeypatch, capsys) -> None:
    now = [0.0]
    monkeypatch.setattr(terminal_progress, "monotonic", lambda: now[0])
    monkeypatch.setattr(terminal_progress.sys.stderr, "isatty", lambda: True)

    auto = terminal_progress._ScanProgressDisplay("auto", "never")
    auto.update(ScanProgress(completed_files=1))
    assert not auto.running
    now[0] = 2.0
    auto.update(ScanProgress(completed_files=2))
    assert auto.running
    auto.stop()
    assert not auto.running

    forced = terminal_progress._ScanProgressDisplay("always", "never")
    forced.update(ScanProgress(completed_files=1))
    assert forced.running
    forced.stop()
    captured = capsys.readouterr()
    assert captured.err
    assert captured.out == ""


def test_progress_stops_on_scan_failure(tmp_path, monkeypatch) -> None:
    from muse.cli import main

    stopped = []
    original_stop = terminal_progress._ScanProgressDisplay.stop

    def stop(self):
        stopped.append(self.running)
        original_stop(self)

    monkeypatch.setattr(terminal_progress._ScanProgressDisplay, "stop", stop)
    def fail(*_args, **_kwargs):
        _kwargs["progress"](ScanProgress(completed_files=1))
        raise OSError("scan failed")

    monkeypatch.setattr("muse.commands.scan.compare_with_vault", fail)
    with pytest.raises(OSError, match="scan failed"):
        main(["--root", str(tmp_path), "scan", str(tmp_path), "--progress", "always"])
    assert stopped == [True]
