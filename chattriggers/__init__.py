from .chattriggers import ChatTriggers

async def setup(bot):
    await bot.add_cog(ChatTriggers(bot))
