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


@pytest.mark.asyncio
async def test_overlapping_controller_edits_release_every_superseded_view(cog):
    entered, release = asyncio.Event(), asyncio.Event()
    older, newer = SimpleNamespace(stop=Mock()), SimpleNamespace(stop=Mock())

    async def slow_edit(view):
        entered.set()
        await release.wait()

    first = asyncio.create_task(cog._activate_controller_view(1, older, slow_edit))
    await asyncio.wait_for(entered.wait(), 1)
    second = asyncio.create_task(cog._activate_controller_view(1, newer, AsyncMock()))
    try:
        await asyncio.sleep(0)
    finally:
        release.set()
        await asyncio.gather(first, second)

    assert cog._controller_views[1] is newer
    older.stop.assert_called_once()
    newer.stop.assert_not_called()


@pytest.mark.asyncio
async def test_overlapping_resends_leave_only_the_latest_panel(cog):
    current = entry(1)
    session = SimpleNamespace(snapshot=lambda: PlaybackSnapshot(current, (), False, 22))
    cog.backend = SimpleNamespace(get=AsyncMock(return_value=session))
    cog._current_entries[1] = current
    views = [SimpleNamespace(stop=Mock()), SimpleNamespace(stop=Mock())]
    messages = [SimpleNamespace(id=11, delete=AsyncMock()), SimpleNamespace(id=12, delete=AsyncMock())]
    cog._controller_view = AsyncMock(side_effect=views)
    entered, release = asyncio.Event(), asyncio.Event()
    send_count = 0

    async def send(**kwargs):
        nonlocal send_count
        index = send_count
        send_count += 1
        if index == 0:
            entered.set()
            await release.wait()
        return messages[index]

    cog._playback_channels[1] = SimpleNamespace(send=send)
    first = asyncio.create_task(cog._resend_controller_for_track_start(guild_id=1))
    await asyncio.wait_for(entered.wait(), 1)
    second = asyncio.create_task(cog._resend_controller_for_track_start(guild_id=1))
    try:
        await asyncio.sleep(0)
    finally:
        release.set()
        await asyncio.gather(first, second)

    assert cog._controller_messages[1] is messages[1]
    assert cog._controller_views[1] is views[1]
    messages[0].delete.assert_awaited_once()
    messages[1].delete.assert_not_awaited()
    views[0].stop.assert_called_once()
    views[1].stop.assert_not_called()


@pytest.mark.asyncio
async def test_old_panel_interaction_cannot_replace_resend_for_the_same_track(cog):
    current = entry(1)
    session = SimpleNamespace(snapshot=lambda: PlaybackSnapshot(current, (), False, 22))
    cog.backend = SimpleNamespace(get=AsyncMock(return_value=session))
    old_message, latest_message = SimpleNamespace(id=11), SimpleNamespace(id=12)
    latest_view = SimpleNamespace(stop=Mock())
    cog._controller_messages[1] = latest_message
    cog._controller_views[1] = latest_view
    cog._controller_view = AsyncMock(return_value=SimpleNamespace(stop=Mock()))
    interaction = SimpleNamespace(
        message=old_message,
        response=SimpleNamespace(is_done=lambda: True),
        edit_original_response=AsyncMock(),
    )

    await cog._refresh_controller(1, interaction)

    assert cog._controller_messages[1] is latest_message
    assert cog._controller_views[1] is latest_view
    latest_view.stop.assert_not_called()
    interaction.edit_original_response.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("blocked_check", [1, 2])
async def test_cancelled_controller_ownership_check_releases_view(cog, blocked_check):
    entered = asyncio.Event()
    view = SimpleNamespace(stop=Mock())
    checks = 0

    async def still_current():
        nonlocal checks
        checks += 1
        if checks == blocked_check:
            entered.set()
            await asyncio.Future()
        return True

    pending = asyncio.create_task(cog._activate_controller_view(
        1, view, AsyncMock(), still_current=still_current,
    ))
    await asyncio.wait_for(entered.wait(), 1)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending

    view.stop.assert_called_once()
    assert 1 not in cog._controller_views


@pytest.mark.asyncio
async def test_cancelling_waiting_controller_update_releases_its_view_and_lock(cog):
    entered, release = asyncio.Event(), asyncio.Event()
    active, waiting = SimpleNamespace(stop=Mock()), SimpleNamespace(stop=Mock())

    async def slow_edit(view):
        entered.set()
        await release.wait()

    first = asyncio.create_task(cog._activate_controller_view(1, active, slow_edit))
    await asyncio.wait_for(entered.wait(), 1)
    edit_waiting = AsyncMock()
    second = asyncio.create_task(cog._activate_controller_view(1, waiting, edit_waiting))
    try:
        await asyncio.sleep(0)
        second.cancel()
        with pytest.raises(asyncio.CancelledError):
            await second
    finally:
        release.set()
        await first

    assert cog._controller_views[1] is active
    waiting.stop.assert_called_once()
    edit_waiting.assert_not_awaited()
    assert not cog._controller_locks


@pytest.mark.asyncio
async def test_controller_lock_does_not_block_other_guilds_or_survive_updates(cog):
    entered, release = asyncio.Event(), asyncio.Event()
    first_view, other_view = SimpleNamespace(stop=Mock()), SimpleNamespace(stop=Mock())

    async def slow_edit(view):
        entered.set()
        await release.wait()

    first = asyncio.create_task(cog._activate_controller_view(1, first_view, slow_edit))
    await asyncio.wait_for(entered.wait(), 1)
    try:
        await asyncio.wait_for(cog._activate_controller_view(2, other_view, AsyncMock()), 1)
        assert cog._controller_views[2] is other_view
    finally:
        release.set()
        await first

    assert not cog._controller_locks
