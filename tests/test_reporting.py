from muse.reporting import human_number


def test_human_number_uses_thousands_separators() -> None:
    assert human_number(999) == "999"
    assert human_number(1_000) == "1,000"
    assert human_number(1_234_567) == "1,234,567"
