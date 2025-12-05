from redbot.core.bot import Red

from .randomtext import RandomText


async def setup(bot: Red) -> None:
    await bot.add_cog(RandomText(bot))
