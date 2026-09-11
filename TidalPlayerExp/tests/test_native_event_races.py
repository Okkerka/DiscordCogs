"""Cog controls must not publish stale UI or enqueue after an explicit stop."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from TidalPlayerExp.playback.models import PlaybackSnapshot
from TidalPlayerExp.providers.youtube_resolver import YouTubeVideoMetadata
from TidalPlayerExp.tests.test_native_session import entry


@pytest.mark.asyncio
async def test_cancelled_track_start_send_releases_unpublished_controller(cog):
    current = entry(1)
    session = SimpleNamespace(snapshot=lambda: PlaybackSnapshot(current, (), False, 22))
    cog.backend = SimpleNamespace(get=AsyncMock(return_value=session))
    cog._current_entries[1] = current
    view = SimpleNamespace(stop=Mock())
    cog._controller_view = AsyncMock(return_value=view)
    entered = asyncio.Event()

    async def send(**kwargs):
        entered.set()
        await asyncio.Future()

    cog._playback_channels[1] = SimpleNamespace(send=send)
    pending = asyncio.create_task(cog._resend_controller_for_track_start(guild_id=1))
    await asyncio.wait_for(entered.wait(), 2)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    view.stop.assert_called_once()
    assert 1 not in cog._controller_views
    assert 1 not in cog._controller_messages


@pytest.mark.asyncio
@pytest.mark.parametrize("blocked_stage", ["view", "edit"])
async def test_recommendation_refresh_cannot_restore_ended_queue_panel(cog, blocked_stage):
    ready, release = asyncio.Event(), asyncio.Event()
    previous = entry(1)
    current = previous
    session = SimpleNamespace(snapshot=lambda: PlaybackSnapshot(current, (), False, 22))
    cog.backend = SimpleNamespace(get=AsyncMock(return_value=session))
    cog._current_entries[1] = previous
    cog._current_meta[1] = previous.meta
    cog._controller_meta[1] = previous.meta
    view = SimpleNamespace(stop=Mock())

    async def make_view(*args, **kwargs):
        if blocked_stage == "view":
            ready.set()
            await release.wait()
        return view

    async def edit(**kwargs):
        if blocked_stage == "edit":
            ready.set()
            await release.wait()

    message = SimpleNamespace(edit=AsyncMock(side_effect=edit), delete=AsyncMock())
    cog._controller_messages[1] = message
    cog._get_recommendations = AsyncMock(return_value=[object()])
    cog._controller_view = make_view
    cog._schedule_autoplay = Mock()
    refresh = asyncio.create_task(cog._refresh_controller_recommendations(1, "id:1"))
    await asyncio.wait_for(ready.wait(), timeout=1)
    current = None
    await cog.queue_ended(1, previous)
    release.set()
    await refresh
    assert 1 not in cog._controller_views
    assert 1 not in cog._controller_messages
    view.stop.assert_called_once()
    if blocked_stage == "view":
        message.edit.assert_not_awaited()


@pytest.mark.asyncio
async def test_stop_cancels_pending_recommendation_admission(cog):
    selected = SimpleNamespace(id=123, name="Song", artist=SimpleNamespace(name="Artist"))
    ready, release = asyncio.Event(), asyncio.Event()
    session = SimpleNamespace(
        snapshot=lambda: PlaybackSnapshot(None, (), False, 22),
        enqueue=AsyncMock(return_value=True), stop=AsyncMock(),
    )
    cog.backend = SimpleNamespace(get=AsyncMock(return_value=session))

    async def metadata(*args, **kwargs):
        ready.set()
        await release.wait()
        return dict(entry(123).meta)

    cog._extract_meta = metadata
    interaction = SimpleNamespace(
        guild=SimpleNamespace(id=1), user=SimpleNamespace(id=5),
        response=SimpleNamespace(is_done=lambda: True, defer=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()),
    )
    pending = asyncio.create_task(cog.queue_recommendation(interaction, selected))
    await ready.wait()
    await cog.controller_stop(interaction)
    release.set()
    assert not await pending
    session.enqueue.assert_not_awaited()


@pytest.mark.asyncio
async def test_slow_controller_refresh_cannot_replace_new_track_panel(cog):
    ready, release = asyncio.Event(), asyncio.Event()
    old_entry, new_entry = entry(1), entry(2)
    current = old_entry
    session = SimpleNamespace(snapshot=lambda: PlaybackSnapshot(current, (), False, 22))
    cog.backend = SimpleNamespace(get=AsyncMock(return_value=session))
    cog._current_entries[1] = old_entry
    cog._current_meta[1] = old_entry.meta
    cog._controller_meta[1] = old_entry.meta
    old_view, new_view = SimpleNamespace(stop=Mock()), SimpleNamespace(stop=Mock())
    old_message = SimpleNamespace(edit=AsyncMock())
    new_message = SimpleNamespace(edit=AsyncMock())
    cog._controller_messages[1] = old_message

    async def make_view(*args, **kwargs):
        ready.set()
        await release.wait()
        return old_view

    cog._controller_view = make_view
    refresh = asyncio.create_task(cog._refresh_controller(1))
    await ready.wait()
    current = new_entry
    cog._current_entries[1] = new_entry
    cog._current_meta[1] = new_entry.meta
    cog._controller_meta[1] = new_entry.meta
    cog._controller_messages[1] = new_message
    cog._controller_views[1] = new_view
    release.set()
    await refresh
    assert cog._controller_views[1] is new_view
    assert cog._controller_messages[1] is new_message
    new_message.edit.assert_not_awaited()
    assert old_view.stop.called


@pytest.mark.asyncio
async def test_stop_prevents_late_youtube_metadata_admission(cog):
    ready, release = asyncio.Event(), asyncio.Event()
    session = SimpleNamespace(
        snapshot=lambda: PlaybackSnapshot(None, (), False, 22),
        enqueue=AsyncMock(return_value=True), stop=AsyncMock(),
    )
    cog.backend = SimpleNamespace(get=AsyncMock(return_value=session))
    cog._prepare_playback_session = AsyncMock(return_value=session)
    cog.tidal = SimpleNamespace(is_logged_in=AsyncMock(return_value=False))
    ctx = SimpleNamespace(guild=SimpleNamespace(id=1), author=SimpleNamespace(id=5), channel=object(), send=AsyncMock())

    async def metadata(*args):
        ready.set()
        await release.wait()
        return YouTubeVideoMetadata("abcdefghijk", "Song", "Artist", 20, None)

    cog._youtube_video_metadata = metadata
    pending = asyncio.create_task(cog._handle_youtube_video(ctx, "abcdefghijk"))
    await ready.wait()
    await cog.controller_stop(SimpleNamespace(
        guild=ctx.guild, response=SimpleNamespace(defer=AsyncMock()), followup=SimpleNamespace(send=AsyncMock()),
    ))
    release.set()
    await pending
    session.enqueue.assert_not_awaited()
