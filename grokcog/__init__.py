from .grokcog import GrokCog


async def setup(bot):
    await bot.add_cog(GrokCog(bot))
