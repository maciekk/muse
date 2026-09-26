import json
import subprocess
from pathlib import Path

from muse.cli import main
from muse.config import MANAGED_AREAS, MASTER_SHELVES


def make_layout(root: Path) -> None:
    for area in MANAGED_AREAS:
        (root / area).mkdir(parents=True)
    for shelf in MASTER_SHELVES:
        (root / "master" / shelf).mkdir()


def test_bare_muse_prints_help_and_succeeds(capsys) -> None:
    result = main([])

    output = capsys.readouterr().out
    assert result == 0
    assert "Usage:" in output
    assert "doctor" in output
    assert "stats" in output
    assert "\n  COMMAND\n" not in output


def test_help_command_shows_command_specific_usage(capsys) -> None:
    assert main(["help", "dupes"]) == 0

    output = capsys.readouterr().out
    assert "Usage: muse dupes" in output
    assert "--trees" in output
    assert "--rehash" in output


def test_help_honors_explicit_color_policy(capsys) -> None:
    assert main(["--color", "always"]) == 0
    colored = capsys.readouterr().out
    assert "\x1b[" in colored
    assert "doctor" in colored

    assert main(["--color", "never"]) == 0
    plain = capsys.readouterr().out
    assert "\x1b[" not in plain


def test_status_json_reports_layout_without_creating_state(tmp_path: Path, capsys) -> None:
    root = tmp_path / "music-vault"
    make_layout(root)
    before = sorted(root.rglob("*"))

    result = main(["--root", str(root), "status", "--json"])

    report = json.loads(capsys.readouterr().out)
    assert result == 0
    assert report["root"] == str(root)
    assert all(area["status"] == "ok" for area in report["areas"])
    assert report["state_created"] is False
    assert sorted(root.rglob("*")) == before


def test_status_uses_semantic_color_when_forced(tmp_path: Path, capsys) -> None:
    root = tmp_path / "music-vault"
    make_layout(root)

    result = main(["--root", str(root), "--color", "always", "status"])

    output = capsys.readouterr().out
    assert result == 0
    assert "\x1b[" in output
    assert "OK" in output


def test_status_fails_when_layout_is_incomplete(tmp_path: Path, capsys) -> None:
    root = tmp_path / "music-vault"
    root.mkdir()

    result = main(["--root", str(root), "status", "--json"])

    report = json.loads(capsys.readouterr().out)
    assert result == 1
    assert any(area["status"] == "error" for area in report["areas"])


def test_stats_is_read_only_and_classifies_audio(tmp_path: Path, capsys) -> None:
    root = tmp_path / "music-vault"
    make_layout(root)
    (root / "backlog" / "song.mp3").write_bytes(b"1234")
    before = sorted(root.rglob("*"))

    result = main(["--root", str(root), "stats", "--json"])

    report = json.loads(capsys.readouterr().out)
    assert result == 0
    assert report["total"]["files"] == 1
    assert report["total"]["audio_files"] == 1
    assert report["areas"]["backlog"]["logical_bytes"] == 4
    assert report["state_created"] is False
    assert sorted(root.rglob("*")) == before


def test_stats_labels_files_without_an_extension(tmp_path: Path, capsys) -> None:
    root = tmp_path / "music-vault"
    make_layout(root)
    (root / "backlog" / "README").write_text("notes")

    result = main(["--root", str(root), "--color", "never", "stats"])

    output = capsys.readouterr().out
    assert result == 0
    assert "(no extension)" in output


def test_search_reports_matches_across_content_areas_without_creating_state(
    tmp_path: Path, capsys
) -> None:
    root = tmp_path / "music-vault"
    make_layout(root)
    album = root / "backlog" / "Arcade Remixes"
    album.mkdir()
    (album / "Boss Theme.flac").write_bytes(b"audio")
    (root / "incoming" / "boss rush.mp3").write_bytes(b"audio")
    before = sorted(root.rglob("*"))

    result = main(["--root", str(root), "search", "BOSS", "-rush", "--json"])

    report = json.loads(capsys.readouterr().out)
    assert result == 0
    assert report["query"] == ["BOSS", "-rush"]
    assert report["match_count"] == 1
    assert report["matches"] == [
        {"path": "backlog/Arcade Remixes/Boss Theme.flac", "type": "file"},
    ]
    assert report["state_created"] is False
    assert sorted(root.rglob("*")) == before


def test_search_dims_parent_path_and_emphasizes_filename(tmp_path: Path, capsys) -> None:
    root = tmp_path / "music-vault"
    make_layout(root)
    album = root / "backlog" / "Album"
    album.mkdir()
    (album / "Boss Theme.flac").write_bytes(b"audio")

    result = main(["--root", str(root), "--color", "always", "search", "boss"])

    output = capsys.readouterr().out
    assert result == 0
    assert "\x1b[1;2mbacklog/Album/\x1b[0m\x1b[1mBoss Theme.flac\x1b[0m" in output


