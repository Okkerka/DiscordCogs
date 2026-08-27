from __future__ import annotations

import sys
import types
import unittest


class FakeEmbed:
    def __init__(self, *, title=None, description=None, colour=None):
        self.title = title
        self.description = description
        self.colour = colour
        self.fields = []
        self.footer = None

    def add_field(self, **field):
        self.fields.append(field)

    def set_footer(self, *, text):
        self.footer = text


class FakeColour:
    @staticmethod
    def blurple():
        return 0x5865F2


class FakeSimpleMenu:
    def __init__(self, pages):
        self.pages = pages

    async def start(self, ctx):
        for page in self.pages:
            await ctx.send(embed=page)


class FakeContext:
    def __init__(self):
        self.embeds = []
        self.messages = []

    async def send(self, content=None, *, embed=None):
        if embed is not None:
            self.embeds.append(embed)
        elif content is not None:
            self.messages.append(content)


def passthrough_decorator(*_args, **_kwargs):
    return lambda function: function


discord = types.ModuleType("discord")
discord.Embed = FakeEmbed
discord.Colour = FakeColour

commands = types.ModuleType("redbot.core.commands")
commands.Cog = object
commands.Context = object
commands.command = passthrough_decorator
commands.is_owner = passthrough_decorator

redbot = types.ModuleType("redbot")
redbot_core = types.ModuleType("redbot.core")
redbot_core.commands = commands
redbot_utils = types.ModuleType("redbot.core.utils")
redbot_menus = types.ModuleType("redbot.core.utils.menus")
redbot_menus.SimpleMenu = FakeSimpleMenu

fake_modules = {
    "discord": discord,
    "redbot": redbot,
    "redbot.core": redbot_core,
    "redbot.core.commands": commands,
    "redbot.core.utils": redbot_utils,
    "redbot.core.utils.menus": redbot_menus,
}
original_modules = {name: sys.modules.get(name) for name in fake_modules}
sys.modules.update(fake_modules)

from deepwoken.deepwoken import Deepwoken

for name, module in original_modules.items():
    if module is None:
        sys.modules.pop(name, None)
    else:
        sys.modules[name] = module


class DeepwokenTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.cog = Deepwoken.__new__(Deepwoken)

    @staticmethod
    def weapon(name, weapon_class, requirements, scaling, base_damage):
        return {
            "Name": name,
            "Weapon Class": weapon_class,
            "Weapon Type": f"{weapon_class} Weapon",
            "Requirements": requirements,
            "Base Damage": str(base_damage),
            "Scaling": scaling,
            "Swing Speed": "1x",
            "Endlag": "-",
        }

    def test_concatenated_requirement_keeps_known_stat(self):
        self.assertEqual(
            self.cog._requirements("50 MEDJusticar Origin"),
            {"MED": 50},
        )

    async def test_multiple_weapon_stats_share_one_order_independent_ranking(self):
        self.cog.weapons = [
            self.weapon("Heavy Only", "Heavy", "100 HVY", "HVY: 10", 20),
            self.weapon("Medium Only", "Medium", "100 MED", "MED: 10", 18),
            self.weapon("Hybrid", "Hybrid", "100 HVY100 MED", "HVY: 5MED: 5", 10),
        ]

        heavy_first = FakeContext()
        parsed = self.cog._parse_stat_query(("heavy", "100", "medium", "100", "prof", "6"))
        self.assertIsNotNone(parsed)
        await self.cog._compare(heavy_first, *parsed)

        medium_first = FakeContext()
        parsed = self.cog._parse_stat_query(("medium", "100", "heavy", "100", "prof", "6"))
        self.assertIsNotNone(parsed)
        await self.cog._compare(medium_first, *parsed)

        self.assertEqual(heavy_first.embeds[0].title, medium_first.embeds[0].title)
        self.assertEqual(heavy_first.embeds[0].description, medium_first.embeds[0].description)
        for name in ("Heavy Only", "Medium Only", "Hybrid"):
            self.assertIn(name, heavy_first.embeds[0].description)

    async def test_ranking_paginates_every_eligible_weapon(self):
        self.cog.weapons = [
            self.weapon(f"Weapon {index:02}", "Heavy", "0 HVY", "HVY: 5", 40 - index)
            for index in range(20)
        ]
        ctx = FakeContext()

        parsed = self.cog._parse_stat_query(("heavy", "100", "6"))
        self.assertIsNotNone(parsed)
        await self.cog._compare(ctx, *parsed)

        rendered = "\n".join(embed.description for embed in ctx.embeds)
        self.assertEqual(len(ctx.embeds), 2)
        for index in range(20):
            self.assertIn(f"Weapon {index:02}", rendered)

    async def test_invalid_stat_query_reports_valid_ranges(self):
        self.cog.weapons = []
        invalid_queries = (
            ("heavy", "101", "prof", "6"),
            ("heavy", "-1", "prof", "6"),
            ("heavy", "1.5", "prof", "6"),
            ("heavy", "²", "6"),
            ("heavy", "100", "prof", "⁶"),
        )

        for query in invalid_queries:
            with self.subTest(query=query):
                ctx = FakeContext()
                await self.cog.dwweapon(ctx, *query)
                self.assertEqual(len(ctx.messages), 1)
                self.assertIn("0 to 100", ctx.messages[0])
                self.assertIn("0 to 6", ctx.messages[0])

    async def test_equal_dps_weapons_use_stable_name_order(self):
        self.cog.weapons = [
            self.weapon("Zulu Blade", "Heavy", "0 HVY", "HVY: 5", 20),
            self.weapon("Alpha Blade", "Heavy", "0 HVY", "HVY: 5", 20),
        ]
        ctx = FakeContext()

        parsed = self.cog._parse_stat_query(("heavy", "100", "6"))
        self.assertIsNotNone(parsed)
        await self.cog._compare(ctx, *parsed)

        description = ctx.embeds[0].description
        self.assertLess(description.index("Alpha Blade"), description.index("Zulu Blade"))

    async def test_weapon_name_starting_with_stat_alias_remains_lookupable(self):
        self.cog.weapons = [
            self.weapon("Frost Gauntlets", "Light", "0 LHT", "LHT: 5ICE: 5", 20),
        ]
        ctx = FakeContext()

        await self.cog.dwweapon(ctx, "Frost", "Gauntlets")

        self.assertEqual(ctx.messages, [])
        self.assertEqual(ctx.embeds[0].title, "Frost Gauntlets")


if __name__ == "__main__":
    unittest.main()
