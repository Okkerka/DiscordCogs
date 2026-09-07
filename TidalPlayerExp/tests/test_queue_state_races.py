"""Controller and sink concurrency against the authoritative native session."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from TidalPlayerExp.tests.conftest import make_entry


def make_controller_interaction(guild):
    channel = SimpleNamespace(send=AsyncMock())
    message = SimpleNamespace(channel=channel, delete=AsyncMock())
    return SimpleNamespace(
        guild=guild,
        channel=channel,
        message=message,
        response=SimpleNamespace(
            defer=AsyncMock(),
            is_done=MagicMock(return_value=False),
            send_message=AsyncMock(),
        ),
        followup=SimpleNamespace(send=AsyncMock()),
    )


@pytest.mark.asyncio
async def test_skip_and_track_start_advance_exactly_once(cog, native_session):
    guild = SimpleNamespace(id=51)
    old, first, second = [make_entry(index) for index in range(1, 4)]
    native_session.current = old
    native_session.entries = [first, second]
    cog._current_entries[guild.id] = old
    cog._current_meta[guild.id] = old.meta
    cog._resend_controller_for_track_start = AsyncMock()
    cog._schedule_controller_recommendations = MagicMock()

    async def skip():
        native_session.current = native_session.entries.pop(0)
        await cog.track_started(guild.id, native_session.current)
        return True

    native_session.skip.side_effect = skip
    await cog.controller_skip(make_controller_interaction(guild))
    await cog.track_started(guild.id, first)

    native_session.skip.assert_awaited_once_with()
    assert cog._current_meta[guild.id] == first.meta
    assert native_session.snapshot().queued == (second,)
    cog._resend_controller_for_track_start.assert_awaited_once()
    cog._schedule_controller_recommendations.assert_called_once_with(guild.id)


@pytest.mark.asyncio
async def test_skip_with_empty_queue_waits_for_end_event(cog, native_session):
    guild = SimpleNamespace(id=52)
    old = make_entry()
    native_session.current = old
    cog._current_entries[guild.id] = old
    cog._current_meta[guild.id] = old.meta
    cog._controller_view = AsyncMock()
    interaction = make_controller_interaction(guild)
    await cog.controller_skip(interaction)

    assert cog._current_meta[guild.id] == old.meta
    cog._controller_view.assert_not_awaited()
    interaction.followup.send.assert_not_awaited()
    native_session.current = None
    await cog.queue_ended(guild.id, old)
    assert guild.id not in cog._current_meta
    if task := cog._autoplay_tasks.get(guild.id):
        await task


@pytest.mark.asyncio
async def test_queue_end_removes_controller_before_autoplay_lookup(cog, native_session):
    guild_id = 53
    previous = make_entry()
    message = SimpleNamespace(delete=AsyncMock())
    view = MagicMock()
    cog._current_entries[guild_id] = previous
    cog._current_meta[guild_id] = previous.meta
    cog._controller_meta[guild_id] = previous.meta
    cog._controller_messages[guild_id] = message
    cog._controller_views[guild_id] = view

    def schedule(*args):
        message.delete.assert_awaited_once()
        assert guild_id not in cog._current_meta

    cog._schedule_autoplay = MagicMock(side_effect=schedule)
    await cog.queue_ended(guild_id, previous)

    assert guild_id not in cog._controller_messages
    view.stop.assert_called_once()
    cog._schedule_autoplay.assert_called_once_with(guild_id, previous, 0)


@pytest.mark.asyncio
async def test_delayed_track_panel_cannot_replace_newer_track(cog, native_session):
    first, second = make_entry(1), make_entry(2)
    native_session.current = first
    started, release = asyncio.Event(), asyncio.Event()
    old_message = SimpleNamespace(delete=AsyncMock())
    new_message = SimpleNamespace(delete=AsyncMock())
    old_view, new_view = MagicMock(), MagicMock()
    calls = 0

    async def send(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            started.set()
            await release.wait()
            return old_message
        return new_message

    cog._controller_view = AsyncMock(side_effect=[old_view, new_view])
    cog._playback_channels[1] = SimpleNamespace(send=send)
    cog._schedule_controller_recommendations = MagicMock()
    first_event = asyncio.create_task(cog.track_started(1, first))
    await started.wait()
    native_session.current = second
    await cog.track_started(1, second)
    release.set()
    await first_event

    assert cog._controller_messages[1] is new_message
    assert cog._controller_views[1] is new_view
    assert cog._current_meta[1] == second.meta
    old_view.stop.assert_called_once()
    old_message.delete.assert_awaited_once()
    cog._schedule_controller_recommendations.assert_called_once_with(1)


@pytest.mark.asyncio
async def test_stale_end_callback_cannot_clear_active_track(cog, native_session):
    current = make_entry()
    native_session.current = current
    cog._current_entries[1] = current
    cog._current_meta[1] = current.meta
    cog._schedule_autoplay = MagicMock()
    await cog.queue_ended(1, make_entry(2))
    assert cog._current_meta[1] == current.meta
    cog._schedule_autoplay.assert_not_called()
