"""Real Red/discord.py registration checks, isolated from unit-test doubles."""

import subprocess
import sys
import textwrap
import unittest
from pathlib import Path


class RegistrationTests(unittest.TestCase):
    def test_hybrid_commands_register_and_keep_owner_checks(self):
        script = textwrap.dedent('''
            import asyncio
            import discord
            from discord.ext import commands
            from types import SimpleNamespace
            from unittest.mock import AsyncMock
            from redbot.core.commands.requires import PrivilegeLevel
            from deepwoken.deepwoken import Deepwoken, PublicSimpleMenu

            async def main():
                bot = commands.Bot(command_prefix=">", intents=discord.Intents.none())
                async with bot:
                    cog = Deepwoken.__new__(Deepwoken)
                    await bot.add_cog(cog)
                    payloads = {command.name: command.to_dict(bot.tree)
                                for command in bot.tree.get_commands()}
                    assert set(payloads) == {"dwweapon", "dwreload"}
                    options = payloads["dwweapon"]["options"]
                    assert len(options) == 1 and options[0]["name"] == "query"
                    assert options[0]["type"] == 3
                    assert "Weapon name" in options[0]["description"]
                    assert bot.get_command("dw") is bot.get_command("dwweapon")
                    assert bot.get_command("weapon") is bot.get_command("dwweapon")
                    assert cog.dwweapon.requires.privilege_level != PrivilegeLevel.BOT_OWNER
                    assert cog.dwreload.requires.privilege_level == PrivilegeLevel.BOT_OWNER
                    ctx = SimpleNamespace(bot=SimpleNamespace(is_owner=AsyncMock(return_value=False)),
                                          author=object(), cog=cog, guild=None,
                                          bot_permissions=discord.Permissions.all())
                    cog.dwreload.requires.ready_event.set()
                    assert not await cog.dwreload.requires.verify(ctx)
                    ctx.bot.is_owner.return_value = True
                    assert await cog.dwreload.requires.verify(ctx)
                    # Public pagination also accepts someone other than the invoker.
                    menu = PublicSimpleMenu([discord.Embed(title="One"), discord.Embed(title="Two")])
                    try:
                        assert await menu.interaction_check(SimpleNamespace(user=object()))
                    finally:
                        menu.stop()
            asyncio.run(main())
        ''')
        result = subprocess.run(
            [sys.executable, "-c", script], cwd=Path(__file__).parents[2],
            capture_output=True, text=True, timeout=20, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
