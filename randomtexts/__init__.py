import discord
from redbot.core.bot import Red

from .randomchats import RandomText


async def setup(bot: Red) -> None:
    if not hasattr(discord.ui, "LayoutView"):
        raise RuntimeError(
            "RandomTexts needs discord.py 2.6+ for Components V2. Update Red; do not replace its Discord library separately."
        )
    await bot.add_cog(RandomText(bot))
