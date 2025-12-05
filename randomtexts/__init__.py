from .randomchats import RandomText


async def setup(bot):
    await bot.add_cog(RandomText(bot))
