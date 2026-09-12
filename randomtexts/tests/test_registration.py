from types import SimpleNamespace

import discord
import pytest
from discord.ext import commands


@pytest.mark.asyncio
async def test_prefix_and_slash_registration(random_cog):
    bot = commands.Bot(command_prefix=">", intents=discord.Intents.none())
    async with bot:
        await bot.add_cog(random_cog)
        payloads = {
            command.name: command.to_dict(bot.tree)
            for command in bot.tree.get_commands()
        }
        assert set(payloads) == {"randomtext", "copypasta"}
        options = {option["name"] for option in payloads["randomtext"]["options"]}
        assert {
            "settings",
            "toggle",
            "settarget",
            "frequency",
            "category",
            "channel",
        } <= options
        assert bot.get_command("randomtext settarget") is not None


@pytest.mark.asyncio
async def test_slash_settings_child_rejects_non_admin(random_cog):
    ctx = SimpleNamespace(
        guild=SimpleNamespace(id=1),
        command=random_cog.frequency,
        author=SimpleNamespace(
            id=2, guild_permissions=SimpleNamespace(manage_guild=False)
        ),
    )
    with pytest.raises(commands.CheckFailure):
        await discord.utils.maybe_coroutine(random_cog.cog_check, ctx)


@pytest.mark.asyncio
async def test_copypasta_stays_public(random_cog):
    ctx = SimpleNamespace(guild=None, command=random_cog.copypasta)
    assert await discord.utils.maybe_coroutine(random_cog.cog_check, ctx)
