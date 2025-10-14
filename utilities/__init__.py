from .utilities import Utilities

async def setup(bot):
    await bot.add_cog(Utilities(bot))
