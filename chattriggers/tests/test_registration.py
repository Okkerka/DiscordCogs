from types import SimpleNamespace

import discord
import pytest
from discord.ext import commands


@pytest.mark.asyncio
async def test_prefix_alias_and_slash_registration(trigger_cog):
    bot = commands.Bot(command_prefix=">", intents=discord.Intents.none())
    async with bot:
        await bot.add_cog(trigger_cog)
        payload = bot.tree.get_command("chattrigger").to_dict(bot.tree)
        options = {option["name"]: option for option in payload["options"]}
        assert {
            "settings",
            "list",
            "add_perm",
            "remove_perm",
            "add_manager",
            "remove_manager",
            "test",
            "cooldown",
            "audio",
        } <= options.keys()
        assert bot.get_command("alert") is bot.get_command("chattrigger")
        assert {
            item["value"] for item in options["audio"]["options"][0]["choices"]
        } == {"skip", "interrupt"}


@pytest.mark.asyncio
async def test_slash_children_reject_dms(trigger_cog):
    with pytest.raises(commands.NoPrivateMessage):
        await discord.utils.maybe_coroutine(
            trigger_cog.cog_check, SimpleNamespace(guild=None)
        )
