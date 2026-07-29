from .inviteperms import InvitePerms


async def setup(bot):
    await bot.add_cog(InvitePerms(bot))
