from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
import pytest

from utilities.utilities import Utilities


@pytest.mark.asyncio
async def test_prefix_cannot_copy_another_channels_content():
    channel = Mock(spec=discord.TextChannel)
    channel.id = 456
    channel.fetch_message = AsyncMock()
    cog = object.__new__(Utilities)
    ctx = SimpleNamespace(
        guild=SimpleNamespace(id=123, get_channel_or_thread=lambda uid: channel),
        channel=SimpleNamespace(id=999),
        interaction=None,
        send=AsyncMock(),
    )
    await Utilities.quote.callback(cog, ctx, "https://discord.com/channels/123/456/789")
    channel.fetch_message.assert_not_awaited()
    assert "source channel" in ctx.send.call_args.args[0]


@pytest.mark.asyncio
async def test_quote_requires_source_history_access():
    channel = Mock(spec=discord.TextChannel)
    channel.id = 456
    channel.permissions_for.return_value = SimpleNamespace(
        view_channel=True, read_message_history=False
    )
    channel.fetch_message = AsyncMock()
    cog = object.__new__(Utilities)
    ctx = SimpleNamespace(
        guild=SimpleNamespace(
            id=123, me=object(), get_channel_or_thread=lambda uid: channel
        ),
        author=object(),
        channel=channel,
        interaction=object(),
        send=AsyncMock(),
    )
    await Utilities.quote.callback(cog, ctx, "https://discord.com/channels/123/456/789")
    channel.fetch_message.assert_not_awaited()
    assert ctx.send.call_args.kwargs["ephemeral"]
