"""Controller and sink concurrency against the authoritative native session."""
import asyncio
import sys
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
async def test_stop_click_announces_the_actor(cog, native_session):
    guild = SimpleNamespace(id=53)
    current = make_entry(1)
    native_session.current = current
    cog._current_entries[guild.id] = current
    interaction = make_controller_interaction(guild)
    interaction.user = SimpleNamespace(mention="<@7>")

    await cog.controller_stop(interaction)

    native_session.stop.assert_awaited_once_with(clear_queue=True)
    interaction.followup.send.assert_awaited_once()
    view = interaction.followup.send.await_args.kwargs["view"]
    assert view.text == (
        "### Playback stopped\nQueue has been cleared.\n-# Stopped by <@7>"
    )
    assert interaction.followup.send.await_args.kwargs["allowed_mentions"].users is False


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
    # The skip is announced to the channel without triggering an error path.
    view = interaction.followup.send.await_args.kwargs["view"]
    assert view.text == "### Skipped\n**Track 1**\nArtist\n-# Duration: 02:00 · Skipped by Someone"
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
    await asyncio.wait_for(started.wait(), 2)
    native_session.current = second
    second_event = asyncio.create_task(cog.track_started(1, second))
    try:
        # The newer event changes authoritative state while publication waits
        # for the older send; do not hold its simulated network reply hostage.
        await asyncio.sleep(0)
    finally:
        release.set()
    await asyncio.wait_for(asyncio.gather(first_event, second_event), 2)

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


@pytest.mark.asyncio
async def test_skip_click_cannot_apply_to_the_next_track(cog, native_session):
    """A click dispatched before a boundary must not skip the replacement track."""
    guild = SimpleNamespace(id=54)
    native_session.current = make_entry()
    interaction = make_controller_interaction(guild)

    async def track_changed() -> None:
        cog._guild_generations[guild.id] += 1

    interaction.response.defer.side_effect = track_changed
    await cog.controller_skip(interaction)

    native_session.skip.assert_not_awaited()
    interaction.followup.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_deleted_controller_message_is_unmapped(cog, native_session):
    """A deleted panel must not keep receiving edits on every refresh."""
    guild_id = 55
    entry = make_entry()
    native_session.current = entry
    cog._current_entries[guild_id] = entry
    cog._current_meta[guild_id] = entry.meta
    cog._controller_meta[guild_id] = entry.meta
    response = SimpleNamespace(status=404, reason="Not Found")
    # The cog may have been imported against the suite's discord stub; raise the
    # exception class that its except clause actually names.
    cog_discord = sys.modules[type(cog).__module__].discord
    message = SimpleNamespace(
        id=400,
        edit=AsyncMock(side_effect=cog_discord.NotFound(response, "Unknown Message")),
    )
    cog._controller_messages[guild_id] = message
    cog._controller_view = AsyncMock(return_value=MagicMock())

    await cog._refresh_controller(guild_id)
    assert guild_id not in cog._controller_messages
    message.edit.assert_awaited_once()

    # A later refresh must not retry the deleted message.
    cog._controller_last_refresh.pop(guild_id, None)
    await cog._refresh_controller(guild_id)
    message.edit.assert_awaited_once()


@pytest.mark.asyncio
async def test_stop_force_releases_a_wedged_batch_claim(cog, monkeypatch):
    """A stuck provider call must not reject new imports until the cog unloads."""
    from TidalPlayerExp import commands as command_module

    monkeypatch.setattr(command_module, "BATCH_RELEASE_GRACE", 0)
    guild_id = 56
    event = cog._claim_batch(guild_id)
    assert event is not None
    assert cog._claim_batch(guild_id) is None

    cog._cancel_imports(guild_id)
    await asyncio.gather(*tuple(cog._tasks))

    assert event.is_set()
    assert guild_id not in cog._cancel_events
    assert cog._claim_batch(guild_id) is not None
