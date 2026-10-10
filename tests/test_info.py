import json
import wave
from pathlib import Path

from mutagen.id3 import APIC, COMM, TALB, TCON, TDRC, TIT2, TPE1, TPE2, TPOS, TRCK, TXXX
from mutagen.wave import WAVE

from muse.cli import main


def make_track(path: Path, number: str, *, artwork: bool = False, album: str = "Album") -> None:
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(8000)
        output.writeframes(b"\0\0" * 8000)
    audio = WAVE(path)
    audio.add_tags()
    assert audio.tags is not None
    for frame in (
        TIT2(encoding=3, text=[f"Song {number}"]),
        TPE1(encoding=3, text=["Singer"]),
        TPE2(encoding=3, text=["Band"]),
        TALB(encoding=3, text=[album]),
        TRCK(encoding=3, text=[number]),
        TPOS(encoding=3, text=["1/1"]),
        TDRC(encoding=3, text=["2024"]),
        TCON(encoding=3, text=["Rock"]),
        COMM(encoding=3, lang="eng", desc="", text=["Album version"]),
        TXXX(encoding=3, desc="MusicBrainz Album Id", text=["release-id"]),
    ):
        audio.tags.add(frame)
    if artwork:
        audio.tags.add(APIC(encoding=3, mime="image/png", type=3, desc="Cover", data=b"image"))
    audio.save()


def test_info_reports_tracks_tags_art_and_numbering_gaps(tmp_path: Path, capsys) -> None:
    album = tmp_path / "backlog" / "Album"
    album.mkdir(parents=True)
    make_track(album / "one.wav", "1/3", artwork=True)
    make_track(album / "three.wav", "3/3")
    (album / "cover.jpg").write_bytes(b"image")
    before = {path.name: path.read_bytes() for path in album.iterdir()}

    assert main(["--root", str(tmp_path), "info", "backlog/Album", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["track_count"] == 2
    assert report["duration_seconds"] == 2.0
    assert [track["track"] for track in report["tracks"]] == [1, 3]
    assert report["tracks"][0]["fields"]["musicbrainz_release_id"] == "release-id"
    assert report["tracks"][0]["fields"]["comment"] == "Album version"
    assert report["tracks"][0]["artwork"] == "embedded (1)"
    assert "artwork" in report["tracks"][1]["missing"]
    assert report["findings"] == ["disc 1: missing track numbers 2"]
    assert report["album"] == "Album"
    assert report["album_artist"] == "Band"
    assert report["missing_counts"] == {"artwork": 1}
    assert report["other_files"] == ["cover.jpg"]
    assert report["state_created"] is False
    assert {path.name: path.read_bytes() for path in album.iterdir()} == before


def test_info_text_shows_missing_fields_and_inconsistent_album(tmp_path: Path, capsys) -> None:
    album = tmp_path / "album"
    album.mkdir()
    make_track(album / "one.wav", "1/2", album="First")
    make_track(album / "two.wav", "2/2", album="Second")

    assert main(["--root", str(tmp_path), "--color", "never", "info", "album"]) == 0
    output = capsys.readouterr().out
    assert "Song 1" in output
    assert "Album version" not in output
    assert "inconsistent album" in output
    assert "Missing: artwork: 2" in output
    assert "artwork" in output
    assert "1.00s" in output
    assert output.count("one.wav") == 1
    assert output.count("two.wav") == 1

    assert main(["--root", str(tmp_path), "--color", "never", "info", "album", "--all"]) == 0
    detailed = capsys.readouterr().out
    assert "Album version" in detailed
    assert "8000 Hz" in detailed
    assert detailed.count("one.wav") == 2


def test_info_missing_target_and_unreadable_track(tmp_path: Path, capsys) -> None:
    assert main(["--root", str(tmp_path), "info", "missing", "--json"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["errors"][0]["message"] == "path does not exist"

    album = tmp_path / "album"
    album.mkdir()
    (album / "broken.mp3").write_bytes(b"not audio")
    assert main(["--root", str(tmp_path), "info", "album", "--json"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["errors"]


def test_info_flags_untagged_totals_and_duplicate_track_numbers(tmp_path: Path, capsys) -> None:
    album = tmp_path / "album"
    album.mkdir()
    make_track(album / "a.wav", "1")
    make_track(album / "b.wav", "1")

    assert main(["--root", str(tmp_path), "info", "album", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["missing_counts"] == {"artwork": 2, "track total": 2}
    assert "disc 1: duplicate track numbers 1" in report["findings"]
