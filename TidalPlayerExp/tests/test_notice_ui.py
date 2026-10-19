"""Skip/stop confirmations render as bounded Components V2 cards."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import discord as real_discord
import pytest


@pytest.fixture
def real_notice_ui(monkeypatch):
    """Load the notice builders against discord.py's real Components V2 serializer."""
    monkeypatch.setitem(sys.modules, "discord", real_discord)
    monkeypatch.setitem(sys.modules, "discord.ui", real_discord.ui)
    path = Path(__file__).parents[1] / "ui" / "notice.py"
    spec = importlib.util.spec_from_file_location("TidalPlayerExp.ui._test_notice", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _meta(**overrides):
    meta = {
        "title": "Night Drive",
        "artist": "Kavinsky",
        "album": "OutRun",
        "duration": 186,
        "source": "TIDAL",
        "image": None,
        "share_url": None,
    }
    meta.update(overrides)
    return meta


def _text(view) -> str:
    return "\n".join(
        item.content for item in view.walk_children() if isinstance(item, real_discord.ui.TextDisplay)
    )


def _container(view):
    return next(
        item for item in view.walk_children() if isinstance(item, real_discord.ui.Container)
    )


def test_skip_notice_mirrors_the_queue_card_metadata(real_notice_ui):
    view = real_notice_ui.skip_notice(
        "<@42>", _meta(share_url="https://tidal.com/track/1")
    )
    assert _text(view) == (
        "### Skipped\n"
        "**Night Drive**\n"
        "Kavinsky\n"
        "*OutRun*\n"
        "[Open in TIDAL](https://tidal.com/track/1)\n"
        "-# Duration: 03:06 · Skipped by <@42>"
    )
    assert _container(view).accent_colour == real_discord.Colour.blurple()


def test_skip_notice_omits_missing_metadata_without_blank_lines(real_notice_ui):
    text = _text(real_notice_ui.skip_notice("<@42>", _meta(album=None, share_url=None)))
    assert text == (
        "### Skipped\n**Night Drive**\nKavinsky\n-# Duration: 03:06 · Skipped by <@42>"
    )


def test_skip_notice_embeds_album_art_like_the_queue_card(real_notice_ui):
    view = real_notice_ui.skip_notice("<@42>", _meta(image="https://cdn.example/art.jpg"))
    section = next(
        item for item in view.walk_children() if isinstance(item, real_discord.ui.Section)
    )
    assert isinstance(section.accessory, real_discord.ui.Thumbnail)
    assert section.accessory.media.url == "https://cdn.example/art.jpg"


def test_skip_notice_without_art_keeps_the_card_compact(real_notice_ui):
    view = real_notice_ui.skip_notice("<@42>", _meta())
    assert not any(
        isinstance(item, real_discord.ui.Section) for item in view.walk_children()
    )


def test_skip_notice_escapes_provider_metadata(real_notice_ui):
    hostile = "**Hostile** @everyone [x](https://evil.example)"
    text = _text(
        real_notice_ui.skip_notice(
            "<@42>", _meta(title=hostile, artist=hostile, album=hostile)
        )
    )
    assert "<@42>" in text  # the actor mention stays renderable (pings stay disabled)
    assert "**Hostile**" not in text  # escaped, so it cannot reformat the card
    assert "@everyone" not in text
    assert "[x](https://evil.example)" not in text


def test_skip_notice_without_a_track_falls_back_to_the_current_song(real_notice_ui):
    assert _text(real_notice_ui.skip_notice("Someone", None)) == (
        "### Skipped\nSomeone skipped the current song."
    )


def test_stop_notice_says_it_once(real_notice_ui):
    view = real_notice_ui.stop_notice("<@7>", "Queue has been cleared.")
    text = _text(view)
    assert text == "### Playback stopped\nQueue has been cleared.\n-# Stopped by <@7>"
    assert "stopped the playback" not in text
    assert _container(view).accent_colour == real_discord.Colour.red()


def test_stop_notice_carries_the_command_and_controller_details(real_notice_ui):
    command = real_notice_ui.stop_notice(
        "<@7>", "Queue has been cleared and imports cancelled."
    )
    assert _text(command) == (
        "### Playback stopped\n"
        "Queue has been cleared and imports cancelled.\n"
        "-# Stopped by <@7>"
    )


def test_notice_headings_do_not_start_with_emojis(real_notice_ui):
    assert _text(real_notice_ui.skip_notice("<@1>", _meta())).splitlines()[0] == (
        "### Skipped"
    )
    assert _text(real_notice_ui.stop_notice("<@1>")).splitlines()[0] == (
        "### Playback stopped"
    )


def test_notice_text_stays_within_the_components_v2_budget(real_notice_ui):
    view = real_notice_ui.skip_notice(
        "<@9>", _meta(title="😀" * 5_000, artist="😀" * 5_000, album="😀" * 5_000)
    )
    assert len(view.text) <= 4_000
    assert "…" in view.text  # long fields were clamped, not dropped
