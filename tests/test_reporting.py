from muse.reporting import human_duration, human_number, middle_truncate


def test_human_number_uses_thousands_separators() -> None:
    assert human_number(999) == "999"
    assert human_number(1_000) == "1,000"
    assert human_number(1_234_567) == "1,234,567"


def test_middle_truncate_preserves_both_ends() -> None:
    assert middle_truncate("backlog/a/long artist/song.flac", 19) == "backlog/a…song.flac"
    assert middle_truncate("song.flac", 20) == "song.flac"
    assert middle_truncate("long", 1) == "…"


def test_human_duration_preserves_useful_precision() -> None:
    assert human_duration(0.123) == "0.12s"
    assert human_duration(12.34) == "12.3s"
    assert human_duration(125) == "2m 05s"
    assert human_duration(3_725) == "1h 02m 05s"
