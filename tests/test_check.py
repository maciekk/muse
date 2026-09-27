import json
import shutil
import subprocess
import wave
from pathlib import Path

import pytest

from muse.cli import main


@pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg and ffprobe are required",
)
def test_check_recurses_and_detects_corrupt_audio(tmp_path: Path, capsys) -> None:
    album = tmp_path / "album"
    nested = album / "disc"
    nested.mkdir(parents=True)
    good = nested / "good.flac"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=duration=1",
         "-c:a", "flac", str(good)],
        check=True,
    )
    damaged = nested / "damaged.flac"
    damaged.write_bytes(good.read_bytes()[: good.stat().st_size // 2])
    (album / "cover.jpg").write_bytes(b"image")

    assert main(["check", str(album), "--json"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["checked"] == 2
    assert report["passed"] == 1
    assert report["failed"] == 1
    assert report["skipped"] == 1
    assert report["failures"][0]["path"] == str(damaged)
    assert good.is_file() and damaged.is_file()

    assert main(["check", str(good), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["passed"] == 1


def test_check_rejects_missing_or_non_audio_target(tmp_path: Path, capsys) -> None:
    assert main(["check", str(tmp_path / "missing"), "--json"]) == 1
    assert json.loads(capsys.readouterr().out)["errors"]

    other = tmp_path / "notes.txt"
    other.write_text("text")
    assert main(["check", str(other), "--json"]) == 1
    assert "recognized audio" in json.loads(capsys.readouterr().out)["errors"][0]["message"]


@pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg and ffprobe are required",
)
def test_check_rejects_empty_wav(tmp_path: Path, capsys) -> None:
    empty = tmp_path / "empty.wav"
    with wave.open(str(empty), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(44100)

    assert main(["check", str(empty), "--json"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert "no audio samples" in report["failures"][0]["message"]


@pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg and ffprobe are required",
)
def test_check_detects_mp3_duration_longer_than_decoded_audio(tmp_path: Path, capsys) -> None:
    track = tmp_path / "bad-duration.mp3"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=duration=2",
         "-c:a", "libmp3lame", str(track)],
        check=True,
    )
    data = bytearray(track.read_bytes())
    header = data.find(b"Info")
    if header < 0:
        header = data.find(b"Xing")
    assert header >= 0
    # Inflate the Xing/Info frame count without changing any audio frames.
    data[header + 8 : header + 12] = (300).to_bytes(4, "big")
    track.write_bytes(data)

    assert main(["check", str(track), "--json"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert "duration mismatch" in report["failures"][0]["message"]
    assert "decoded 2.0s" in report["failures"][0]["message"]
