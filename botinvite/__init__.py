from .botinvite import BotInvite


async def setup(bot):
    await bot.add_cog(BotInvite(bot))
