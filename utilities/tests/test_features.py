import importlib.util
from pathlib import Path

import pytest


def helpers():
    path = Path(__file__).parents[1] / "helpers.py"
    assert path.exists(), "Utility helpers have not been implemented"
    spec = importlib.util.spec_from_file_location("utility_helpers", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_timestamp_explicit_offset():
    assert helpers().parse_timestamp("2026-01-01T01:00+01:00", "UTC") == 1767225600


@pytest.mark.parametrize("value", ["2026-10-25 02:30", "2026-03-29 02:30"])
def test_dst_ambiguity_or_gap_rejected(value):
    with pytest.raises(ValueError):
        helpers().parse_timestamp(value, "Europe/Budapest")


def test_quote_link_strict():
    assert helpers().parse_message_link("https://discord.com/channels/123/456/789") == (123, 456, 789)
    with pytest.raises(ValueError):
        helpers().parse_message_link("https://evil.test/channels/123/456/789")
