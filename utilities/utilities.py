from redbot.core import commands, Config
import discord
import random
import asyncio
from typing import Optional

GAY_PERCENTAGE_MIN_NORMAL = 0
GAY_PERCENTAGE_MAX_NORMAL = 100
GAY_PERCENTAGE_MIN_HAWK = 51
GAY_PERCENTAGE_MAX_HAWK = 150

skibiditoilet = "\u200b"

THANOS_IMG = "https://cdn.discordapp.com/attachments/1069748983293022249/1425583704532848721/6LpanIV.png"
HAWK_ENABLED_GIF = "https://cdn.discordapp.com/attachments/1069748983293022249/1425831721160540281/NzusuSn.png?ex=68ef9c44&is=68ee4ac4&hm=e97e9983b9d353846965007409b69c50f696589f21fe423e257d6e43e61972cb&"
HAWK_DISABLED_GIF = "https://cdn.discordapp.com/attachments/1069748983293022249/1425831928644501624/4rMETw3.gif?ex=68ef9c76&is=68ee4af6&hm=39b6924ec16d99466f581f6f85427430d72d646729aa82566aa87e2b4ad24b3f&"

BASE = discord.Color.purple()
ERROR = discord.Color.red()

class Utilities(commands.Cog):
    """Fun, info, and meme commands for everyone."""

    def __init__(self, bot):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=987654321, force_registration=True)
        self.config.register_guild(hawk_users=[], hawk_enabled=True, gay_enabled=True)
        self.awaiting_hawk_response = {}
        self.last_hawk_user = {}
        self.timed_pings = {}

    @commands.command(aliases=["av", "pfp"])
    async def avatar(self, ctx, member: Optional[discord.Member] = None):
        await ctx.send((member or ctx.author).display_avatar.url)

    @commands.command(aliases=["ui", "whois"])
    async def userinfo(self, ctx, member: Optional[discord.Member] = None):
        member = member or ctx.author
        roles = [r.mention for r in member.roles if r != ctx.guild.default_role]
        lines = [f"__{member}__", f"ID: `{member.id}`",
                 f"Account Created: {discord.utils.format_dt(member.created_at, 'f')}",
                 f"Joined Server: {discord.utils.format_dt(member.joined_at, 'f')}",
                 f"Top Role: {member.top_role.mention}"]
        if roles:
            lines.append(f"Roles: {', '.join(roles)}")
        await ctx.send("\n".join(lines))

    @commands.command(aliases=["si", "guildinfo"])
    async def serverinfo(self, ctx):
        g = ctx.guild
        await ctx.send(f"**{g.name}** (ID: `{g.id}`)\nOwner: {g.owner.display_name}\n"
                      f"Created: {g.created_at.strftime('%Y-%m-%d %H:%M')}\nMembers: {g.member_count}\nChannels: {len(g.channels)}")

    @commands.command(aliases=["latency", "botping"])
    async def status(self, ctx):
        await ctx.send(f"Bot latency: `{round(ctx.bot.latency * 1000)} ms`")

    @commands.command(aliases=["8ball"])
    async def eightball(self, ctx, *, question: str):
        await ctx.send(random.choice(['It is certain', 'Without a doubt', 'Yes', 'No', 'Maybe', 'Ask again later', 'My reply is no', 'My sources say no', 'Outlook good', 'Very doubtful']))

    @commands.command()
    async def poll(self, ctx, *, question: str):
        msg = await ctx.send(f"**{question}**\nYes = 👍\nNo = 👎")
        await msg.add_reaction("👍")
        await msg.add_reaction("👎")

    @commands.command()
    async def choose(self, ctx, *choices):
        if len(choices) < 2:
            return await ctx.send("You must provide at least 2 options")
        await ctx.send(f"I choose: **{random.choice(choices)}**")

    @commands.command()
    async def coinflip(self, ctx):
        await ctx.send(f"Result: **{random.choice(['Heads', 'Tails'])}**")

    @commands.command()
    async def dice(self, ctx, sides: int = 6):
        if not 2 <= sides <= 100:
            return await ctx.send("Number of sides must be between 2 and 100.")
        await ctx.send(f"Rolled: **{random.randint(1, sides)}** (d{sides})")

    @commands.command()
    @commands.guild_only()
    async def hawk(self, ctx, user: Optional[discord.Member] = None):
        if not await self.config.guild(ctx.guild).hawk_enabled():
            embed = discord.Embed(title="🐦 Hawk", description="The hawk command is currently **disabled**.", color=ERROR)
            embed.set_image(url=HAWK_DISABLED_GIF)
            return await ctx.send(embed=embed)
        hawk_users = set(await self.config.guild(ctx.guild).hawk_users())
        if user is None:
            eligible = [u for u in hawk_users if ctx.guild.get_member(u)]
            if not eligible:
                return await ctx.send("There are no hawk users set!")
            last_id = self.last_hawk_user.get(ctx.guild.id)
            selectable = [u for u in eligible if u != last_id] if len(eligible) > 1 else eligible
            selected_id = random.choice(selectable)
            user = ctx.guild.get_member(selected_id)
            self.last_hawk_user[ctx.guild.id] = selected_id
        if not user or user.id not in hawk_users:
            return await ctx.send("Target user is not in the hawk list or not found in server.")
        self.awaiting_hawk_response[ctx.guild.id] = user.id
        await ctx.send(f"{user.mention} Are you a hawk?", allowed_mentions=discord.AllowedMentions(users=True))

    @commands.command()
    @commands.guild_only()
    async def gay(self, ctx, user: Optional[discord.Member] = None):
        if not await self.config.guild(ctx.guild).gay_enabled():
            embed = discord.Embed(title="Gay Percentage", description="The gay command is currently **disabled**.", color=ERROR)
            embed.set_image(url=HAWK_DISABLED_GIF)
            return await ctx.send(embed=embed)
        if user is None:
            return await ctx.send("Please mention a user.")
        hawk_users = set(await self.config.guild(ctx.guild).hawk_users())
        is_owner = await ctx.bot.is_owner(ctx.author)
        if user.id in hawk_users and skibiditoilet in ctx.message.content and is_owner:
            pct = 9999
        elif user.id in hawk_users:
            pct = 9999 if random.randrange(100) == 0 else random.randint(GAY_PERCENTAGE_MIN_HAWK, GAY_PERCENTAGE_MAX_HAWK)
        else:
            pct = random.randint(GAY_PERCENTAGE_MIN_NORMAL, GAY_PERCENTAGE_MAX_NORMAL)
        await ctx.send(embed=discord.Embed(title="Gay Percentage", description=f"{user.display_name} is **{pct}% gay**", color=BASE))

    @commands.command()
    async def thanos(self, ctx):
        await ctx.send(embed=discord.Embed(title="Thanos Meme", color=BASE).set_image(url=THANOS_IMG))

    @commands.command()
    @commands.is_owner()
    @commands.guild_only()
    async def addhawk(self, ctx, *users: discord.Member):
        if not users:
            return await ctx.send("Please specify one or more users.")
        config = self.config.guild(ctx.guild)
        hawk_users = set(await config.hawk_users())
        added, already = [], []
        for user in users:
            (hawk_users.add(user.id) or added.append(user.display_name)) if user.id not in hawk_users else already.append(user.display_name)
        await config.hawk_users.set(list(hawk_users))
        msg = (f"🐦 Added: {', '.join(f'**{n}**' for n in added)}" if added else "") + (f"\nAlready in hawk list: {', '.join(already)}" if already else "")
        await ctx.send(msg or "No users added.")

    @commands.command()
    @commands.is_owner()
    @commands.guild_only()
    async def removehawk(self, ctx, user: discord.Member):
        config = self.config.guild(ctx.guild)
        hawk_users = set(await config.hawk_users())
        if user.id in hawk_users:
            hawk_users.remove(user.id)
            await config.hawk_users.set(list(hawk_users))
            await ctx.send(f"🐦 Removed **{user.display_name}** from the hawk list.")
        else:
            await ctx.send("Not found in the hawk list.")

    @commands.command()
    @commands.is_owner()
    @commands.guild_only()
    async def listhawk(self, ctx):
        hawk_users = set(await self.config.guild(ctx.guild).hawk_users())
        if not hawk_users:
            return await ctx.send(embed=discord.Embed(title="🐦 Hawk List", description="The hawk list is empty.", color=ERROR).set_footer(text="Total: 0"))
        lines = [f"**{m.display_name}** (`{uid}`)" if (m := ctx.guild.get_member(uid)) else f"`{uid}` (not in server)" for uid in hawk_users]
        await ctx.send(embed=discord.Embed(title="🐦 Hawk List", description="\n".join(lines), color=BASE).set_footer(text=f"Total: {len(hawk_users)}"))

    @commands.command()
    @commands.is_owner()
    @commands.guild_only()
    async def clearhawks(self, ctx):
        config = self.config.guild(ctx.guild)
        hawk_users = set(await config.hawk_users())
        removed = [uid for uid in hawk_users if not ctx.guild.get_member(uid)]
        await config.hawk_users.set([uid for uid in hawk_users if ctx.guild.get_member(uid)])
        embed = discord.Embed(title="🐦 Hawk List Cleanup", description=f"Removed {len(removed)} users who left the server.", color=BASE)
        embed.add_field(name="Removed User IDs", value=", ".join(str(u) for u in removed) if removed else "None")
        await ctx.send(embed=embed)

    @commands.command()
    @commands.is_owner()
    @commands.guild_only()
    async def disablehawk(self, ctx):
        config = self.config.guild(ctx.guild)
        enabled = await config.hawk_enabled()
        await config.hawk_enabled.set(not enabled)
        state = "enabled" if not enabled else "disabled"
        await ctx.send(embed=discord.Embed(title="🐦 Hawk Command Toggled", description=f"Hawk command is now **{state}**.", color=BASE))

    @commands.command()
    @commands.is_owner()
    @commands.guild_only()
    async def disablegay(self, ctx):
        config = self.config.guild(ctx.guild)
        enabled = await config.gay_enabled()
        await config.gay_enabled.set(not enabled)
        state = "enabled" if not enabled else "disabled"
        await ctx.send(embed=discord.Embed(title="Gay Command Toggled", description=f"Gay command is now **{state}**.", color=BASE))

    @commands.command()
    @commands.is_owner()
    @commands.guild_only()
    async def timedping(self, ctx, user: discord.Member, seconds: int = 0, minutes: int = 0):
        total = seconds + minutes * 60
        if total < 1 or total > 21600:
            return await ctx.send("Please specify a total wait time between 1 second and 6 hours (21600 seconds).")
        key = (ctx.guild.id, user.id)
        if key in self.timed_pings:
            return await ctx.send(f"{user.mention} already has an active timed ping. Use `!stoptimedping @user` first.")
        self.timed_pings[key] = asyncio.create_task(self._repeating_ping(ctx.channel, user, total))
        await ctx.send(f"Repeating timer set! Will ping {user.mention} every {total} seconds ({total // 60} min {total % 60} sec).")

    async def _repeating_ping(self, channel, user, interval):
        try:
            while True:
                await asyncio.sleep(interval)
                await channel.send(f"{user.mention} Repeating ping!", allowed_mentions=discord.AllowedMentions(users=True))
        except asyncio.CancelledError:
            pass

    @commands.command()
    @commands.is_owner()
    @commands.guild_only()
    async def stoptimedping(self, ctx, user: discord.Member):
        key = (ctx.guild.id, user.id)
        if key not in self.timed_pings:
            return await ctx.send(f"No active timed ping found for {user.mention}.")
        self.timed_pings[key].cancel()
        del self.timed_pings[key]
        await ctx.send(f"Stopped repeating ping for {user.mention}.")

    @commands.command()
    @commands.is_owner()
    @commands.guild_only()
    async def listtimedpings(self, ctx):
        active = [(uid, task) for (gid, uid), task in self.timed_pings.items() if gid == ctx.guild.id]
        if not active:
            return await ctx.send("No active timed pings in this server.")
        lines = [f"• {m.mention} (`{uid}`)" if (m := ctx.guild.get_member(uid)) else f"• Unknown user (`{uid}`)" for uid, _ in active]
        await ctx.send("**Active Timed Pings:**\n" + "\n".join(lines))

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if not message.guild or message.author.bot or message.guild.id not in self.awaiting_hawk_response:
            return
        if message.author.id != self.awaiting_hawk_response[message.guild.id]:
            return
        content = message.content.lower()
        if any(w in content for w in {"yes", "yea", "ye", "yuh"}):
            await message.channel.send("I'm a hawk too")
            del self.awaiting_hawk_response[message.guild.id]
        elif any(w in content for w in {"no", "nah", "nuh", "naw"}):
            await message.channel.send("Fuck you then")
            del self.awaiting_hawk_response[message.guild.id]

async def setup(bot):
    await bot.add_cog(Utilities(bot))
