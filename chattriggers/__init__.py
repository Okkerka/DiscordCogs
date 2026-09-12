import discord
from redbot.core.bot import Red

from .chattriggers import ChatTriggers


async def setup(bot: Red) -> None:
    if not hasattr(discord.ui, "LayoutView"):
        raise RuntimeError(
            "ChatTriggers needs discord.py 2.6+ for Components V2. Update Red; do not replace its Discord library separately."
        )
    await bot.add_cog(ChatTriggers(bot))
