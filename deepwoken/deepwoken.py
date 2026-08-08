from __future__ import annotations

from pathlib import Path

import discord
from openpyxl import load_workbook
from redbot.core import commands


class Deepwoken(commands.Cog):
    """Deepwoken weapon base-stat lookup."""

    def __init__(self, bot):
        self.bot = bot
        self.workbook_path = Path(__file__).parent / "data" / "weapons.xlsx"
        self.weapons = self._load_weapons()

    def _load_weapons(self) -> dict[str, dict[str, str]]:
        if not self.workbook_path.exists():
            raise FileNotFoundError(f"Missing bundled workbook: {self.workbook_path}")

        workbook = load_workbook(self.workbook_path, read_only=True, data_only=True)
        sheet = workbook["All Weapons"]
        headers = [cell.value for cell in next(sheet.iter_rows(min_row=1, max_row=1))]
        weapons = {}

        for values in sheet.iter_rows(min_row=2, values_only=True):
            row = dict(zip(headers, values))
            name = str(row.get("Name") or "").strip()
            if name:
                weapons[name.casefold()] = row

        workbook.close()
        return weapons

    @commands.command(name="weapon")
    async def weapon(self, ctx: commands.Context, *, name: str):
        """Show the base data of a Deepwoken weapon."""
        query = name.casefold().strip()
        row = self.weapons.get(query)

        if row is None:
            matches = [weapon for weapon in self.weapons if query in weapon]
            if len(matches) == 1:
                row = self.weapons[matches[0]]
            elif matches:
                suggestions = ", ".join(self.weapons[match]["Name"] for match in matches[:8])
                await ctx.send(f"Multiple matches: {suggestions}")
                return
            else:
                await ctx.send("Weapon not found.")
                return

        embed = discord.Embed(
            title=row["Name"],
            colour=discord.Colour.blurple(),
        )
        embed.add_field(name="Class / Type", value=f'{row.get("Weapon Class", "?")} / {row.get("Weapon Type", "?")}', inline=False)
        embed.add_field(name="Requirements", value=str(row.get("Requirements") or "N/A"), inline=False)
        embed.add_field(name="Base Damage", value=str(row.get("Base Damage") or "N/A"))
        embed.add_field(name="Scaled Damage",value=str(row.get("Scaled Damage") or "N/A"))
        embed.add_field(name="Scaling", value=str(row.get("Scaling") or "N/A"))
        embed.add_field(name="Swing Speed", value=str(row.get("Swing Speed") or "N/A"))
        embed.add_field(name="Range", value=str(row.get("Range") or "N/A"))
        embed.add_field(name="Armor Penetration", value=str(row.get("Armor Penetration") or "N/A"))
        embed.add_field(name="Tags", value=str(row.get("Tags") or "None"), inline=False)
        await ctx.send(embed=embed)

    @commands.command(name="reloadweapons")
    @commands.is_owner()
    async def reloadweapons(self, ctx: commands.Context):
        """Reload the bundled workbook after updating it and restarting/reinstalling the cog."""
        self.weapons = self._load_weapons()
        await ctx.send(f"Reloaded {len(self.weapons)} weapons.")
