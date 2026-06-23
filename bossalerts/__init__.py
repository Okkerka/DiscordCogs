from .bossalerts import BossAlerts

async def setup(bot):
    await bot.add_cog(BossAlerts(bot))
