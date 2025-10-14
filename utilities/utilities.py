from redbot.core import commands, Config
import discord
import random
from typing import Optional

GAY_PERCENTAGE_MIN_NORMAL = 0
GAY_PERCENTAGE_MAX_NORMAL = 100
GAY_PERCENTAGE_MIN_HAWK = 51
GAY_PERCENTAGE_MAX_HAWK = 150

THANOS_IMG = "https://cdn.discordapp.com/attachments/1069748983293022249/1425583704532848721/6LpanIV.png"
HAWK_ENABLED_GIF = "https://cdn.discordapp.com/attachments/1069748983293022249/1425831721160540281/NzusuSn.png?ex=68ef9c44&is=68ee4ac4&hm=e97e9983b9d353846965007409b69c50f696589f21fe423e257d6e43e61972cb&"
HAWK_DISABLED_GIF = "https://cdn.discordapp.com/attachments/1069748983293022249/1425831928644501624/4rMETw3.gif?ex=68ef9c76&is=68ee4af6&hm=39b6924ec16d99466f581f6f85427430d72d646729aa82566aa87e2b4ad24b3f&"

class Utilities(commands.Cog):
    """Fun and info commands anyone can use."""

    def __init__(self, bot):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=42424242, force_registration=True)
        default_guild = {
            "hawk_users": [],
            "hawk_enabled": True,
            "gay_enabled": True,
        }
        self.config.register_guild(**default_guild)
        self.awaiting_hawk_response = {}
        self.last_hawk_user = {}

    @commands.command(aliases=["av", "pfp"])
    async def avatar(self, ctx, member: discord.Member = None):
        """Show a user's avatar."""
        member = member or ctx.author
        await ctx.send(member.display_avatar.url)
    
    @commands.command(aliases=["ui", "whois"])
    async def userinfo(self, ctx, member: discord.Member = None):
        """Show info about a user."""
        member = member or ctx.author
        embed = discord.Embed(title=f"User info for {member}", color=member.color)
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.add_field(name="ID", value=member.id)
        embed.add_field(name="Joined", value=member.joined_at)
        embed.add_field(name="Account Created", value=member.created_at)
        await ctx.send(embed=embed)

    @commands.command(aliases=["si", "guildinfo"])
    async def serverinfo(self, ctx):
        """Show info about the server."""
        guild = ctx.guild
        embed = discord.Embed(title=guild.name, color=0x5865F2)
        embed.add_field(name="ID", value=guild.id)
        embed.add_field(name="Members", value=guild.member_count)
        embed.add_field(name="Owner", value=str(guild.owner))
        embed.set_thumbnail(url=guild.icon.url if guild.icon else None)
        await ctx.send(embed=embed)

    @commands.command(aliases=["latency", "botping"])
    async def status(self, ctx):
        """Show the bot's latency."""
        await ctx.send(f"Pong! `{round(ctx.bot.latency * 1000)}ms`")

    @commands.command(aliases=["8ball"])
    async def eightball(self, ctx, *, question: str):
        """Magic 8-ball."""
        responses = [
            "It is certain", "Without a doubt", "Yes", "No", "Maybe", "Ask again later",
            "My reply is no", "My sources say no", "Outlook good", "Very doubtful"
        ]
        await ctx.send(f":8ball: {random.choice(responses)}")

    @commands.command()
    async def poll(self, ctx, *, question: str):
        """Create a yes/no poll."""
        msg = await ctx.send(f"**{question}**\n:thumbsup: = Yes\n:thumbsdown: = No")
        await msg.add_reaction("👍")
        await msg.add_reaction("👎")

    @commands.command()
    async def choose(self, ctx, *choices):
        """Randomly choose from options."""
        if not choices or len(choices) < 2:
            await ctx.send("You must provide at least 2 options")
            return
        choice = random.choice(choices)
        await ctx.send(f"I choose: **{choice}**")

    @commands.command()
    async def coinflip(self, ctx):
        """Flip a coin!"""
        await ctx.send(f"Result: {random.choice(['Heads', 'Tails'])}")

    @commands.command()
    async def dice(self, ctx, sides: int = 6):
        """Roll a dice!"""
        if not 2 <= sides <= 100:
            await ctx.send("Number of sides must be between 2 and 100.")
            return
        await ctx.send(f"Rolled: {random.randint(1, sides)} (d{sides})")

    @commands.command()
    @commands.guild_only()
    async def hawk(self, ctx, user: Optional[discord.Member] = None):
        """Ask a user if they're a hawk."""
        if not await self.config.guild(ctx.guild).hawk_enabled():
            embed = discord.Embed(color=0xED4245)
            embed.set_image(url=HAWK_DISABLED_GIF)
            embed.description = "The hawk command is currently disabled."
            await ctx.send(embed=embed)
            return

        hawk_users = await self.config.guild(ctx.guild).hawk_users()
        if user is None:
            if not hawk_users:
                await ctx.send("There are no hawk users set!")
                return
            available = hawk_users.copy()
            if len(available) > 1 and ctx.guild.id in self.last_hawk_user:
                last_id = self.last_hawk_user[ctx.guild.id]
                if last_id in available:
                    available.remove(last_id)
            random_user_id = random.choice(available)
            user = ctx.guild.get_member(random_user_id)
            if not user:
                await ctx.send(f"User ID `{random_user_id}` is not in this server!")
                return
            self.last_hawk_user[ctx.guild.id] = random_user_id

        self.awaiting_hawk_response[ctx.guild.id] = user.id
        await ctx.send(
            f"{user.mention} Are you a hawk?",
            allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False)
        )

    @commands.command()
    @commands.guild_only()
    async def gay(self, ctx, user: Optional[discord.Member] = None):
        """How gay is this user?"""
        if not await self.config.guild(ctx.guild).gay_enabled():
            embed = discord.Embed(color=0xED4245)
            embed.set_image(url=HAWK_DISABLED_GIF)
            embed.description = "The gay command is currently disabled."
            await ctx.send(embed=embed)
            return
        if user is None:
            await ctx.send("Please mention a user.")
            return
        hawk_users = await self.config.guild(ctx.guild).hawk_users()
        if user.id in hawk_users:
            pct = random.randint(GAY_PERCENTAGE_MIN_HAWK, GAY_PERCENTAGE_MAX_HAWK)
        else:
            pct = random.randint(GAY_PERCENTAGE_MIN_NORMAL, GAY_PERCENTAGE_MAX_NORMAL)
        await ctx.send(f"{user.mention} is {pct}% gay! 🏳️‍🌈")

    @commands.command()
    async def thanos(self, ctx):
        """Show Thanos meme."""
        embed = discord.Embed(color=0x800080)
        embed.set_image(url=THANOS_IMG)
        await ctx.send(embed=embed)

    @commands.command()
    @commands.is_owner()
    @commands.guild_only()
    async def addhawk(self, ctx, user: discord.Member):
        """Add a user to the hawk list."""
        async with self.config.guild(ctx.guild).hawk_users() as hawk_users:
            if user.id not in hawk_users:
                hawk_users.append(user.id)
                await ctx.send(f"Added {user.mention} to the hawk list.")
            else:
                await ctx.send("Already on the hawk list.")

    @commands.command()
    @commands.is_owner()
    @commands.guild_only()
    async def removehawk(self, ctx, user: discord.Member):
        """Remove a user from the hawk list."""
        async with self.config.guild(ctx.guild).hawk_users() as hawk_users:
            if user.id in hawk_users:
                hawk_users.remove(user.id)
                await ctx.send(f"Removed {user.mention} from the hawk list.")
            else:
                await ctx.send("Not found in the hawk list.")

    @commands.command()
    @commands.is_owner()
    @commands.guild_only()
    async def listhawk(self, ctx):
        """List all hawk users."""
        hawk_users = await self.config.guild(ctx.guild).hawk_users()
        if not hawk_users:
            await ctx.send("The hawk list is empty.")
            return
        msg = []
        for uid in hawk_users:
            member = ctx.guild.get_member(uid)
            if member:
                msg.append(f"{member.mention} (`{uid}`)")
            else:
                msg.append(f"`{uid}` (not in server)")
        await ctx.send("Hawk list:\n" + "\n".join(msg))

    @commands.command()
    @commands.is_owner()
    @commands.guild_only()
    async def disablehawk(self, ctx):
        """Toggle the hawk command on/off."""
        enabled = await self.config.guild(ctx.guild).hawk_enabled()
        await self.config.guild(ctx.guild).hawk_enabled.set(not enabled)
        embed = discord.Embed(color=0x57F287 if not enabled else 0xED4245)
        gif = HAWK_ENABLED_GIF if not enabled else HAWK_DISABLED_GIF
        embed.set_image(url=gif)
        embed.description = f"Hawk command is now {'enabled' if not enabled else 'disabled'}."
        await ctx.send(embed=embed)

    @commands.command()
    @commands.is_owner()
    @commands.guild_only()
    async def disablegay(self, ctx):
        """Toggle the gay command on/off."""
        enabled = await self.config.guild(ctx.guild).gay_enabled()
        await self.config.guild(ctx.guild).gay_enabled.set(not enabled)
        embed = discord.Embed(color=0x57F287 if not enabled else 0xED4245)
        gif = HAWK_ENABLED_GIF if not enabled else HAWK_DISABLED_GIF
        embed.set_image(url=gif)
        embed.description = f"Gay command is now {'enabled' if not enabled else 'disabled'}."
        await ctx.send(embed=embed)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if (
            not message.guild
            or message.author.bot
            or message.guild.id not in self.awaiting_hawk_response
        ):
            return
        user_id = self.awaiting_hawk_response[message.guild.id]
        if message.author.id != user_id:
            return
        content = message.content.lower()
        yes_words = ["yes", "yea", "ye"]
        no_words = ["no", "nah", "nuh"]
        if any(word in content for word in yes_words):
            reply = "I'm a hawk too"
            await message.channel.send(reply)
            del self.awaiting_hawk_response[message.guild.id]
        elif any(word in content for word in no_words):
            reply = "Fuck you then"
            await message.channel.send(reply)
            del self.awaiting_hawk_response[message.guild.id]

async def setup(bot):
    await bot.add_cog(Utilities(bot))
