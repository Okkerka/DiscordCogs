from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import discord
from openpyxl import load_workbook
from redbot.core import commands
from redbot.core.utils.menus import SimpleMenu

STAT_ALIASES = {
    "light": "LHT", "lht": "LHT", "medium": "MED", "med": "MED", "heavy": "HVY", "hvy": "HVY",
    "flame": "FIR", "fire": "FIR", "fir": "FIR", "frost": "ICE", "ice": "ICE",
    "lightning": "LTN", "thunder": "LTN", "ltn": "LTN", "wind": "WND", "gale": "WND", "wnd": "WND",
    "shadow": "SDW", "sdw": "SDW", "blood": "BLD", "bloodrend": "BLD", "bld": "BLD",
    "metal": "MTL", "mtl": "MTL", "strength": "STR", "str": "STR", "fortitude": "FTD", "ftd": "FTD",
    "agility": "AGI", "agi": "AGI", "intelligence": "INT", "int": "INT", "charisma": "CHA", "cha": "CHA",
    "willpower": "WLL", "will": "WLL", "wll": "WLL", "mind": "MND", "mnd": "MND", "body": "BDY", "bdy": "BDY",
}
STAT_PATTERN = "|".join(sorted(set(STAT_ALIASES.values())))

RANKING_EXCLUSIONS = {
    "Ebonshard Lexicon", "The Rock", "The Endless Wave", "Unsung Scythern", "Worldpainter Brush",
    "Keyblade", "Soulshot", "Metal Greatsword", "Prototype Railblade", "Par's Glaive", "Saintsblade",
    "Ferractine", "Formless Shard", "Handcuffs",
}
RANKING_PAGE_SIZE = 15


