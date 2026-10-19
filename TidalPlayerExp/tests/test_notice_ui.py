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


def _text(view) -> str:
    return "\n".join(
        item.content for item in view.walk_children() if isinstance(item, real_discord.ui.TextDisplay)
    )


def _accent(view):
    return next(
        item for item in view.walk_children() if isinstance(item, real_discord.ui.Container)
    ).accent_colour


def test_skip_notice_names_the_actor_and_escapes_provider_titles(real_notice_ui):
    view = real_notice_ui.skip_notice("<@42>", "**Hostile** @everyone [x](https://evil.example)")
    text = _text(view)
    assert text.startswith("### ⏭ Skipped\n")
    assert "<@42>" in text  # the actor mention stays renderable (pings stay disabled)
    assert "**Hostile**" not in text  # escaped, so it cannot reformat the card
    assert "@everyone" not in text
    assert "[x](https://evil.example)" not in text
    assert _accent(view) == real_discord.Colour.blurple()


def test_skip_notice_without_a_title_falls_back_to_the_current_song(real_notice_ui):
    view = real_notice_ui.skip_notice("Someone", None)
    assert _text(view) == "### ⏭ Skipped\nSomeone skipped the current song."


def test_stop_notice_carries_the_command_and_controller_details(real_notice_ui):
    command = real_notice_ui.stop_notice("<@7>", "Queue cleared and imports cancelled.")
    controller = real_notice_ui.stop_notice("<@7>", "Queue cleared.")
    assert _text(controller) == "### ⏹ Playback stopped\n<@7> stopped the playback. Queue cleared."
    assert _text(command).endswith("Queue cleared and imports cancelled.")
    assert _accent(command) == real_discord.Colour.red()


def test_notice_text_stays_within_the_components_v2_budget(real_notice_ui):
    view = real_notice_ui.skip_notice("<@9>", "😀" * 5_000)
    assert len(view.text) <= 4_000
    assert "…" in view.text  # the title was clamped, not dropped
