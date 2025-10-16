from redbot.core import commands, Config
import discord
import random
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
        default_guild = {
            "hawk_users": [],
            "hawk_enabled": True,
            "gay_enabled": True,
        }
        self.config.register_guild(**default_guild)
        self.awaiting_hawk_response = {}
        self.last_hawk_user = {}

    @commands.command(aliases=["av", "pfp"])
    async def avatar(self, ctx, member: Optional[discord.Member] = None):
        member = member or ctx.author
        await ctx.send(member.display_avatar.url)

    @commands.command(aliases=["ui", "whois"])
    async def userinfo(self, ctx, member: Optional[discord.Member] = None):
        member = member or ctx.author
        lines = [
            f"__{member}__",
            f"ID: `{member.id}`",
            f"Account Created: {discord.utils.format_dt(member.created_at, 'f')}",
            f"Joined Server: {discord.utils.format_dt(member.joined_at, 'f')}",
            f"Top Role: {member.top_role.mention}",
        ]
        roles = [r.mention for r in member.roles if r != ctx.guild.default_role]
        if roles:
            lines.append(f"Roles: {', '.join(roles)}")
        await ctx.send("\n".join(lines))

    @commands.command(aliases=["si", "guildinfo"])
    async def serverinfo(self, ctx):
        guild = ctx.guild
        lines = [
            f"**{guild.name}** (ID: `{guild.id}`)",
            f"Owner: {guild.owner.display_name}",
            f"Created: {guild.created_at.strftime('%Y-%m-%d %H:%M')}",
            f"Members: {guild.member_count}",
            f"Channels: {len(guild.channels)}",
        ]
        await ctx.send("\n".join(lines))

    @commands.command(aliases=["latency", "botping"])
    async def status(self, ctx):
        await ctx.send(f"Bot latency: `{round(ctx.bot.latency * 1000)} ms`")

    @commands.command(aliases=["8ball"])
    async def eightball(self, ctx, *, question: str):
        responses = [
            "It is certain", "Without a doubt", "Yes", "No", "Maybe", "Ask again later",
            "My reply is no", "My sources say no", "Outlook good", "Very doubtful"
        ]
        await ctx.send(f"🎱 {random.choice(responses)}")

    @commands.command()
    async def poll(self, ctx, *, question: str):
        msg = await ctx.send(f"**{question}**\n👍 = Yes\n👎 = No")
        await msg.add_reaction("👍")
        await msg.add_reaction("👎")

    @commands.command()
    async def choose(self, ctx, *choices):
        if len(choices) < 2:
            await ctx.send("You must provide at least 2 options")
            return
        await ctx.send(f"I choose: **{random.choice(choices)}**")

    @commands.command()
    async def coinflip(self, ctx):
        await ctx.send(f"Result: **{random.choice(['Heads', 'Tails'])}**")

    @commands.command()
    async def dice(self, ctx, sides: int = 6):
        if not 2 <= sides <= 100:
            await ctx.send("Number of sides must be between 2 and 100.")
            return
        await ctx.send(f"Rolled: **{random.randint(1, sides)}** (d{sides})")

    @commands.command()
    @commands.guild_only()
    async def hawk(self, ctx, user: Optional[discord.Member] = None):
        if not await self.config.guild(ctx.guild).hawk_enabled():
            embed = discord.Embed(
                title="🦅 Hawk",
                description="The hawk command is currently **disabled**.",
                color=ERROR
            )
            embed.set_image(url=HAWK_DISABLED_GIF)
            await ctx.send(embed=embed)
            return

        hawk_users = set(await self.config.guild(ctx.guild).hawk_users())
        if user is None:
            eligible = [u for u in hawk_users if ctx.guild.get_member(u)]
            if not eligible:
                await ctx.send("There are no hawk users set!")
                return
            last_id = self.last_hawk_user.get(ctx.guild.id, None)
            selectable = [u for u in eligible if u != last_id] if len(eligible) > 1 else eligible
            selected_id = random.choice(selectable)
            user = ctx.guild.get_member(selected_id)
            self.last_hawk_user[ctx.guild.id] = selected_id

        if not user or user.id not in hawk_users:
            await ctx.send("Target user is not in the hawk list or not found in server.")
            return

        self.awaiting_hawk_response[ctx.guild.id] = user.id
        await ctx.send(
            f"{user.mention} Are you a hawk?",
            allowed_mentions=discord.AllowedMentions(users=True)
        )

    @commands.command()
    @commands.guild_only()
    async def gay(self, ctx, user: Optional[discord.Member] = None):
        if not await self.config.guild(ctx.guild).gay_enabled():
            embed = discord.Embed(
                title="Gay Percentage",
                description="The gay command is currently **disabled**.",
                color=ERROR
            )
            embed.set_image(url=HAWK_DISABLED_GIF)
            await ctx.send(embed=embed)
            return
        if user is None:
            await ctx.send("Please mention a user.")
            return
        hawk_users = set(await self.config.guild(ctx.guild).hawk_users())
        message_content = ctx.message.content
        is_owner = await ctx.bot.is_owner(ctx.author)
        if user.id in hawk_users and skibiditoilet in message_content and is_owner:
            pct = 9999
        elif user.id in hawk_users:
            pct = 9999 if random.randrange(100) == 0 else random.randint(GAY_PERCENTAGE_MIN_HAWK, GAY_PERCENTAGE_MAX_HAWK)
        else:
            pct = random.randint(GAY_PERCENTAGE_MIN_NORMAL, GAY_PERCENTAGE_MAX_NORMAL)
        embed = discord.Embed(
            title="Gay Percentage",
            description=f"{user.display_name} is **{pct}% gay!** 🏳️‍🌈",
            color=BASE
        )
        await ctx.send(embed=embed)

    @commands.command()
    async def thanos(self, ctx):
        embed = discord.Embed(
            title="Thanos Meme",
            color=BASE
        )
        embed.set_image(url=THANOS_IMG)
        await ctx.send(embed=embed)

    @commands.command()
    @commands.is_owner()
    @commands.guild_only()
    async def addhawk(self, ctx, *users: discord.Member):
        if not users:
            await ctx.send("Please specify one or more users.")
            return
        config = self.config.guild(ctx.guild)
        hawk_users = set(await config.hawk_users())
        added = []
        already = []
        for user in users:
            if user.id not in hawk_users:
                hawk_users.add(user.id)
                added.append(user.display_name)
            else:
                already.append(user.display_name)
        await config.hawk_users.set(list(hawk_users))
        msg = ""
        if added:
            msg += f"🦅 Added: {', '.join(f'**{name}**' for name in added)}"
        if already:
            msg += f"\nAlready in hawk list: {', '.join(already)}"
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
            await ctx.send(f"🦅 Removed **{user.display_name}** from the hawk list.")
        else:
            await ctx.send("Not found in the hawk list.")

    @commands.command()
    @commands.is_owner()
    @commands.guild_only()
    async def listhawk(self, ctx):
        hawk_users = set(await self.config.guild(ctx.guild).hawk_users())
        total = len(hawk_users)
        if not hawk_users:
            embed = discord.Embed(
                title="🦅 Hawk List",
                description="The hawk list is empty.",
                color=ERROR
            )
            embed.set_footer(text="Total: 0")
            await ctx.send(embed=embed)
            return
        lines = []
        for uid in hawk_users:
            member = ctx.guild.get_member(uid)
            if member:
                lines.append(f"**{member.display_name}** (`{uid}`)")
            else:
                lines.append(f"`{uid}` (not in server)")
        embed = discord.Embed(
            title="🦅 Hawk List",
            description="\n".join(lines),
            color=BASE
        )
        embed.set_footer(text=f"Total: {total}")
        await ctx.send(embed=embed)

    @commands.command()
    @commands.is_owner()
    @commands.guild_only()
    async def clearhawks(self, ctx):
        config = self.config.guild(ctx.guild)
        hawk_users = set(await config.hawk_users())
        removed = [uid for uid in hawk_users if not ctx.guild.get_member(uid)]
        kept = [uid for uid in hawk_users if ctx.guild.get_member(uid)]
        await config.hawk_users.set(kept)
        embed = discord.Embed(
            title="🦅 Hawk List Cleanup",
            description=f"Removed {len(removed)} users who left the server.",
            color=BASE
        )
        embed.add_field(name="Removed User IDs", value=", ".join(str(u) for u in removed) if removed else "None", inline=False)
        await ctx.send(embed=embed)

    @commands.command()
    @commands.is_owner()
    @commands.guild_only()
    async def disablehawk(self, ctx):
        config = self.config.guild(ctx.guild)
        enabled = await config.hawk_enabled()
        await config.hawk_enabled.set(not enabled)
        state = "enabled" if not enabled else "disabled"
        embed = discord.Embed(
            title="🦅 Hawk Command Toggled",
            description=f"Hawk command is now **{state}**.",
            color=BASE
        )
        gif = HAWK_ENABLED_GIF if not enabled else HAWK_DISABLED_GIF
        embed.set_image(url=gif)
        await ctx.send(embed=embed)

    @commands.command()
    @commands.is_owner()
    @commands.guild_only()
    async def disablegay(self, ctx):
        config = self.config.guild(ctx.guild)
        enabled = await config.gay_enabled()
        await config.gay_enabled.set(not enabled)
        state = "enabled" if not enabled else "disabled"
        embed = discord.Embed(
            title="Gay Command Toggled",
            description=f"Gay command is now **{state}**.",
            color=BASE
        )
        gif = HAWK_ENABLED_GIF if not enabled else HAWK_DISABLED_GIF
        embed.set_image(url=gif)
        await ctx.send(embed=embed)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if (
            not message.guild or
            message.author.bot or
            message.guild.id not in self.awaiting_hawk_response
        ):
            return
        user_id = self.awaiting_hawk_response[message.guild.id]
        if message.author.id != user_id:
            return
        content = message.content.lower()
        yes_words = {"yes", "yea", "ye", "yuh"}
        no_words = {"no", "nah", "nuh", "naw"}
        if any(word in content for word in yes_words):
            await message.channel.send("I'm a hawk too")
            del self.awaiting_hawk_response[message.guild.id]
        elif any(word in content for word in no_words):
            await message.channel.send("Fuck you then")
            del self.awaiting_hawk_response[message.guild.id]

async def setup(bot):
    await bot.add_cog(Utilities(bot))
