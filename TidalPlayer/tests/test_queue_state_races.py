import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


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
        followup=SimpleNamespace(
            send=AsyncMock(return_value=SimpleNamespace(channel=channel))
        ),
    )


@pytest.mark.asyncio
async def test_skip_and_track_start_consume_exactly_one_metadata_entry(cog) -> None:
    guild = SimpleNamespace(id=51)
    old = {"track_id": 1, "title": "Old", "artist": "Artist", "album": None}
    first = {
        "track_id": 2,
        "title": "First",
        "artist": "Artist",
        "album": None,
    }
    second = {
        "track_id": 3,
        "title": "Second",
        "artist": "Artist",
        "album": None,
    }
    cog._current_meta[guild.id] = old
    cog._controller_meta[guild.id] = old
    cog._queued_meta[guild.id].extend((first, second))
    event_task = None

    async def skip() -> None:
        nonlocal event_task
        event_task = asyncio.create_task(
            cog.on_red_audio_track_start(
                guild,
                SimpleNamespace(title="First", author="Artist"),
                SimpleNamespace(),
            )
        )
        await asyncio.sleep(0)

    player = SimpleNamespace(current=object(), skip=skip)
    interaction = make_controller_interaction(guild)
    with (
        patch.object(
            type(cog),
            "_get_player_for_guild",
            new=AsyncMock(return_value=player),
        ),
        patch.object(
            type(cog),
            "_controller_view",
            new=AsyncMock(return_value=SimpleNamespace(children=[1])),
        ),
        patch.object(
            type(cog),
            "_resend_controller_for_track_start",
            new=AsyncMock(),
        ),
    ):
        await cog.controller_skip(interaction)
    assert event_task is not None
    await event_task

    assert cog._current_meta[guild.id] == first
    assert list(cog._queued_meta[guild.id]) == [second]


@pytest.mark.asyncio
async def test_skip_with_empty_queue_does_not_resend_stale_controller(cog) -> None:
    guild = SimpleNamespace(id=52)
    stale = {"track_id": 1, "title": "Old", "artist": "Artist", "album": None}
    cog._current_meta[guild.id] = stale
    cog._controller_meta[guild.id] = stale
    interaction = make_controller_interaction(guild)
    player = SimpleNamespace(current=object(), skip=AsyncMock())
    with (
        patch.object(
            type(cog),
            "_get_player_for_guild",
            new=AsyncMock(return_value=player),
        ),
        patch.object(type(cog), "_controller_view", new=AsyncMock()) as controller_view,
    ):
        await cog.controller_skip(interaction)

    assert guild.id not in cog._current_meta
    assert guild.id not in cog._controller_meta
    controller_view.assert_not_awaited()
    interaction.followup.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_queue_end_without_autoplay_removes_stale_controller(cog) -> None:
    guild = SimpleNamespace(id=53)
    meta = {
        "track_id": 1,
        "title": "Ended",
        "artist": "Artist",
        "album": None,
    }
    message = SimpleNamespace(delete=AsyncMock())
    view = MagicMock()
    cog._current_meta[guild.id] = meta
    cog._controller_meta[guild.id] = meta
    cog._controller_messages[guild.id] = message
    cog._controller_views[guild.id] = view

    await cog.on_red_audio_queue_end(guild, SimpleNamespace(), SimpleNamespace())

    assert guild.id not in cog._current_meta
    assert guild.id not in cog._controller_meta
    assert guild.id not in cog._controller_messages
    view.stop.assert_called_once()
    message.delete.assert_awaited_once()
