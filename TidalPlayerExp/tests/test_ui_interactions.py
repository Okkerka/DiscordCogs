"""Stale component interactions abort quietly instead of raising on defer."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from TidalPlayerExp.ui import interactions as interactions_module
from TidalPlayerExp.ui.interactions import safe_defer


def make_interaction(*, is_done=None, defer=None):
    response = SimpleNamespace(defer=defer if defer is not None else AsyncMock())
    if is_done is not None:
        response.is_done = is_done
    return SimpleNamespace(response=response)


@pytest.mark.asyncio
async def test_safe_defer_acknowledges_a_pending_response_with_keywords() -> None:
    interaction = make_interaction()
    assert await safe_defer(interaction, ephemeral=True, thinking=True) is True
    interaction.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)


@pytest.mark.asyncio
async def test_safe_defer_skips_an_already_acknowledged_response() -> None:
    interaction = make_interaction(is_done=MagicMock(return_value=True))
    assert await safe_defer(interaction) is True
    interaction.response.defer.assert_not_awaited()


@pytest.mark.asyncio
async def test_safe_defer_tolerates_a_response_without_is_done() -> None:
    interaction = make_interaction()
    assert await safe_defer(interaction) is True
    interaction.response.defer.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_safe_defer_reports_an_expired_token() -> None:
    interaction = make_interaction(
        defer=AsyncMock(
            side_effect=interactions_module.discord.NotFound(
                SimpleNamespace(status=404, reason="Not Found"), "Unknown interaction"
            )
        )
    )
    assert await safe_defer(interaction) is False


@pytest.mark.asyncio
async def test_safe_defer_accepts_an_interaction_without_response() -> None:
    assert await safe_defer(SimpleNamespace()) is True
