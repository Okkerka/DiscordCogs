from .msgdelete import MessageDelete

async def setup(bot):
    await bot.add_cog(MessageDelete(bot))