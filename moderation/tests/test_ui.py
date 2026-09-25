from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

from moderation.ui import ConfirmView, HistoryView


@pytest.mark.asyncio
async def test_confirmation_only_accepts_invoker_and_defaults_to_cancel():
    view = ConfirmView(1)
    other = SimpleNamespace(
        user=SimpleNamespace(id=2), response=SimpleNamespace(send_message=AsyncMock())
    )
    assert not view.confirmed
    assert not await view.interaction_check(other)
    assert not view.confirmed
    assert other.response.send_message.call_args.kwargs["ephemeral"]
    view.stop()


@pytest.mark.asyncio
async def test_history_navigation_wraps_pages():
    pages = [discord.Embed(title="one"), discord.Embed(title="two")]
    view = HistoryView(1, pages)
    interaction = SimpleNamespace(
        user=SimpleNamespace(id=1), response=SimpleNamespace(edit_message=AsyncMock())
    )
    assert await view.interaction_check(interaction)
    await view.previous.callback(interaction)
    assert view.page == 1
    await view.next.callback(interaction)
    assert view.page == 0
    view.stop()
