import subprocess
from pathlib import Path

from muse.cli import main
from muse.commands import play


def test_play_expands_targets_in_order_and_invokes_mpv(
    tmp_path: Path, monkeypatch
) -> None:
    album = tmp_path / "backlog" / "Album"
    disc = album / "Disc 2"
    disc.mkdir(parents=True)
    first = album / "01 First.mp3"
    second = disc / "02 Second.FLAC"
    first.write_bytes(b"audio")
    second.write_bytes(b"audio")
    (album / "cover.jpg").write_bytes(b"image")
    calls: list[tuple[list[str], bool]] = []

    monkeypatch.setattr(play.shutil, "which", lambda name: "/usr/bin/mpv")

    def run(arguments: list[str], *, check: bool) -> subprocess.CompletedProcess:
        calls.append((arguments, check))
        return subprocess.CompletedProcess(arguments, 0)

    monkeypatch.setattr(play.subprocess, "run", run)

    result = main(
        [
            "--root",
            str(tmp_path),
            "play",
            "backlog/Album",
            "backlog/Album/01 First.mp3",
        ]
    )

    assert result == 0
    assert calls == [
        (
            [
                "/usr/bin/mpv",
                "--audio-display=no",
                "--autocreate-playlist=no",
                "--msg-level=all=warn,statusline=status",
                "--term-status-msg=[${playlist-pos-1}/2] "
                "${media-title}  ${playback-time} / ${duration}",
                "--",
                str(first),
                str(second),
            ],
            False,
        )
    ]


def test_play_prints_colored_control_cheatsheet(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    track = tmp_path / "song.mp3"
    track.write_bytes(b"audio")
    monkeypatch.setattr(play.shutil, "which", lambda name: "/usr/bin/mpv")
    monkeypatch.setattr(
        play.subprocess,
        "run",
        lambda arguments, *, check: subprocess.CompletedProcess(arguments, 0),
    )

    assert main(["--root", str(tmp_path), "--color", "always", "play", "song.mp3"]) == 0

    error = capsys.readouterr().err
    assert "\x1b[" in error
    assert all(
        text in error
        for text in (
            "1 AUDIO FILE SELECTED",
            "MPV KEYS",
            "SPACE",
            "pause/resume",
            "←/→",
            "9/0",
            "</>",
            "q",
            "quit",
        )
    )


def test_play_requires_mpv(tmp_path: Path, monkeypatch, capsys) -> None:
    track = tmp_path / "song.mp3"
    track.write_bytes(b"audio")
    monkeypatch.setattr(play.shutil, "which", lambda name: None)

    assert main(["--root", str(tmp_path), "play", "song.mp3"]) == 1

    assert "mpv is required" in capsys.readouterr().err


def test_play_rejects_missing_and_non_audio_targets(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    notes = tmp_path / "notes.txt"
    notes.write_text("notes")
    called = False
    monkeypatch.setattr(play.shutil, "which", lambda name: "/usr/bin/mpv")

    def run(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("mpv should not be started")

    monkeypatch.setattr(play.subprocess, "run", run)

    result = main(["--root", str(tmp_path), "play", "missing", "notes.txt"])

    assert result == 1
    assert called is False
    error = " ".join(capsys.readouterr().err.split())
    assert "path does not exist" in error
    assert "no recognized audio files found" in error


def test_play_propagates_mpv_failure(tmp_path: Path, monkeypatch) -> None:
    track = tmp_path / "song.mp3"
    track.write_bytes(b"audio")
    monkeypatch.setattr(play.shutil, "which", lambda name: "/usr/bin/mpv")
    monkeypatch.setattr(
        play.subprocess,
        "run",
        lambda arguments, *, check: subprocess.CompletedProcess(arguments, 2),
    )

    assert main(["--root", str(tmp_path), "play", "song.mp3"]) == 2
