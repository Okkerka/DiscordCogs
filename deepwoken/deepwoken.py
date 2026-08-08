from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import discord
from openpyxl import load_workbook
from redbot.core import commands


CLASS_ALIASES = {
    "light": "Light",
    "lht": "Light",
    "medium": "Medium",
    "med": "Medium",
    "heavy": "Heavy",
    "hvy": "Heavy",
    "hybrid": "Hybrid",
    "elemental": "Elemental",
    "gun": "Gun",
    "guns": "Gun",
    "fist": "Fighting Style",
    "fists": "Fighting Style",
    "fighting": "Fighting Style",
    "style": "Fighting Style",
    "crazy": "Crazy Slots",
    "slots": "Crazy Slots",
}

RANKING_EXCLUSIONS = {
    "Ebonshard Lexicon",
    "The Rock",
    "The Endless Wave",
    "Unsung Scythern",
    "Worldpainter Brush",
    "Keyblade",
    "Soulshot",
    "Metal Greatsword",
    "Prototype Railblade",
    "Par's Glaive",
    "Saintsblade",
    "Ferractine",
    "Formless Shard",
    "Handcuffs",
}


class Deepwoken(commands.Cog):
    """Deepwoken weapon base-stat lookup and standard weapon ranking."""

    def __init__(self, bot):
        self.bot = bot
        self.workbook_path = Path(__file__).parent / "data" / "weapons.xlsx"
        self.weapons = self._load_weapons()

    def _load_weapons(self) -> list[dict[str, Any]]:
        if not self.workbook_path.exists():
            raise FileNotFoundError(f"Missing bundled workbook: {self.workbook_path}")

        workbook = load_workbook(self.workbook_path, read_only=True, data_only=True)
        sheet = workbook["All Weapons"]
        headers = [str(cell.value or "").strip() for cell in next(sheet.iter_rows(max_row=1))]
        weapons = []
        seen = set()

        for values in sheet.iter_rows(min_row=2, values_only=True):
            row = {header: value for header, value in zip(headers, values)}
            name = str(row.get("Name") or "").strip()
            if not name:
                continue

            key = (
                name.casefold(),
                str(row.get("Requirements") or ""),
                str(row.get("Base Damage") or ""),
                str(row.get("Scaling") or ""),
                str(row.get("Swing Speed") or ""),
            )
            if key in seen:
                continue
            seen.add(key)
            weapons.append(row)

        workbook.close()
        return weapons

    @staticmethod
    def _number(value: Any) -> float | None:
        match = re.search(r"-?\d+(?:\.\d+)?", str(value or ""))
        return float(match.group()) if match else None

    @staticmethod
    def _requirements(value: Any) -> dict[str, int]:
        output = {}
        text = str(value or "").upper().replace(" OR ", " ")
        for amount, stat in re.findall(r"(\d+)\s*([A-Z]{2,4})(?=\s|\d|$)", text):
            output[stat] = int(amount)
        return output

    @staticmethod
    def _scaling(value: Any) -> dict[str, float]:
        return {
            stat: float(amount)
            for stat, amount in re.findall(
                r"([A-Z]{2,4})\s*:\s*(\d+(?:\.\d+)?)",
                str(value or "").upper(),
            )
        }

    @staticmethod
    def _primary_stat(weapon_class: str) -> str | None:
        return {
            "Light": "LHT",
            "Medium": "MED",
            "Heavy": "HVY",
        }.get(weapon_class)

    def _damage(self, row: dict[str, Any], investment: int, proficiency: int) -> float | None:
        """Raw M1 damage before target resistance, PEN, enchants, talents, and procs."""
        base = self._number(row.get("Base Damage"))
        if base is None:
            return None

        scaling = self._scaling(row.get("Scaling"))
        requirements = self._requirements(row.get("Requirements"))
        primary_stat = self._primary_stat(str(row.get("Weapon Class") or ""))
        proficiency_multiplier = 1 + (proficiency * 0.065)
        damage = base

        for stat, scale in scaling.items():
            stat_level = investment if stat == primary_stat else requirements.get(stat, 0)
            damage += (
                0.00075
                * base
                * scale
                * stat_level
                * proficiency_multiplier
            )

        return damage

    def _sustained_dps(self, row: dict[str, Any], investment: int, proficiency: int) -> float | None:
        """M1 damage divided by attack interval, including listed additional endlag."""
        damage = self._damage(row, investment, proficiency)
        speed = self._number(row.get("Swing Speed"))
        if damage is None or speed is None or speed <= 0:
            return None

        endlag = self._number(row.get("Endlag")) or 0.0
        attack_interval = (1 / (speed * 2)) + endlag
        return damage / attack_interval

    def _is_rankable(self, row: dict[str, Any]) -> bool:
        name = str(row.get("Name") or "").strip()
        weapon_class = str(row.get("Weapon Class") or "")
        tags = str(row.get("Tags") or "")

        if name in RANKING_EXCLUSIONS:
            return False
        if weapon_class in {"Special / Other", "Crazy Slots", "Elemental", "Fighting Style"}:
            return False
        if "Crazy Slots" in tags:
            return False

        requirements = self._requirements(row.get("Requirements"))
        return not any(value > 100 for value in requirements.values())

    def _find_weapon(self, query: str) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
        query = query.casefold().strip()
        exact = [row for row in self.weapons if str(row.get("Name") or "").casefold() == query]
        if exact:
            return exact[0], []

        matches = [row for row in self.weapons if query in str(row.get("Name") or "").casefold()]
        if len(matches) == 1:
            return matches[0], []
        return None, matches

    @commands.command(name="dwweapon", aliases=["dw", "weapon"])
    async def dwweapon(self, ctx: commands.Context, *args: str):
        """Look up a weapon, or rank a class: [p]dwweapon heavy 100 6."""
        if not args:
            await ctx.send("Use `[p]dwweapon <name>` or `[p]dwweapon heavy 100 6`.")
            return

        requested_class = CLASS_ALIASES.get(args[0].casefold())
        if requested_class and len(args) in {2, 3}:
            try:
                investment = int(args[1])
                proficiency = int(args[2]) if len(args) == 3 else 0
            except ValueError:
                await ctx.send("Use whole numbers, e.g. `[p]dwweapon heavy 100 6`.")
                return

            if not 0 <= investment <= 100 or not 0 <= proficiency <= 6:
                await ctx.send("Investment must be 0-100 and proficiency must be 0-6.")
                return

            await self._class_compare(ctx, requested_class, investment, proficiency)
            return

        await self._weapon_lookup(ctx, " ".join(args))

    async def _class_compare(
        self,
        ctx: commands.Context,
        weapon_class: str,
        investment: int,
        proficiency: int,
    ):
        ranked = []

        for row in self.weapons:
            if row.get("Weapon Class") != weapon_class:
                continue
            if not self._is_rankable(row):
                continue

            damage = self._damage(row, investment, proficiency)
            dps = self._sustained_dps(row, investment, proficiency)
            if damage is not None and dps is not None:
                ranked.append((dps, damage, row))

        if not ranked:
            await ctx.send(f"No standard {weapon_class} weapons were found.")
            return

        ranked.sort(key=lambda item: item[0], reverse=True)
        lines = []

        for position, (dps, damage, row) in enumerate(ranked[:15], start=1):
            weapon_type = str(row.get("Weapon Type") or "Unknown")
            lines.append(
                f"`{position:>2}.` **{row['Name']}** ({weapon_type})\n"
                f"`DPS:` {dps:.2f} | `M1:` {damage:.2f}"
            )

        embed = discord.Embed(
            title=(
                f"{weapon_class} ranking | "
                f"{investment} investment | "
                f"{proficiency} proficiency"
            ),
            description="\n".join(lines),
            colour=discord.Colour.blurple(),
        )
        embed.set_footer(
            text=(
                "Standard obtainable weapons only. Sustained DPS includes listed Endlag. "
                "Bleed, crits, procs, enchants, talents, PEN, and resistance are excluded."
            )
        )
        await ctx.send(embed=embed)

    async def _weapon_lookup(self, ctx: commands.Context, query: str):
        row, matches = self._find_weapon(query)
        if row is None:
            if matches:
                suggestions = ", ".join(str(match["Name"]) for match in matches[:10])
                await ctx.send(f"Multiple matches: {suggestions}")
            else:
                await ctx.send("Weapon not found.")
            return

        embed = discord.Embed(
            title=str(row.get("Name") or "Unknown weapon"),
            colour=discord.Colour.blurple(),
        )
        embed.add_field(
            name="Class / Type",
            value=f"{row.get('Weapon Class') or '?'} / {row.get('Weapon Type') or '?'}",
            inline=False,
        )
        embed.add_field(name="Requirements", value=str(row.get("Requirements") or "N/A"), inline=False)
        embed.add_field(name="Base Damage", value=str(row.get("Base Damage") or "N/A"))
        embed.add_field(name="Scaled Damage", value=str(row.get("Scaled Damage") or "N/A"))
        embed.add_field(name="Scaling", value=str(row.get("Scaling") or "N/A"))
        embed.add_field(name="Armor Penetration", value=str(row.get("Armor Penetration") or "N/A"))
        embed.add_field(name="Chip Damage", value=str(row.get("Chip Damage") or "N/A"))
        embed.add_field(name="Posture Damage", value=str(row.get("Posture Damage") or "N/A"))
        embed.add_field(name="Range", value=str(row.get("Range") or "N/A"))
        embed.add_field(name="Swing Speed", value=str(row.get("Swing Speed") or "N/A"))
        embed.add_field(name="Endlag", value=str(row.get("Endlag") or "N/A"))
        embed.add_field(name="Tags", value=str(row.get("Tags") or "None"), inline=False)
        embed.set_footer(text="Use [p]dwweapon heavy 100 6 to rank standard heavy weapons.")
        await ctx.send(embed=embed)

    @commands.command(name="dwreload")
    @commands.is_owner()
    async def dwreload(self, ctx: commands.Context):
        """Reload the bundled workbook."""
        self.weapons = self._load_weapons()
        await ctx.send(f"Reloaded {len(self.weapons)} unique weapon rows.")
