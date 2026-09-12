from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

from randomtexts.ui import RandomTextView


@pytest.mark.asyncio
async def test_panel_serializes_saved_channel_and_category_choices(random_cog):
    await random_cog.update_settings(1, channels=[123], categories=["fact"])
    view = RandomTextView(random_cog, 2, 1)
    await view.build()
    assert view.has_components_v2()
    channels = next(
        item
        for item in view.walk_children()
        if isinstance(item, discord.ui.ChannelSelect)
    )
    categories = next(
        item for item in view.walk_children() if type(item) is discord.ui.Select
    )
    assert channels.to_component_dict()["default_values"] == [
        {"id": 123, "type": "channel"}
    ]
    assert [option.value for option in categories.options if option.default] == ["fact"]
    assert view.content_length() < 4000


@pytest.mark.asyncio
async def test_panel_rejects_other_user_and_revoked_admin(random_cog):
    view = RandomTextView(random_cog, 2, 1)
    user = SimpleNamespace(id=3, guild_permissions=SimpleNamespace(manage_guild=True))
    interaction = SimpleNamespace(
        user=user,
        guild_id=1,
        guild=SimpleNamespace(id=1),
        response=SimpleNamespace(send_message=AsyncMock()),
    )
    assert not await view.interaction_check(interaction)
    user.id = 2
    assert await view.interaction_check(interaction)
    user.guild_permissions.manage_guild = False
    assert not await view.interaction_check(interaction)


@pytest.mark.asyncio
async def test_timeout_disables_controls(random_cog):
    view = RandomTextView(random_cog, 2, 1)
    await view.build()
    view.message = SimpleNamespace(edit=AsyncMock())
    await view.on_timeout()
    assert all(
        item.disabled for item in view.walk_children() if hasattr(item, "disabled")
    )


@pytest.mark.asyncio
async def test_blocked_admin_cannot_use_existing_panel(random_cog):
    view = RandomTextView(random_cog, 2, 1)
    random_cog.bot.allowed_by_whitelist_blacklist.return_value = False
    interaction = SimpleNamespace(
        user=SimpleNamespace(
            id=2, guild_permissions=SimpleNamespace(manage_guild=True)
        ),
        guild_id=1,
        guild=SimpleNamespace(id=1),
        response=SimpleNamespace(send_message=AsyncMock()),
    )
    assert not await view.interaction_check(interaction)
