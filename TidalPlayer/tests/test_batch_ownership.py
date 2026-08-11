import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from TidalPlayer.ui.embeds import Messages


@pytest.mark.asyncio
async def test_second_batch_in_same_guild_is_rejected(cog) -> None:
    guild_id = 81
    existing = asyncio.Event()
    cog._cancel_events[guild_id] = existing
    progress = SimpleNamespace(edit=AsyncMock())
    ctx = SimpleNamespace(
        guild=SimpleNamespace(id=guild_id),
        author=SimpleNamespace(),
        channel=SimpleNamespace(),
        send=AsyncMock(return_value=progress),
    )
    with (
        patch.object(type(cog), "check_ready", new=AsyncMock(return_value=True)),
        patch.object(
            type(cog),
            "_ensure_player",
            new=AsyncMock(return_value=SimpleNamespace()),
        ),
        patch.object(
            type(cog), "_resolve_and_extract", new=AsyncMock(return_value=None)
        ),
        patch.object(type(cog), "_queue_resolved_chunk", new=AsyncMock()) as queue_chunk,
    ):
        await cog._process_track_list(
            ctx, [object()], "Second", lambda item: item
        )

    queue_chunk.assert_not_awaited()
    assert cog._cancel_events[guild_id] is existing
    assert (
        ctx.send.await_args_list[0].kwargs["embed"].description
        == Messages.ERROR_BATCH_IN_PROGRESS
    )


def test_batch_event_release_requires_the_owner(cog) -> None:
    owned = cog._claim_batch(82)
    assert owned is not None
    assert cog._claim_batch(82) is None

    cog._release_batch(82, asyncio.Event())
    assert cog._cancel_events[82] is owned

    cog._release_batch(82, owned)
    assert 82 not in cog._cancel_events


@pytest.mark.asyncio
async def test_tstop_sets_only_the_active_batch_event(cog) -> None:
    guild = SimpleNamespace(id=83)
    event = cog._claim_batch(guild.id)
    ctx = SimpleNamespace(guild=guild, send=AsyncMock())

    await cog.tstop(ctx)

    assert event is not None and event.is_set()


@pytest.mark.asyncio
async def test_tstop_without_active_batch_does_not_create_an_event(cog) -> None:
    guild = SimpleNamespace(id=84)
    ctx = SimpleNamespace(guild=guild, send=AsyncMock())

    await cog.tstop(ctx)

    assert guild.id not in cog._cancel_events
