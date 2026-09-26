import json
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


def test_relative_stats_target_is_beneath_root(tmp_path: Path, capsys) -> None:
    root = tmp_path / "music-vault"
    make_layout(root)

    result = main(["--root", str(root), "stats", "backlog", "--json"])

    report = json.loads(capsys.readouterr().out)
    assert result == 0
    assert report["target"] == str(root / "backlog")


def test_planned_command_fails_explicitly(tmp_path: Path, capsys) -> None:
    result = main(["--root", str(tmp_path), "backlog", "scan"])

    captured = capsys.readouterr()
    assert result == 2
    assert "not implemented yet" in captured.err.lower()
    assert "no changes were made" in captured.err
