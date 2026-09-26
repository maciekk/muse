from pathlib import Path

from muse.slag import apply, candidates


def test_slag_copies_non_audio_with_backlog_provenance(tmp_path: Path) -> None:
    root = tmp_path / "music-vault"
    source = root / "backlog" / "pc" / "saves" / "game.sav"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"save")
    (root / "backlog" / "pc" / "song.flac").write_bytes(b"audio")
    (root / "backlog" / "pc" / "cover.jpg").write_bytes(b"art")
    (root / "backlog" / "pc" / "checksums.sfv").write_bytes(b"checksums")

    copies = candidates(root, [root / "backlog" / "pc"])

    assert [(item.source.name, item.destination.relative_to(root)) for item in copies] == [
        ("game.sav", Path("slag/pc/saves/game.sav"))
    ]
    thorough = candidates(root, [root / "backlog" / "pc"], thorough=True)
    assert {item.source.name for item in thorough} == {"game.sav", "cover.jpg", "checksums.sfv"}

    assert apply(copies) == (1, 0)
    assert not source.exists()
