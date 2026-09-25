import discord
import pytest
from discord.ext import commands

from moderation.moderation import Moderation


@pytest.mark.asyncio
async def test_real_tree_registers_groups_and_exact_id_strings():
    bot = commands.Bot(command_prefix=">", intents=discord.Intents.none())
    async with bot:
        cog = Moderation.__new__(Moderation)
        cog.cog_unload = lambda: None
        await bot.add_cog(cog)
        payloads = {cmd.name: cmd.to_dict(bot.tree) for cmd in bot.tree.get_commands()}
        assert len(payloads) == 29
        assert {item["name"] for item in payloads["purge"]["options"]} >= {
            "recent",
            "bots",
            "contains",
            "user",
        }
        assert (
            payloads["unban"]["options"][0]["type"]
            == discord.AppCommandOptionType.string.value
        )
        assert bot.get_command("clean") is bot.get_command("purge")
