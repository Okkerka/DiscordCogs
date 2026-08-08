from .deepwoken import Deepwoken


async def setup(bot):
    await bot.add_cog(Deepwoken(bot))