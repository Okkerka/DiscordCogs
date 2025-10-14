from .tidalplayer import TidalPlayer

async def setup(bot):
    await bot.add_cog(TidalPlayer(bot))
