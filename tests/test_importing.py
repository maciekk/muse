import subprocess
from pathlib import Path

import pytest

from muse.importing import abort_plan, apply_plan, load_plan, make_plan, plan_path


def make_track(
    path: Path,
    track: int,
    *,
    album: str = "Album",
    codec: str = "flac",
    total: int = 2,
    sample_rate: int | None = None,
    album_artist: str = "Artist",
) -> None:
    command = [
        "ffmpeg",
        "-y",
        "-v",
        "error",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:duration=0.05",
        "-metadata",
        f"title=Track {track}",
        "-metadata",
        "artist=Artist",
        "-metadata",
        f"album={album}",
        "-metadata",
        f"album_artist={album_artist}",
        "-metadata",
        f"track={track}/{total}",
        "-metadata",
        "disc=1/1",
        "-c:a",
        codec,
    ]
    if sample_rate is not None:
        command.extend(("-ar", str(sample_rate)))
    subprocess.run([*command, str(path)], check=True)


def make_source(root: Path, name: str = "album") -> Path:
    source = root / "backlog" / name
    source.mkdir(parents=True)
    make_track(source / "01.flac", 1)
    make_track(source / "02.flac", 2)
    (root / "master" / "games").mkdir(parents=True)
    return source


def test_plan_and_apply_audio_only_import(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    source = make_source(root)

    plan = make_plan(root, source, "games/")

    assert plan.source == "backlog/album"
    assert plan.destination == "master/games/album"
    assert plan.state == "ready"
    assert plan.profile == "release"
    assert len(plan.files) == 2
    assert source.is_dir()

    completed = apply_plan(root)

    assert completed.state == "completed"
    assert not source.exists()
    assert (root / "master" / "games" / "album" / "01.flac").is_file()
    assert plan.files[0].media.codec == "flac"
    assert plan.files[0].media.track_number == 1
    assert not plan_path(root, source).exists()
    assert list((root / ".muse" / "audit").glob("import-*.json"))


def test_plan_suggests_artist_release_destination_from_tags(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    source = make_source(root)

    plan = make_plan(root, source)

    assert plan.destination == "master/artists/Artist/Album"
    assert plan.profile == "release"


def test_plan_suggests_single_and_selection_destinations_from_tags(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    single = root / "backlog" / "single.flac"
    selection = root / "backlog" / "selection.flac"
    single.parent.mkdir(parents=True)
    make_track(single, 1, album="Single", total=1)
    make_track(selection, 4, album="Full Album", total=10)

    single_plan = make_plan(root, single)
    abort_plan(root)
    selection_plan = make_plan(root, selection)

    assert single_plan.destination == "master/artists/Artist/singles/Single/single.flac"
    assert selection_plan.destination == (
        "master/artists/Artist/selections/Full Album/selection.flac"
    )


def test_plan_and_apply_single_file_into_shared_directory(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    source = root / "backlog" / "mixes" / "song.flac"
    source.parent.mkdir(parents=True)
    make_track(source, 1, total=1)
    destination = root / "master" / "games" / "mixes"
    destination.mkdir(parents=True)

    plan = make_plan(root, source, "games/mixes")

    assert plan.source == "backlog/mixes/song.flac"
    assert plan.destination == "master/games/mixes/song.flac"
    assert plan.profile == "standalone-single"
    assert plan.files[0].path == "."

    completed = apply_plan(root, source)

    assert completed.state == "completed"
    assert not source.exists()
    assert (destination / "song.flac").is_file()
    assert source.parent.is_dir()


def test_import_into_singles_fills_missing_track_and_disc_as_one_of_one(
    tmp_path: Path,
) -> None:
    root = tmp_path / "vault"
    source = root / "backlog" / "47 song.flac"
    source.parent.mkdir(parents=True)
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
            "album=Single",
            "-metadata",
            "album_artist=Artist",
            str(source),
        ],
        check=True,
    )

    plan = make_plan(root, source, "artists/Artist/singles/Single")

    assert plan.profile == "standalone-single"
    assert plan.files[0].media.track_number == 1
    assert plan.files[0].media.track_total == 1
    assert plan.files[0].media.disc_number == 1
    assert plan.files[0].media.disc_total == 1
    assert f"{source.name}: track = 1/1" in plan.fixups
    assert f"{source.name}: disc = 1/1" in plan.fixups


def test_single_album_track_uses_selection_profile_and_preserves_position(
    tmp_path: Path,
) -> None:
    root = tmp_path / "vault"
    source = root / "backlog" / "selections" / "song.flac"
    source.parent.mkdir(parents=True)
    make_track(source, 4, total=10)
    destination = root / "master" / "artists" / "Artist" / "selections"
    destination.mkdir(parents=True)

    plan = make_plan(root, source, "artists/Artist/selections")

    assert plan.profile == "selection"
    assert plan.files[0].media.track_number == 4
    assert plan.files[0].media.track_total == 10

    completed = apply_plan(root, source)

    assert completed.state == "completed"
    assert not source.exists()
    assert (destination / "song.flac").is_file()


def test_single_file_can_be_renamed_at_an_exact_destination(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    source = root / "backlog" / "song.flac"
    source.parent.mkdir(parents=True)
    make_track(source, 1, total=1)
    (root / "master" / "artists").mkdir(parents=True)

    plan = make_plan(root, source, "artists/Artist - Song.flac")
    completed = apply_plan(root, source)

    assert plan.destination == "master/artists/Artist - Song.flac"
    assert completed.state == "completed"
    assert (root / plan.destination).is_file()


def test_directory_wrapped_single_uses_single_profile(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    source = root / "backlog" / "single"
    source.mkdir(parents=True)
    make_track(source / "song.flac", 1, total=1)
    (root / "master" / "artists").mkdir(parents=True)

    plan = make_plan(root, source, "artists/single")

    assert plan.profile == "standalone-single"


def test_apply_resumes_after_directory_was_renamed(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    source = make_source(root)
    plan = make_plan(root, source, "master/games/new-name")
    destination = root / plan.destination
    source.rename(destination)

    completed = apply_plan(root, source)

    assert completed.state == "completed"
    assert destination.is_dir()


def test_plan_can_accept_inconsistent_album_artists_durably(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    source = root / "backlog" / "soundtrack"
    source.mkdir(parents=True)
    make_track(source / "01.flac", 1, album_artist="Kenji Kawai")
    make_track(source / "02.flac", 2, album_artist="Kimiko Itoh")

    with pytest.raises(ValueError, match="inconsistent album artist tags"):
        make_plan(root, source, "movies-tv/Soundtrack")

    plan = make_plan(
        root,
        source,
        "movies-tv/Soundtrack",
        accept_inconsistent_album_artists=True,
    )

    assert plan.accepted_inconsistent_album_artists is True
    assert "Kenji Kawai, Kimiko Itoh" in plan.warnings[0]
    completed = apply_plan(root, source)
    assert completed.state == "completed"


def test_import_refuses_changed_or_non_audio_content(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    source = make_source(root)
    make_plan(root, source, "games/album")
    (source / "01.flac").write_bytes(b"changed")

    with pytest.raises(ValueError, match="changed"):
        apply_plan(root, source)
    abort_plan(root)

    other = root / "backlog" / "with-notes"
    other.mkdir()
    make_track(other / "song.flac", 1)
    (other / "notes.txt").write_text("notes")
    with pytest.raises(ValueError, match="non-audio"):
        make_plan(root, other, "games/with-notes")


def test_import_repairs_safe_missing_tags_and_preserves_and_embeds_cover(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    source = root / "backlog" / "album"
    source.mkdir(parents=True)
    track = source / "01. Song.mp3"
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
            str(track),
        ],
        check=True,
    )
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=blue:size=16x16",
            "-frames:v",
            "1",
            str(source / "cover.jpg"),
        ],
        check=True,
    )
    track.chmod(0o555)
    (root / "master" / "artists").mkdir(parents=True)

    plan = make_plan(root, source, "artists/Artist/Album")

    assert track.stat().st_mode & 0o777 == 0o555
    assert plan.files[0].media.album_artist == "Artist"
    assert plan.files[0].media.track_number == 1
    assert plan.files[0].media.disc_number == 1
    assert len(plan.artifacts) == 1
    assert any("embedded cover" in fixup for fixup in plan.fixups)

    apply_plan(root, source)
    destination = root / "master" / "artists" / "Artist" / "Album"
    assert (destination / "cover.jpg").is_file()
    tags = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format_tags",
            "-of",
            "json",
            str(destination / track.name),
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert "album_artist" in tags


def test_file_destination_is_created_as_a_directory(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    source = root / "backlog" / "song.flac"
    source.parent.mkdir(parents=True)
    make_track(source, 1, total=1)
    (root / "master").mkdir()

    plan = make_plan(root, source, "artists/Artist/selections/Album")

    assert plan.destination == "master/artists/Artist/selections/Album/song.flac"
    apply_plan(root, source)
    assert (root / plan.destination).is_file()


def test_import_rejects_unsafe_sources_and_destinations(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    source = make_source(root)

    for destination in ("../outside", "backlog/other"):
        with pytest.raises(ValueError, match="destination"):
            make_plan(root, source, destination)

    outside = tmp_path / "outside"
    outside.mkdir()
    make_track(outside / "song.flac", 1)
    with pytest.raises(ValueError, match="backlog"):
        make_plan(root, outside, "games/outside")


def test_mixed_audio_parameters_are_preserved_with_a_warning(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    source = make_source(root)
    make_track(source / "02.flac", 2, sample_rate=32_000)

    plan = make_plan(root, source, "games/album")

    assert len(plan.warnings) == 1
    assert "preserved as-is" in plan.warnings[0]
    assert "32000 Hz" in plan.warnings[0]
    assert "44100 Hz" in plan.warnings[0]
    assert plan.to_dict()["warnings"] == list(plan.warnings)


def test_missing_plan_has_actionable_error(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    source = make_source(root)

    with pytest.raises(ValueError, match="provide a destination to create one"):
        load_plan(root, source)


def test_only_one_source_can_have_a_current_import_plan(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    first = make_source(root, "first")
    second = root / "backlog" / "second"
    second.mkdir()
    make_track(second / "01.flac", 1)
    make_track(second / "02.flac", 2)
    make_plan(root, first, "games/first")

    with pytest.raises(ValueError, match="already exists for backlog/first"):
        make_plan(root, second, "games/second")

    assert load_plan(root).source == "backlog/first"


def test_replanning_keeps_one_plan_for_source(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    source = make_source(root)
    make_plan(root, source, "games/first")

    updated = make_plan(root, source, "games/second")

    assert updated.destination == "master/games/second"
    assert load_plan(root, source) == updated
    assert len(list((root / ".muse" / "imports").glob("*.json"))) == 1


@pytest.mark.parametrize("codec", ["aac", "alac"])
def test_import_accepts_m4a_aac_and_apple_lossless(tmp_path: Path, codec: str) -> None:
    root = tmp_path / "vault"
    source = root / "backlog" / "album"
    source.mkdir(parents=True)
    make_track(source / "01.m4a", 1, codec=codec, total=1)
    (root / "master" / "games").mkdir(parents=True)

    plan = make_plan(root, source, "games/album")

    assert plan.files[0].media.container == "m4a"
    assert plan.files[0].media.codec == codec