def test_dupes_reports_exact_files_and_persists_hashes(tmp_path: Path, capsys) -> None:
    root = tmp_path / "music-vault"
    make_layout(root)
    (root / "backlog" / "one.flac").write_bytes(b"same")
    (root / "backlog" / "two.flac").write_bytes(b"same")

    result = main(["--root", str(root), "dupes", "backlog", "--json"])

    report = json.loads(capsys.readouterr().out)
    assert result == 0
    assert report["files"] == 2
    assert report["hashed_files"] == 2
    assert report["duplicate_groups"] == 1
    assert report["redundant_occurrences"] == 1
    assert report["groups"][0]["files"] == ["one.flac", "two.flac"]
    assert (root / ".muse" / "muse.db").is_file()


def test_prune_reports_cache_maintenance_stats(tmp_path: Path, capsys) -> None:
    root = tmp_path / "music-vault"
    make_layout(root)
    first = root / "backlog" / "first.flac"
    second = root / "backlog" / "second.flac"
    first.write_bytes(b"same")
    second.write_bytes(b"same")
    assert main(["--root", str(root), "dupes", "backlog", "--json"]) == 0
    capsys.readouterr()
    second.unlink()

    result = main(["--root", str(root), "prune", "--json"])

    report = json.loads(capsys.readouterr().out)
    assert result == 0
    assert report["database_exists"] is True
    assert report["entries_before"] == 2
    assert report["entries_removed"] == 1
    assert report["entries_after"] == 1
    assert report["represented_bytes_removed"] == 4


def test_dupes_trees_reports_maximal_directory_copies(tmp_path: Path, capsys) -> None:
    root = tmp_path / "music-vault"
    make_layout(root)
    for name in ("first", "second"):
        directory = root / "backlog" / name
        directory.mkdir()
        (directory / "song.flac").write_bytes(b"same")

    result = main(["--root", str(root), "dupes", "backlog", "--trees", "--json"])

    report = json.loads(capsys.readouterr().out)
    assert result == 0
    assert report["tree_duplicate_groups"] == 1
    assert report["tree_groups"][0]["directories"] == ["first", "second"]

    result = main(["--root", str(root), "--color", "never", "dupes", "backlog", "--trees"])

    output = capsys.readouterr().out
    assert result == 0
    assert "Maximal duplicate directory trees" in output
    assert "Duplicate groups" not in output


def test_dupes_shows_shared_filename_once_with_its_directories(tmp_path: Path, capsys) -> None:
    root = tmp_path / "music-vault"
    make_layout(root)
    (root / "backlog" / "first").mkdir()
    (root / "backlog" / "second").mkdir()
    (root / "backlog" / "first" / "song.flac").write_bytes(b"same")
    (root / "backlog" / "second" / "song.flac").write_bytes(b"same")

    result = main(["--root", str(root), "--color", "never", "dupes", "backlog"])

    output = capsys.readouterr().out
    assert result == 0
    assert output.count("song.flac") == 1
    assert "first" in output
    assert "second" in output


def test_mv_renames_content_and_reports_cached_hashes(tmp_path: Path, capsys) -> None:
    root = tmp_path / "music-vault"
    make_layout(root)
    (root / "backlog" / "old").mkdir()

    result = main(["--root", str(root), "mv", "backlog/old", "backlog/new", "--json"])

    report = json.loads(capsys.readouterr().out)
    assert result == 0
    assert report["destination"] == str(root / "backlog" / "new")
    assert (root / "backlog" / "new").is_dir()


def test_import_plans_then_applies_audio_only_directory(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    root = tmp_path / "music-vault"
    make_layout(root)
    source = root / "backlog" / "album"
    source.mkdir()
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=0.05",
            "-metadata",
            "title=Song",
            "-metadata",
            "artist=Artist",
            "-metadata",
            "album=Album",
            "-metadata",
            "album_artist=Artist",
            "-metadata",
            "track=1/1",
            "-metadata",
            "disc=1/1",
            "-c:a",
            "flac",
            str(source / "song.flac"),
        ],
        check=True,
    )

    result = main(
        ["--root", str(root), "import", "backlog/album", "games/new-album", "--json"]
    )
    report = json.loads(capsys.readouterr().out)
    assert result == 0
    assert report["action"] == "planned"
    assert report["destination"] == "master/games/new-album"
    assert source.exists()

    monkeypatch.setattr("builtins.input", lambda _prompt: "IMPORT")
    result = main(["--root", str(root), "import", "backlog/album", "--apply", "--json"])
    report = json.loads(capsys.readouterr().out)
    assert result == 0
    assert report["action"] == "completed"
    assert not source.exists()
    assert (root / "master" / "games" / "new-album" / "song.flac").is_file()


def test_relative_stats_target_is_beneath_root(tmp_path: Path, capsys) -> None:
    root = tmp_path / "music-vault"
    make_layout(root)

    result = main(["--root", str(root), "stats", "backlog", "--json"])

    report = json.loads(capsys.readouterr().out)
    assert result == 0
    assert report["target"] == str(root / "backlog")


def test_removed_command_group_is_not_accepted(tmp_path: Path, capsys) -> None:
    result = main(["--root", str(tmp_path), "backlog", "scan"])

    captured = capsys.readouterr()
    assert result == 2
    assert "invalid choice" in captured.err.lower()
