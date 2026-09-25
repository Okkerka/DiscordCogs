import discord
import pytest
from discord.ext import commands

from utilities.utilities import Utilities


@pytest.mark.asyncio
async def test_real_tree_registration_serializes_without_thanos():
    bot = commands.Bot(command_prefix=">", intents=discord.Intents.none())
    async with bot:
        cog = Utilities.__new__(Utilities)
        cog.cog_unload = lambda: None
        await bot.add_cog(cog)
        payloads = [cmd.to_dict(bot.tree) for cmd in bot.tree.get_commands()]
        assert len(payloads) == 25
        assert "thanos" not in {item["name"] for item in payloads}
        assert bot.get_command("thanos") is not None