class Deepwoken(commands.Cog):
    """Deepwoken weapon lookup and valid multi-stat sustained-DPS comparison."""

    def __init__(self, bot):
        self.bot = bot
        self.workbook_path = Path(__file__).parent / "data" / "weapons.xlsx"
        self.weapons = self._load_weapons()

    @staticmethod
    def _number(value: Any) -> float | None:
        match = re.search(r"-?\d+(?:\.\d+)?", str(value or ""))
        return float(match.group()) if match else None

    @staticmethod
    def _requirements(value: Any) -> dict[str, int]:
        text = str(value or "").upper()
        return {
            stat: int(amount)
            for amount, stat in re.findall(rf"(\d+)\s*({STAT_PATTERN})", text)
        }

    @staticmethod
    def _scaling(value: Any) -> dict[str, float]:
        return {
            stat: float(amount)
            for stat, amount in re.findall(r"([A-Z]{2,4})\s*:\s*(\d+(?:\.\d+)?)", str(value or "").upper())
        }

    @staticmethod
    def _integer(value: str) -> int | None:
        return int(value) if re.fullmatch(r"-?[0-9]+", value) else None

    @staticmethod
    def _looks_like_stat_query(args: tuple[str, ...]) -> bool:
        if len(args) < 2 or args[0].casefold() not in STAT_ALIASES:
            return False
        numeric_token = args[1].lstrip("+-")
        looks_numeric = numeric_token.replace(".", "", 1).isnumeric()
        has_proficiency = any(token.casefold() in {"prof", "proficiency"} for token in args[2:])
        return looks_numeric or has_proficiency

    def _row_score(self, row: dict[str, Any]) -> int:
        score = 0
        requirements = str(row.get("Requirements") or "")
        scaling = str(row.get("Scaling") or "")
        if self._requirements(requirements): score += 4
        if "CRAZY SLOTS" in requirements.upper(): score += 4
        if ":" in scaling: score += 4
        if self._number(row.get("Base Damage")) is not None: score += 2
        if "x" in str(row.get("Swing Speed") or "").lower(): score += 2
        return score

    def _load_weapons(self) -> list[dict[str, Any]]:
        if not self.workbook_path.exists():
            raise FileNotFoundError(f"Missing bundled workbook: {self.workbook_path}")
        workbook = load_workbook(self.workbook_path, read_only=True, data_only=True)
        sheet = workbook["All Weapons"]
        headers = [str(cell.value or "").strip() for cell in next(sheet.iter_rows(max_row=1))]
        best_rows: dict[str, dict[str, Any]] = {}
        for values in sheet.iter_rows(min_row=2, values_only=True):
            row = dict(zip(headers, values))
            name = str(row.get("Name") or "").strip()
            if not name:
                continue
            key = name.casefold()
            if key not in best_rows or self._row_score(row) > self._row_score(best_rows[key]):
                best_rows[key] = row
        workbook.close()
        return list(best_rows.values())

    def _parse_stat_query(self, args: tuple[str, ...]) -> tuple[dict[str, int], int] | None:
        if len(args) < 2 or args[0].casefold() not in STAT_ALIASES:
            return None
        first_stat = STAT_ALIASES[args[0].casefold()]
        if len(args) == 3 and (stat_value := self._integer(args[1])) is not None and (prof_value := self._integer(args[2])) is not None:
            stats, prof = {first_stat: stat_value}, prof_value
            return (stats, prof) if 0 <= stats[first_stat] <= 100 and 0 <= prof <= 6 else None
        stats, index, prof = {}, 0, 0
        while index < len(args):
            token = args[index].casefold()
            if token in {"prof", "proficiency"}:
                if index + 1 >= len(args) or (value := self._integer(args[index + 1])) is None: return None
                prof, index = value, index + 2
            elif token in STAT_ALIASES and index + 1 < len(args) and (value := self._integer(args[index + 1])) is not None:
                stats[STAT_ALIASES[token]], index = value, index + 2
            else:
                return None
        return (stats, prof) if first_stat in stats and all(0 <= x <= 100 for x in stats.values()) and 0 <= prof <= 6 else None

    def _meets_requirements(self, row: dict[str, Any], stats: dict[str, int]) -> bool:
        return all(stats.get(stat, 0) >= required for stat, required in self._requirements(row.get("Requirements")).items())

    def _damage(self, row: dict[str, Any], stats: dict[str, int], prof: int) -> float | None:
        base = self._number(row.get("Base Damage"))
        if base is None: return None
        damage = base
        for stat, scale in self._scaling(row.get("Scaling")).items():
            damage += 0.00075 * base * scale * stats.get(stat, 0) * (1 + prof * 0.065)
        return damage

    def _dps(self, row: dict[str, Any], stats: dict[str, int], prof: int) -> float | None:
        damage, speed = self._damage(row, stats, prof), self._number(row.get("Swing Speed"))
        if damage is None or speed is None or speed <= 0: return None
        return damage / ((1 / (speed * 2)) + (self._number(row.get("Endlag")) or 0))

    def _rankable(self, row: dict[str, Any]) -> bool:
        if str(row.get("Name") or "").strip() in RANKING_EXCLUSIONS: return False
        if str(row.get("Weapon Class") or "") in {"Special / Other", "Elemental", "Fighting Style"}: return False
        return not any(value > 100 for value in self._requirements(row.get("Requirements")).values())

    @commands.command(name="dwweapon", aliases=["dw", "weapon"])
    async def dwweapon(self, ctx: commands.Context, *args: str):
        """Look up a weapon or rank every weapon usable with a multi-stat build."""
        if not args:
            await ctx.send("Use `[p]dwweapon <name>` or `[p]dwweapon heavy 100 medium 100 prof 6`.")
            return
        parsed = self._parse_stat_query(args)
        if parsed:
            await self._compare(ctx, *parsed)
        elif self._looks_like_stat_query(args):
            await ctx.send("Invalid stat query. Stats must be from 0 to 100 and proficiency must be from 0 to 6.")
        else:
            await self._lookup(ctx, " ".join(args))

    async def _compare(self, ctx: commands.Context, stats: dict[str, int], prof: int):
        ranked = []
        for row in self.weapons:
            if not self._rankable(row): continue
            if not self._scaling(row.get("Scaling")).keys() & stats.keys(): continue
            if not self._meets_requirements(row, stats): continue
            damage, dps = self._damage(row, stats, prof), self._dps(row, stats, prof)
            if damage is not None and dps is not None: ranked.append((dps, damage, row))
        if not ranked:
            await ctx.send("No rankable weapons meet those exact stats.")
            return
        ranked.sort(key=lambda item: (-item[0], str(item[2].get("Name") or "").casefold()))
        stat_text = " | ".join(f"{stat} {value}" for stat, value in sorted(stats.items()))
        pages = []
        for start in range(0, len(ranked), RANKING_PAGE_SIZE):
            lines = []
            for position, (dps, damage, row) in enumerate(ranked[start:start + RANKING_PAGE_SIZE], start + 1):
                label = str(row.get("Weapon Type") or "Unknown")
                if str(row.get("Weapon Class") or "") == "Crazy Slots": label += " | Crazy Slots"
                lines.append(f"`{position:>2}.` **{row['Name']}** ({label})\n`DPS:` {dps:.2f} | `M1:` {damage:.2f}")
            embed = discord.Embed(title=f"Weapon ranking | {stat_text} | Prof {prof}", description="\n".join(lines), colour=discord.Colour.blurple())
            embed.set_footer(text=f"Showing {start + 1}-{min(start + RANKING_PAGE_SIZE, len(ranked))} of {len(ranked)}. Numeric stat requirements are enforced; non-stat unlock alternatives are not modeled. Crazy Slots and qualifying hybrids are included. DPS includes listed Endlag; bleed, crits, procs, enchants, talents, PEN, and resistance are excluded.")
            pages.append(embed)
        if len(pages) == 1:
            await ctx.send(embed=pages[0])
        else:
            await SimpleMenu(pages).start(ctx)

    async def _lookup(self, ctx: commands.Context, query: str):
        query = query.casefold().strip()
        exact = [row for row in self.weapons if str(row.get("Name") or "").casefold() == query]
        matches = exact or [row for row in self.weapons if query in str(row.get("Name") or "").casefold()]
        if not matches:
            await ctx.send("Weapon not found.")
            return
        if len(matches) > 1:
            await ctx.send("Multiple matches: " + ", ".join(str(row["Name"]) for row in matches[:10]))
            return
        row = matches[0]
        embed = discord.Embed(title=str(row.get("Name") or "Unknown weapon"), colour=discord.Colour.blurple())
        embed.add_field(name="Class / Type", value=f"{row.get('Weapon Class') or '?'} / {row.get('Weapon Type') or '?'}", inline=False)
        fields = [("Requirements", "Requirements"), ("Base Damage", "Base Damage"), ("Scaled Damage", "Scaled Damage"), ("Scaling", "Scaling"), ("Armor Penetration", "Armor Penetration"), ("Chip Damage", "Chip Damage"), ("Posture Damage", "Posture Damage"), ("Range", "Range"), ("Swing Speed", "Swing Speed"), ("Endlag", "Endlag"), ("Tags", "Tags")]
        for label, key in fields:
            embed.add_field(name=label, value=str(row.get(key) or "N/A"), inline=label != "Requirements")
        embed.set_footer(text="Example: [p]dwweapon heavy 100 medium 100 prof 6")
        await ctx.send(embed=embed)

    @commands.command(name="dwreload")
    @commands.is_owner()
    async def dwreload(self, ctx: commands.Context):
        self.weapons = self._load_weapons()
        await ctx.send(f"Reloaded {len(self.weapons)} unique weapon rows.")
