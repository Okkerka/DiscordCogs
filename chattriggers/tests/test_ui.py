from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest


@pytest.mark.asyncio
async def test_all_triggers_accessible_across_pages(trigger_cog):
    from chattriggers.ui import TriggerPanel

    for number in range(31):
        await trigger_cog.save_trigger(1, f"trigger {number:02}", {"title": "Alert"})
    view = TriggerPanel(trigger_cog, 2, 1)
    await view.build()
    first = next(
        item for item in view.walk_children() if isinstance(item, discord.ui.Select)
    )
    assert len(first.options) <= 25
    first_keys = {option.value for option in first.options}
    view.page = 1
    await view.build()
    second = next(
        item for item in view.walk_children() if isinstance(item, discord.ui.Select)
    )
    assert len(first_keys | {option.value for option in second.options}) == 31
    assert view.has_components_v2()
    assert view.content_length() <= 4000


@pytest.mark.asyncio
async def test_creation_exposes_both_audio_modes(trigger_cog):
    from chattriggers.ui import CreateTriggerView

    view = CreateTriggerView(trigger_cog, 2, 1)
    options = next(
        item for item in view.walk_children() if isinstance(item, discord.ui.Select)
    ).options
    assert {option.value for option in options} == {"skip", "interrupt"}
    assert view.audio_mode == "skip"


@pytest.mark.asyncio
async def test_revoked_manager_cannot_use_old_panel(trigger_cog):
    from chattriggers.ui import TriggerPanel

    view = TriggerPanel(trigger_cog, 2, 1)
    user = SimpleNamespace(id=2, guild_permissions=SimpleNamespace(manage_guild=False))
    interaction = SimpleNamespace(
        user=user,
        guild_id=1,
        guild=SimpleNamespace(id=1),
        response=SimpleNamespace(send_message=AsyncMock()),
    )
    await trigger_cog.change_permission(1, "admin_users", 2, True)
    assert await view.interaction_check(interaction)
    await trigger_cog.change_permission(1, "admin_users", 2, False)
    assert not await view.interaction_check(interaction)


@pytest.mark.asyncio
async def test_modal_rechecks_revoked_permission(trigger_cog):
    from chattriggers.ui import CreateTriggerView, TriggerModal

    view = CreateTriggerView(trigger_cog, 2, 1)
    modal = TriggerModal(view, audio_mode="interrupt")
    interaction = SimpleNamespace(
        user=SimpleNamespace(
            id=2, guild_permissions=SimpleNamespace(manage_guild=False)
        ),
        guild_id=1,
        guild=SimpleNamespace(id=1),
        response=SimpleNamespace(send_message=AsyncMock()),
    )
    await modal.on_submit(interaction)
    assert (await trigger_cog.get_settings(1))["triggers"] == {}
