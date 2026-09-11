import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest


def helpers():
    path = Path(__file__).parents[1] / "helpers.py"
    assert path.exists(), "Moderation helpers have not been implemented"
    spec = importlib.util.spec_from_file_location("moderation_helpers", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("name, expected", [("!!! Alice", "Alice"), ("  Éva", "Éva"), ("张三", None), ("!!!", None), ("Alice!", None)])
def test_dehoist_preserves_names(name, expected):
    assert helpers().dehoisted_name(name) == expected


def test_purge_protects_pins_and_humans():
    match = helpers().matches_purge
    msg = SimpleNamespace(pinned=False, author=SimpleNamespace(bot=False, id=1), content="hi", embeds=[], attachments=[])
    assert not match(msg, "bots")
    msg.author.bot = True
    assert match(msg, "bots")
    assert not match(msg, "bots", member_id=2)
    msg.pinned = True
    assert not match(msg, "bots")


def test_separate_attachments_and_embeds():
    msg = SimpleNamespace(pinned=False, author=SimpleNamespace(bot=False, id=1), content="", embeds=[1], attachments=[])
    assert helpers().matches_purge(msg, "embeds")
    assert not helpers().matches_purge(msg, "attachments")

