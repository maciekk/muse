from pathlib import Path

from muse.search import search_vault


def test_search_finds_case_insensitive_file_and_directory_names(tmp_path: Path) -> None:
    album = tmp_path / "backlog" / "Game Music Remixes"
    album.mkdir(parents=True)
    (album / "Battle Theme.FLAC").write_bytes(b"audio")
    (tmp_path / "master").mkdir()
    (tmp_path / "master" / "quiet theme.mp3").write_bytes(b"audio")

    matches, errors = search_vault(tmp_path, ["THEME", "-game"])

    assert errors == []
    assert [(match.path, match.kind) for match in matches] == [
        ("master/quiet theme.mp3", "file")
    ]

    matches, errors = search_vault(tmp_path, ["music", "remix"])
    assert errors == []
    assert [(match.path, match.kind) for match in matches] == [
        ("backlog/Game Music Remixes", "directory")
    ]

    matches, errors = search_vault(tmp_path, ["-theme"])
    assert errors == []
    assert all("theme" not in match.path.casefold() for match in matches)


def test_search_ignores_internal_state_and_does_not_follow_symlinks(tmp_path: Path) -> None:
    (tmp_path / "backlog").mkdir()
    hidden = tmp_path / ".muse" / "Secret Album"
    hidden.mkdir(parents=True)
    link = tmp_path / "backlog" / "Secret Link"
    link.symlink_to(hidden, target_is_directory=True)

    matches, errors = search_vault(tmp_path, ["secret"])

    assert errors == []
    assert [(match.path, match.kind) for match in matches] == [
        ("backlog/Secret Link", "symlink")
    ]
