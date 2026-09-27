import json
import struct
from pathlib import Path

from mutagen.id3 import APIC, ID3, TALB, TPE2, TXXX

from muse.art import image_info
from muse.cli import main


def png(width: int, height: int) -> bytes:
    return b"\x89PNG\r\n\x1a\n" + b"\0" * 8 + struct.pack(">II", width, height) + b"\0" * 8


def tagged_mp3(path: Path, cover: bytes | None = None) -> None:
    tags = ID3()
    tags.add(TPE2(encoding=3, text=["Artist"]))
    tags.add(TALB(encoding=3, text=["Album"]))
    if cover:
        tags.add(APIC(encoding=3, mime="image/png", type=3, desc="Cover", data=cover))
    tags.save(path)


def test_fix_art_previews_then_embeds_better_cover(tmp_path: Path, monkeypatch, capsys) -> None:
    root = tmp_path / "vault"
    album = root / "backlog" / "album"
    album.mkdir(parents=True)
    first, second = album / "one.mp3", album / "two.mp3"
    tagged_mp3(first, png(300, 300))
    tagged_mp3(second)
    before = {path: path.read_bytes() for path in (first, second)}
    calls = []

    def get(url: str, **kwargs) -> bytes:
        calls.append(url)
        if "musicbrainz.org" in url:
            return json.dumps(
                {
                    "release-groups": [
                        {
                            "id": "2416c497-32a1-4a13-8ed5-ef197c187e7b",
                            "title": "Album",
                            "score": 100,
                            "artist-credit": [{"name": "Artist"}],
                        }
                    ]
                }
            ).encode()
        return png(1200, 1200)

    monkeypatch.setattr("muse.art._get", get)
    assert main(["--root", str(root), "fix-art", "backlog/album", "--json"]) == 0
    preview = json.loads(capsys.readouterr().out)
    assert [file["status"] for file in preview["albums"][0]["files"]] == ["upgrade", "upgrade"]
    assert {path: path.read_bytes() for path in (first, second)} == before
    assert len(calls) == 2

    assert main(["--root", str(root), "fix-art", "backlog/album", "--apply", "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert [file["status"] for file in result["albums"][0]["files"]] == ["updated", "updated"]
    for path in (first, second):
        assert image_info(ID3(path).getall("APIC")[0].data).width == 1200
    for item in result["albums"][0]["files"]:
        assert (root / item["backup"]).read_bytes() == before[root / item["path"]]


def test_fix_art_requires_approval_for_smaller_cover_and_ignores_symlinks(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    root = tmp_path / "vault"
    album = root / "backlog" / "album"
    album.mkdir(parents=True)
    song = album / "song.mp3"
    tagged_mp3(song, png(1500, 1500))
    (album / "linked.mp3").symlink_to(song)
    before = song.read_bytes()
    monkeypatch.setattr(
        "muse.art.find_art", lambda *args: ("test", image_info(png(1200, 1200)), None)
    )

    assert main(["--root", str(root), "fix-art", "backlog/album", "--apply", "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["files"] == 1
    assert result["albums"][0]["files"][0]["status"] == "needs-approval"
    assert song.read_bytes() == before


def test_fix_art_uses_tagged_release_id_without_search(tmp_path: Path, monkeypatch, capsys) -> None:
    root = tmp_path / "vault"
    song = root / "backlog" / "song.mp3"
    song.parent.mkdir(parents=True)
    tagged_mp3(song)
    tags = ID3(song)
    release_id = "2416c497-32a1-4a13-8ed5-ef197c187e7b"
    tags.add(TXXX(encoding=3, desc="MusicBrainz Album Id", text=[release_id]))
    tags.save(song)
    urls = []

    def get(url: str, **kwargs) -> bytes:
        urls.append(url)
        return png(1200, 1200)

    monkeypatch.setattr("muse.art._get", get)
    assert main(["--root", str(root), "fix-art", "backlog/song.mp3", "--json"]) == 0
    capsys.readouterr()
    assert urls == [f"https://coverartarchive.org/release/{release_id}/front"]


def test_fix_art_accepts_unique_completion_of_truncated_title(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    root = tmp_path / "vault"
    song = root / "backlog" / "song.mp3"
    song.parent.mkdir(parents=True)
    tagged_mp3(song)
    tags = ID3(song)
    tags.setall("TALB", [TALB(encoding=3, text=["Kyoretsu Na Rhythm - Characte"])])
    tags.save(song)
    calls = []

    def get(url: str, **kwargs) -> bytes:
        calls.append(url)
        if "musicbrainz.org" in url:
            matches = (
                []
                if len(calls) == 1
                else [
                    {
                        "id": "2416c497-32a1-4a13-8ed5-ef197c187e7b",
                        "title": "Kyoretsu Na Rhythm - Characters",
                        "artist-credit": [{"name": "Artist"}],
                    }
                ]
            )
            return json.dumps({"release-groups": matches}).encode()
        return png(1200, 1200)

    monkeypatch.setattr("muse.art._get", get)
    assert main(["--root", str(root), "fix-art", "backlog/song.mp3", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["albums"][0]["files"][0]["status"] == "upgrade"
    assert report["albums"][0]["matched_title"] == "Kyoretsu Na Rhythm - Characters"
    assert len(calls) == 3


def test_fix_art_reports_no_match_as_skipped(tmp_path: Path, monkeypatch, capsys) -> None:
    root = tmp_path / "vault"
    song = root / "backlog" / "song.mp3"
    song.parent.mkdir(parents=True)
    tagged_mp3(song)
    before = song.read_bytes()
    monkeypatch.setattr("muse.art._get", lambda *args, **kwargs: b'{"release-groups": []}')

    assert main(["--root", str(root), "fix-art", "backlog/song.mp3", "--apply", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["albums"] == []
    assert report["errors"] == []
    assert report["skipped"][0]["reason"] == "no confident MusicBrainz release match"
    assert song.read_bytes() == before


def test_fix_art_accepts_manual_cover_without_network(tmp_path: Path, monkeypatch, capsys) -> None:
    root = tmp_path / "vault"
    song = root / "backlog" / "song.mp3"
    song.parent.mkdir(parents=True)
    tagged_mp3(song)
    cover = tmp_path / "front.png"
    cover.write_bytes(png(1000, 1000))

    def unexpected(*args, **kwargs):
        raise AssertionError("manual cover must not query the network")

    monkeypatch.setattr("muse.art._get", unexpected)
    assert (
        main(
            [
                "--root",
                str(root),
                "fix-art",
                "backlog/song.mp3",
                "--cover",
                str(cover),
                "--apply",
                "--json",
            ]
        )
        == 0
    )
    report = json.loads(capsys.readouterr().out)
    assert report["albums"][0]["files"][0]["status"] == "updated"
    assert image_info(ID3(song).getall("APIC")[0].data).width == 1000


def test_fix_art_asks_before_using_below_minimum_art(tmp_path: Path, monkeypatch, capsys) -> None:
    root = tmp_path / "vault"
    song = root / "backlog" / "song.mp3"
    song.parent.mkdir(parents=True)
    tagged_mp3(song, png(200, 200))
    monkeypatch.setattr(
        "muse.art.find_art", lambda *args: ("test", image_info(png(600, 597)), None)
    )
    monkeypatch.setattr("muse.commands.art._can_prompt", lambda: True)
    prompts = []

    def answer(prompt: str) -> str:
        prompts.append(prompt)
        return "yes"

    monkeypatch.setattr("builtins.input", answer)
    assert main(["--root", str(root), "fix-art", "backlog/song.mp3", "--apply"]) == 0
    capsys.readouterr()
    assert "600x597" in prompts[0]
    assert "600px minimum" in prompts[0]
    assert image_info(ID3(song).getall("APIC")[0].data).height == 597


def test_fix_art_declining_small_art_changes_nothing(tmp_path: Path, monkeypatch, capsys) -> None:
    root = tmp_path / "vault"
    song = root / "backlog" / "song.mp3"
    song.parent.mkdir(parents=True)
    tagged_mp3(song)
    before = song.read_bytes()
    monkeypatch.setattr(
        "muse.art.find_art", lambda *args: ("test", image_info(png(600, 597)), None)
    )
    monkeypatch.setattr("muse.commands.art._can_prompt", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt: "no")
    assert main(["--root", str(root), "fix-art", "backlog/song.mp3", "--apply"]) == 0
    assert "declined" in capsys.readouterr().out
    assert song.read_bytes() == before


def test_fix_art_json_needs_explicit_small_art_approval(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    root = tmp_path / "vault"
    song = root / "backlog" / "song.mp3"
    song.parent.mkdir(parents=True)
    tagged_mp3(song)
    monkeypatch.setattr(
        "muse.art.find_art", lambda *args: ("test", image_info(png(600, 597)), None)
    )
    monkeypatch.setattr("muse.commands.art._can_prompt", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt: 1 / 0)
    assert main(["--root", str(root), "fix-art", "backlog/song.mp3", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["albums"][0]["files"][0]["status"] == "needs-approval"
    assert "--allow-small" in report["albums"][0]["files"][0]["reason"]
    assert (
        main(
            [
                "--root",
                str(root),
                "fix-art",
                "backlog/song.mp3",
                "--allow-small",
                "--apply",
                "--json",
            ]
        )
        == 0
    )
    report = json.loads(capsys.readouterr().out)
    assert report["albums"][0]["files"][0]["status"] == "updated"


def test_fix_art_can_approve_smaller_art_over_larger_embedded_art(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    root = tmp_path / "vault"
    album = root / "backlog" / "album"
    album.mkdir(parents=True)
    songs = [album / "one.mp3", album / "two.mp3"]
    for song in songs:
        tagged_mp3(song, png(1893, 3872))
    monkeypatch.setattr(
        "muse.art.find_art", lambda *args: ("test", image_info(png(600, 597)), None)
    )
    monkeypatch.setattr("muse.commands.art._can_prompt", lambda: True)
    prompts = []

    def answer(prompt: str) -> str:
        prompts.append(prompt)
        return "y"

    monkeypatch.setattr("builtins.input", answer)
    assert main(["--root", str(root), "fix-art", "backlog/album", "--apply", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["albums"][0]["files"][0]["status"] == "needs-approval"
    assert not prompts

    assert main(["--root", str(root), "fix-art", "backlog/album", "--apply"]) == 0
    capsys.readouterr()
    assert len(prompts) == 1
    assert "600x597" in prompts[0]
    assert "1893x3872" in prompts[0]
    assert "below the 600px minimum" in prompts[0]
    for song in songs:
        assert image_info(ID3(song).getall("APIC")[0].data).height == 597
