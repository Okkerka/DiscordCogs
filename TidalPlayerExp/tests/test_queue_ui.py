"""Components V2 queue panel behavior and Discord safety limits."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord as real_discord
import pytest

from TidalPlayerExp.playback.models import PlaybackSnapshot


@pytest.fixture
def real_queue_ui(monkeypatch):
    """Load the queue view against py-cord's real Components V2 serializer."""
    monkeypatch.setitem(sys.modules, "discord", real_discord)
    monkeypatch.setitem(sys.modules, "discord.ui", real_discord.ui)
    path = Path(__file__).parents[1] / "ui" / "queue.py"
    spec = importlib.util.spec_from_file_location("TidalPlayerExp.ui._test_queue", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _entry(number: int, *, title: str | None = None, artist: str = "Artist", requester_id: int | None = 42):
    from TidalPlayerExp.tests.conftest import make_entry

    entry = make_entry(number, title=title, artist=artist)
    if requester_id == 5:
        return entry
    return entry.__class__(entry.entry_id, entry.primary, entry.fallback, entry.meta, requester_id)


def _text(view) -> str:
    return "\n".join(
        item.content for item in view.walk_children() if isinstance(item, real_discord.ui.TextDisplay)
    )


def _buttons(view):
    return [item for item in view.walk_children() if isinstance(item, real_discord.ui.Button)]


def _view(real_queue_ui, snapshot, *, page: int = 0, session=None):
    session = session or SimpleNamespace(snapshot=lambda: snapshot)
    cog = SimpleNamespace(backend=SimpleNamespace(get=AsyncMock(return_value=session)))
    return real_queue_ui.QueueView(cog, 99, snapshot, page=page), cog


def test_queue_panel_separates_now_playing_from_numbered_waiting_tracks(real_queue_ui):
    current = _entry(99, title="Now @everyone", artist="<@123456789012345678>", requester_id=7)
    waiting = tuple(_entry(index, title=f"Waiting {index}", requester_id=1_000 + index) for index in range(1, 12))
    snapshot = PlaybackSnapshot(current, waiting, False, 123)

    view, _ = _view(real_queue_ui, snapshot)
    text = _text(view)

    assert "## Now playing" in text
    assert "Now @\u200beveryone" in text
    assert "<@123456789012345678>" not in text
    assert "**1.** Waiting 1" in text
    assert "**10.** Waiting 10" in text
    assert "**11.**" not in text
    assert "Requester: 1001" in text
    assert "<@1001>" not in text
    assert "11 waiting · Page 1/2" in text


def test_queue_panel_bounds_untrusted_track_text_and_serializes_as_components_v2(real_queue_ui):
    huge = "😀* @everyone " * 5_000
    snapshot = PlaybackSnapshot(_entry(1, title=huge, artist=huge), tuple(
        _entry(index + 2, title=huge, artist=huge) for index in range(10)
    ), False, 123)

    view, _ = _view(real_queue_ui, snapshot)
    text = _text(view)

    assert len(text.encode("utf-16-le")) // 2 <= 4_000
    assert "@everyone" not in text
    assert "**1.**" in text
    view.to_components()


def test_empty_queue_is_friendly_and_includes_supported_playback_state(real_queue_ui):
    snapshot = SimpleNamespace(
        current=None, queued=(), paused=False, channel_id=None, volume=35, repeat=True, halted=False,
    )

    view, _ = _view(real_queue_ui, snapshot)
    text = _text(view)

    assert "No songs are waiting. Add one to keep the music going." in text
    assert "Volume: 35" in text
    assert "Repeat: On" in text
    assert "Halted: Off" in text


def test_queue_bounds_unexpectedly_long_requester_ids_without_dropping_the_page(real_queue_ui):
    requester_id = int("9" * 500)
    snapshot = PlaybackSnapshot(None, tuple(
        _entry(index, requester_id=requester_id) for index in range(1, 11)
    ), False, 123)

    view, _ = _view(real_queue_ui, snapshot)
    text = _text(view)

    assert "**10.** Track 10" in text
    assert f"Requester: {'9' * 31}…" in text
    assert len(text.encode("utf-16-le")) // 2 <= 4_000


@pytest.mark.asyncio
async def test_refresh_reads_authoritative_snapshot_and_clamps_a_shrunken_page(real_queue_ui):
    old_snapshot = PlaybackSnapshot(None, tuple(_entry(index) for index in range(1, 12)), False, 123)
    new_snapshot = PlaybackSnapshot(None, (_entry(1, title="Only remaining"),), True, 123)
    session = SimpleNamespace(snapshot=lambda: new_snapshot)
    view, cog = _view(real_queue_ui, old_snapshot, page=1, session=session)
    interaction = SimpleNamespace(response=SimpleNamespace(edit_message=AsyncMock()))

    await view._refresh(interaction)

    cog.backend.get.assert_awaited_once_with(99)
    assert view.page == 0
    assert "Only remaining" in _text(view)
    interaction.response.edit_message.assert_awaited_once()
    assert interaction.response.edit_message.await_args.kwargs["view"] is view
    mentions = interaction.response.edit_message.await_args.kwargs["allowed_mentions"]
    assert not mentions.everyone and not mentions.users and not mentions.roles


@pytest.mark.asyncio
async def test_timeout_disables_buttons_and_stops_the_nonpersistent_view(real_queue_ui):
    snapshot = PlaybackSnapshot(None, (_entry(1),), False, 123)
    view, _ = _view(real_queue_ui, snapshot)
    view.message = SimpleNamespace(edit=AsyncMock())

    await view.on_timeout()

    assert all(button.disabled for button in _buttons(view))
    assert view.is_finished()
    view.message.edit.assert_awaited_once()
    assert view.message.edit.await_args.kwargs["view"] is view
    mentions = view.message.edit.await_args.kwargs["allowed_mentions"]
    assert not mentions.everyone and not mentions.users and not mentions.roles
