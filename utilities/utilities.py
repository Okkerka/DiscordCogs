from redbot.core import commands, Config
import discord
import random
import asyncio
from typing import Optional
import re
import logging
import shlex
import uuid
from datetime import timedelta

from .helpers import parse_timestamp, parse_message_link
from .ui import ReminderView

log = logging.getLogger("red.utilities")

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

DURATION_RE = re.compile(r"(?i)\s*(\d+)\s*([dhms]?)")

def parse_duration_to_seconds(s: str) -> int:
    if not s or not isinstance(s, str):
        raise ValueError("Empty duration")
    s = s.strip()
    total = 0
    idx = 0
    for m in DURATION_RE.finditer(s):
        if m.start() != idx:
            raise ValueError("Invalid duration format")
        num = int(m.group(1))
        unit = (m.group(2) or "s").lower()
        if unit == "d":
            total += num * 86400
        elif unit == "h":
            total += num * 3600
        elif unit == "m":
            total += num * 60
        elif unit == "s":
            total += num
        else:
            raise ValueError("Invalid unit")
        idx = m.end()
    if idx != len(s):
        raise ValueError("Invalid trailing characters")
    if total <= 0:
        raise ValueError("Zero duration")
    return total

class Utilities(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=987654321, force_registration=True)
        self.config.register_guild(hawk_users=[], hawk_enabled=True, gay_enabled=True)
        self.awaiting_hawk_response = {}
        self.last_hawk_user = {}
        self.timed_pings = {}
        self.config.register_user(reminders={})
        self._reminder_lock = asyncio.Lock()
        self._reminder_task = asyncio.create_task(self._reminder_loop())

    async def cog_before_invoke(self, ctx: commands.Context) -> None:
        if ctx.interaction and not ctx.interaction.response.is_done():
            await ctx.defer(ephemeral=ctx.command.qualified_name.split()[0] in {"remindme", "reminders", "quote"})

    def cog_unload(self):
        self._reminder_task.cancel()
        for task in self.timed_pings.values():
            task.cancel()

    @commands.hybrid_command(aliases=["av", "pfp"])
    async def avatar(self, ctx, member: Optional[discord.Member] = None):
        member = member or ctx.author
        
        avatar_url = member.display_avatar.url
        embed = discord.Embed(
            title=f"{member.display_name}'s Avatar",
            color=BASE
        )
        embed.set_image(url=avatar_url)
        
        formats = []
        for fmt in ["png", "jpg", "webp"]:
            try:
                url = member.display_avatar.replace(format=fmt, size=1024).url
                formats.append(f"[{fmt.upper()}]({url})")
            except Exception:
                pass
        
        embed.description = " | ".join(formats) if formats else None
        
        if getattr(member, "guild_avatar", None):
            guild_avatar_url = member.guild_avatar.url
            embed.set_thumbnail(url=guild_avatar_url)
            embed.set_footer(text="Thumbnail shows Server-Specific Avatar")
            
        await ctx.send(embed=embed)

    @commands.hybrid_command(aliases=["ui", "whois"])
    @commands.guild_only()
    async def userinfo(self, ctx, member: Optional[discord.Member] = None):
        member = member or ctx.author
        
        flags = []
        if member.public_flags:
            for flag, active in member.public_flags:
                if active:
                    flags.append(flag.replace("_", " ").title())
        
        roles = [r.mention for r in sorted(member.roles, key=lambda x: x.position, reverse=True) if r != ctx.guild.default_role]
        
        embed = discord.Embed(
            title=f"User Info - {member}",
            color=member.color if member.color != discord.Color.default() else BASE
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        
        embed.add_field(name="Display Name", value=member.display_name, inline=True)
        embed.add_field(name="ID", value=f"`{member.id}`", inline=True)
        embed.add_field(name="Bot?", value="Yes" if member.bot else "No", inline=True)
        
        created_at_str = f"{discord.utils.format_dt(member.created_at, 'f')} ({discord.utils.format_dt(member.created_at, 'R')})"
        joined_at_str = f"{discord.utils.format_dt(member.joined_at, 'f')} ({discord.utils.format_dt(member.joined_at, 'R')})" if member.joined_at else "Unknown"
        
        embed.add_field(name="Account Created", value=created_at_str, inline=False)
        embed.add_field(name="Joined Server", value=joined_at_str, inline=False)
        
        activity_str = "None"
        if member.activity:
            if member.activity.type == discord.ActivityType.custom:
                activity_str = f"Status: {member.activity.name or ''} {member.activity.state or ''}".strip()
            else:
                activity_str = f"{member.activity.type.name.title()}: **{member.activity.name}**"
        embed.add_field(name="Current Activity", value=activity_str, inline=False)
        
        if flags:
            embed.add_field(name="Badges / Flags", value=", ".join(flags), inline=False)
            
        embed.add_field(name="Top Role", value=member.top_role.mention, inline=True)
        
        if roles:
            roles_str = ", ".join(roles)
            if len(roles_str) > 1024:
                roles_str = ", ".join(roles[:15]) + f" ... and {len(roles) - 15} more"
            embed.add_field(name=f"Roles ({len(roles)})", value=roles_str, inline=False)
        else:
            embed.add_field(name="Roles", value="No roles", inline=False)
            
        await ctx.send(embed=embed)

    @commands.hybrid_command(aliases=["si", "guildinfo"])
    @commands.guild_only()
    async def serverinfo(self, ctx):
        g = ctx.guild
        
        bots = sum(1 for m in g.members if m.bot)
        humans = g.member_count - bots
        
        text_channels = len(g.text_channels)
        voice_channels = len(g.voice_channels)
        categories = len(g.categories)
        stage_channels = len(g.stage_channels)
        total_channels = len(g.channels)
        
        embed = discord.Embed(
            title=f"Server Info - {g.name}",
            color=BASE
        )
        if g.icon:
            embed.set_thumbnail(url=g.icon.url)
            
        embed.add_field(name="Owner", value=f"{g.owner.mention} ({g.owner.id})", inline=False)
        embed.add_field(name="Server ID", value=f"`{g.id}`", inline=True)
        embed.add_field(name="Verification Level", value=str(g.verification_level).title(), inline=True)
        
        created_at_str = f"{discord.utils.format_dt(g.created_at, 'f')} ({discord.utils.format_dt(g.created_at, 'R')})"
        embed.add_field(name="Created On", value=created_at_str, inline=False)
        
        members_val = (
            f"Total: **{g.member_count}**\n"
            f"👤 Humans: **{humans}**\n"
            f"🤖 Bots: **{bots}**"
        )
        embed.add_field(name="Members", value=members_val, inline=True)
        
        channels_val = (
            f"Total: **{total_channels}**\n"
            f"💬 Text: **{text_channels}**\n"
            f"🔊 Voice: **{voice_channels}**\n"
            f"📁 Categories: **{categories}**"
        )
        if stage_channels:
            channels_val += f"\n🎭 Stage: **{stage_channels}**"
        embed.add_field(name="Channels", value=channels_val, inline=True)
        
        boosts_val = (
            f"Level: **{g.premium_tier}**\n"
            f"Boosts: **{g.premium_subscription_count}**"
        )
        embed.add_field(name="Boost Status", value=boosts_val, inline=True)
        
        embed.add_field(name="Roles Count", value=f"**{len(g.roles)}** roles", inline=True)
        embed.add_field(name="Emoji Count", value=f"**{len(g.emojis)}** emojis", inline=True)
        
        await ctx.send(embed=embed)

    @commands.hybrid_command(aliases=["latency", "botping"])
    async def status(self, ctx):
        await ctx.send(f"Bot latency: `{round(ctx.bot.latency * 1000)} ms`")

    @commands.hybrid_command(aliases=["8ball"])
    async def eightball(self, ctx, *, question: str):
        """Magic 8-Ball response."""
        answers = [
            "It is certain",
            "Without a doubt",
            "Yes",
            "No",
            "Maybe",
            "Ask again later",
            "My reply is no",
            "My sources say no",
            "Outlook good",
            "Very doubtful",
        ]
        embed = discord.Embed(
            title="🎱 Magic 8-Ball",
            color=BASE
        )
        embed.add_field(name="Question", value=question, inline=False)
        embed.add_field(name="Answer", value=f"🔮 **{random.choice(answers)}**", inline=False)
        embed.set_footer(text=f"Asked by {ctx.author.display_name}", icon_url=ctx.author.display_avatar.url)
        await ctx.send(embed=embed)

    @commands.hybrid_command()
    @commands.guild_only()
    async def poll(self, ctx, *, question: str):
        if "|" in question:
            parts = [p.strip() for p in question.split("|") if p.strip()]
            if len(parts) < 3:
                return await ctx.send("❌ For a multi-choice poll, you must specify a question and at least 2 choices separated by `|` (e.g. `poll Question | Option A | Option B`).")
            
            poll_title = parts[0]
            options = parts[1:]
            
            if len(options) > 10:
                return await ctx.send("❌ You can only specify up to 10 choices.")
            
            reactions = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]
            
            embed = discord.Embed(
                title=f"📊 {poll_title}",
                color=BASE
            )
            embed.set_footer(text=f"Polled by {ctx.author.display_name}", icon_url=ctx.author.display_avatar.url)
            
            desc = []
            for i, opt in enumerate(options):
                desc.append(f"{reactions[i]} {opt}")
            
            embed.description = "\n".join(desc)
            msg = await ctx.send(embed=embed)
            
            for i in range(len(options)):
                await msg.add_reaction(reactions[i])
        else:
            embed = discord.Embed(
                title="📊 Poll",
                description=f"**{question}**\n\n👍 Yes\n👎 No",
                color=BASE
            )
            embed.set_footer(text=f"Polled by {ctx.author.display_name}", icon_url=ctx.author.display_avatar.url)
            msg = await ctx.send(embed=embed)
            await msg.add_reaction("👍")
            await msg.add_reaction("👎")

    @commands.hybrid_command()
    async def choose(self, ctx: commands.Context, *, choices: str):
        """Choose from space-separated options; quote multiword options or separate with |."""
        try:
            choices = [c.strip() for c in choices.split("|") if c.strip()] if "|" in choices else shlex.split(choices)
        except ValueError:
            return await ctx.send("Close any quoted options before trying again.")
        if len(choices) < 2:
            return await ctx.send("❌ You must provide at least 2 options.")
        
        selected = random.choice(choices)
        embed = discord.Embed(
            title="🤔 The Decider",
            description=f"Out of your options, I choose:\n\n✨ **{selected}** ✨",
            color=BASE
        )
        embed.set_footer(text=f"Requested by {ctx.author.display_name}", icon_url=ctx.author.display_avatar.url)
        await ctx.send(embed=embed)

    @commands.hybrid_command()
    async def coinflip(self, ctx):
        """Flip a coin."""
        result = random.choice(['Heads', 'Tails'])
        embed = discord.Embed(
            title="🪙 Coinflip",
            description=f"The coin spun in the air and landed on:\n\n**{result}**",
            color=BASE
        )
        embed.set_footer(text=f"Flipped by {ctx.author.display_name}", icon_url=ctx.author.display_avatar.url)
        await ctx.send(embed=embed)

    @commands.hybrid_command()
    async def dice(self, ctx, *, expression: str = "6"):
        """
        Roll dice! Supports RPG notation (e.g. 2d6 + 4, 3d20) or single number of sides (e.g. 20).
        """
        expression = expression.strip().lower()
        
        if expression.isdigit():
            sides = int(expression)
            if not 2 <= sides <= 1000:
                return await ctx.send("❌ Number of sides must be between 2 and 1000.")
            roll = random.randint(1, sides)
            embed = discord.Embed(
                title="🎲 Dice Roll",
                description=f"Rolled a **d{sides}**:\nResult: **{roll}**",
                color=BASE
            )
            return await ctx.send(embed=embed)

        match = re.match(r"^\s*(\d*)\s*d\s*(\d+)(?:\s*([+-])\s*(\d+))?\s*$", expression)
        if not match:
            return await ctx.send("❌ Invalid dice format. Use D&D notation (e.g., `2d6`, `3d20 + 5`) or a single number of sides (e.g., `20`).")

        num_dice = int(match.group(1)) if match.group(1) else 1
        sides = int(match.group(2))
        modifier_sign = match.group(3)
        modifier_val = int(match.group(4)) if match.group(4) else 0

        if num_dice < 1 or num_dice > 50:
            return await ctx.send("❌ You can only roll between 1 and 50 dice at a time.")
        if sides < 2 or sides > 1000:
            return await ctx.send("❌ Dice must have between 2 and 1000 sides.")

        rolls = [random.randint(1, sides) for _ in range(num_dice)]
        subtotal = sum(rolls)
        
        if modifier_sign == "+":
            total = subtotal + modifier_val
            mod_str = f" + {modifier_val}"
        elif modifier_sign == "-":
            total = subtotal - modifier_val
            mod_str = f" - {modifier_val}"
        else:
            total = subtotal
            mod_str = ""

        rolls_str = ", ".join(f"`{r}`" for r in rolls)
        if len(rolls_str) > 1024:
            rolls_str = rolls_str[:1000] + "... [truncated]"

        embed = discord.Embed(
            title="🎲 RPG Dice Roll",
            color=BASE
        )
        embed.add_field(name="Formula", value=f"**{num_dice}d{sides}{mod_str}**", inline=True)
        embed.add_field(name="Result Breakdown", value=rolls_str, inline=False)
        
        if mod_str:
            embed.add_field(name="Subtotal", value=f"**{subtotal}**", inline=True)
            embed.add_field(name="Modifier", value=f"**{modifier_sign} {modifier_val}**", inline=True)
            
        embed.add_field(name="Total", value=f"🏆 **{total}**", inline=False)
        embed.set_footer(text=f"Rolled by {ctx.author.display_name}", icon_url=ctx.author.display_avatar.url)
        
        await ctx.send(embed=embed)

    @commands.hybrid_command()
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
        await ctx.send(
            f"{user.mention} Are you a hawk?",
            allowed_mentions=discord.AllowedMentions(users=True),
        )

    @commands.hybrid_command()
    @commands.guild_only()
    async def gay(self, ctx, user: Optional[discord.Member] = None):
        if not await self.config.guild(ctx.guild).gay_enabled():
            embed = discord.Embed(title="Gay Percentage", description="The gay command is currently **disabled**.", color=ERROR)
            embed.set_image(url=HAWK_DISABLED_GIF)
            return await ctx.send(embed=embed)
        user = user or ctx.author
        hawk_users = set(await self.config.guild(ctx.guild).hawk_users())
        is_owner = await ctx.bot.is_owner(ctx.author)
        if user.id in hawk_users and skibiditoilet in (ctx.message.content or "") and is_owner:
            pct = 9999
        elif user.id in hawk_users:
            pct = 9999 if random.randrange(100) == 0 else random.randint(GAY_PERCENTAGE_MIN_HAWK, GAY_PERCENTAGE_MAX_HAWK)
        else:
            pct = random.randint(GAY_PERCENTAGE_MIN_NORMAL, GAY_PERCENTAGE_MAX_NORMAL)
        await ctx.send(
            embed=discord.Embed(
                title="Gay Percentage", description=f"{user.display_name} is **{pct}% gay**", color=BASE
            )
        )

    @commands.command()
    async def thanos(self, ctx):
        await ctx.send(embed=discord.Embed(title="Thanos Meme", color=BASE).set_image(url=THANOS_IMG))

    @commands.hybrid_command()
    @commands.is_owner()
    @commands.guild_only()
    async def addhawk(self, ctx: commands.Context, *, users: str):
        """Add members by mentions or IDs separated by spaces; quote multiword names."""
        try:
            users = [await commands.MemberConverter().convert(ctx, value) for value in shlex.split(users)]
        except ValueError:
            return await ctx.send("Close quoted member names before trying again.")
        if not users:
            return await ctx.send("Please specify one or more users.")
        config = self.config.guild(ctx.guild)
        hawk_users = set(await config.hawk_users())
        added, already = [], []
        for user in users:
            (hawk_users.add(user.id) or added.append(user.display_name)) if user.id not in hawk_users else already.append(
                user.display_name
            )
        await config.hawk_users.set(list(hawk_users))
        msg = (f"🐦 Added: {', '.join(f'**{n}**' for n in added)}" if added else "") + (
            f"\nAlready in hawk list: {', '.join(already)}" if already else ""
        )
        await ctx.send(msg or "No users added.")

    @commands.hybrid_command()
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

    @commands.hybrid_command()
    @commands.is_owner()
    @commands.guild_only()
    async def listhawk(self, ctx):
        hawk_users = set(await self.config.guild(ctx.guild).hawk_users())
        if not hawk_users:
            return await ctx.send(
                embed=discord.Embed(title="🐦 Hawk List", description="The hawk list is empty.", color=ERROR).set_footer(
                    text="Total: 0"
                )
            )
        lines = [
            f"**{m.display_name}** (`{uid}`)" if (m := ctx.guild.get_member(uid)) else f"`{uid}` (not in server)"
            for uid in hawk_users
        ]
        await ctx.send(
            embed=discord.Embed(title="🐦 Hawk List", description="\n".join(lines), color=BASE).set_footer(
                text=f"Total: {len(hawk_users)}"
            )
        )

    @commands.hybrid_command()
    @commands.is_owner()
    @commands.guild_only()
    async def clearhawks(self, ctx):
        config = self.config.guild(ctx.guild)
        hawk_users = set(await config.hawk_users())
        removed = [uid for uid in hawk_users if not ctx.guild.get_member(uid)]
        await config.hawk_users.set([uid for uid in hawk_users if ctx.guild.get_member(uid)])
        embed = discord.Embed(
            title="🐦 Hawk List Cleanup",
            description=f"Removed {len(removed)} users who left the server.",
            color=BASE,
        )
        embed.add_field(name="Removed User IDs", value=", ".join(str(u) for u in removed) if removed else "None")
        await ctx.send(embed=embed)

    @commands.hybrid_command()
    @commands.is_owner()
    @commands.guild_only()
    async def disablehawk(self, ctx):
        config = self.config.guild(ctx.guild)
        enabled = await config.hawk_enabled()
        await config.hawk_enabled.set(not enabled)
        state = "enabled" if not enabled else "disabled"
        embed = discord.Embed(title="🐦 Hawk Command Toggled", description=f"Hawk command is now **{state}**.", color=BASE)
        if not enabled:
            embed.set_image(url=HAWK_ENABLED_GIF)
        await ctx.send(embed=embed)

    @commands.hybrid_command()
    @commands.is_owner()
    @commands.guild_only()
    async def disablegay(self, ctx):
        config = self.config.guild(ctx.guild)
        enabled = await config.gay_enabled()
        await config.gay_enabled.set(not enabled)
        state = "enabled" if not enabled else "disabled"
        await ctx.send(
            embed=discord.Embed(
                title="Gay Command Toggled", description=f"Gay command is now **{state}**.", color=BASE
            )
        )

    @commands.hybrid_command()
    @commands.is_owner()
    @commands.guild_only()
    async def timedping(self, ctx, user: discord.Member, *, duration: str):
        try:
            total = parse_duration_to_seconds(duration)
        except ValueError:
            return await ctx.send(
                "Invalid time. Use formats like 300, 5m, 1h30m, 2m15s. Units: d/h/m/s."
            )
        if total < 1 or total > 21600:
            return await ctx.send(
                "Please specify a total wait time between 1 second and 6 hours (21600 seconds)."
            )
        key = (ctx.guild.id, user.id)
        if key in self.timed_pings:
            return await ctx.send(
                f"{user.mention} already has an active timed ping. Use `[{ctx.clean_prefix}]stoptimedping @user` first.",
                allowed_mentions=discord.AllowedMentions(users=True),
            )
        self.timed_pings[key] = asyncio.create_task(self._repeating_ping(ctx.channel, user, total))
        mins, secs = divmod(total, 60)
        await ctx.send(
            f"Repeating timer set! Will ping {user.mention} every {total} seconds ({mins} min {secs} sec).",
            allowed_mentions=discord.AllowedMentions(users=True),
        )

    async def _repeating_ping(self, channel, user, interval):
        try:
            while True:
                await asyncio.sleep(interval)
                await channel.send(
                    f"{user.mention} Repeating ping!",
                    allowed_mentions=discord.AllowedMentions(users=True),
                )
        except asyncio.CancelledError:
            raise
        except discord.HTTPException:
            log.warning("Repeating ping stopped because delivery failed")
        finally:
            key = (channel.guild.id, user.id)
            if self.timed_pings.get(key) is asyncio.current_task():
                self.timed_pings.pop(key, None)

    @commands.hybrid_command()
    @commands.is_owner()
    @commands.guild_only()
    async def stoptimedping(self, ctx, user: discord.Member):
        key = (ctx.guild.id, user.id)
        if key not in self.timed_pings:
            return await ctx.send(f"No active timed ping found for {user.mention}.")
        self.timed_pings[key].cancel()
        del self.timed_pings[key]
        await ctx.send(f"Stopped repeating ping for {user.mention}.")

    @commands.hybrid_command()
    @commands.is_owner()
    @commands.guild_only()
    async def listtimedpings(self, ctx):
        active = [(uid, task) for (gid, uid), task in self.timed_pings.items() if gid == ctx.guild.id]
        if not active:
            return await ctx.send("No active timed pings in this server.")
        lines = [
            f"• {m.mention} (`{uid}`)" if (m := ctx.guild.get_member(uid)) else f"• Unknown user (`{uid}`)"
            for uid, _ in active
        ]
        await ctx.send("**Active Timed Pings:**\n" + "\n".join(lines))

    @commands.hybrid_command()
    @commands.guild_only()
    async def membercount(self, ctx: commands.Context):
        """Show human, bot, and total member counts."""
        if not ctx.guild.chunked:
            await ctx.guild.chunk()
        members = ctx.guild.members
        bots = sum(member.bot for member in members)
        embed = discord.Embed(title="Member count", color=BASE)
        embed.add_field(name="Humans", value=str(len(members) - bots))
        embed.add_field(name="Bots", value=str(bots))
        embed.add_field(name="Total", value=str(ctx.guild.member_count or len(members)))
        if ctx.guild.member_count != len(members):
            embed.set_footer(text="Human/bot breakdown reflects available cached members.")
        await ctx.send(embed=embed)

    @commands.hybrid_command()
    async def timestamp(self, ctx: commands.Context, date_time: str, timezone: str = "UTC"):
        """Create Discord timestamps. Quote YYYY-MM-DD HH:MM for prefix use; default zone UTC."""
        try:
            stamp = parse_timestamp(date_time, timezone)
        except (ValueError, OverflowError, OSError) as exc:
            return await ctx.send(str(exc), allowed_mentions=discord.AllowedMentions.none())
        embed = discord.Embed(title="Discord timestamp", description=f"<t:{stamp}:F>\n<t:{stamp}:R>", color=BASE)
        for label, style in (("Full date", "F"), ("Short date/time", "f"), ("Relative", "R"), ("Time", "t")):
            embed.add_field(name=label, value=f"`<t:{stamp}:{style}>`", inline=False)
        embed.set_footer(text=f"Input timezone: {timezone}. Discord displays times in each viewer's timezone.")
        await ctx.send(embed=embed)

    @commands.hybrid_command()
    @commands.guild_only()
    async def quote(self, ctx: commands.Context, message_link: str):
        """Quote a message. Cross-channel quotes are private slash responses only."""
        try:
            guild_id, channel_id, message_id = parse_message_link(message_link)
        except ValueError as exc:
            return await ctx.send(str(exc), ephemeral=True)
        if guild_id != ctx.guild.id:
            return await ctx.send("Quote a message from this server.", ephemeral=True)
        channel = ctx.guild.get_channel_or_thread(channel_id)
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            return await ctx.send("That message channel is unavailable.", ephemeral=True)
        # Prefix output remains in its source channel. Slash output is always private.
        if channel.id != ctx.channel.id and not ctx.interaction:
            return await ctx.send("Use quote in the source channel, or /quote for a private cross-channel quote.")
        for member in (ctx.author, ctx.guild.me):
            perms = channel.permissions_for(member)
            if not (perms.view_channel and perms.read_message_history):
                return await ctx.send("You and I both need access to that channel's message history.", ephemeral=True)
        if isinstance(channel, discord.Thread) and channel.is_private():
            if not channel.permissions_for(ctx.author).manage_threads:
                try:
                    await channel.fetch_member(ctx.author.id)
                except discord.HTTPException:
                    return await ctx.send("You must belong to that private thread.", ephemeral=True)
        try:
            message = await channel.fetch_message(message_id)
        except discord.HTTPException:
            return await ctx.send("I could not retrieve that message. It may have been deleted or become inaccessible.", ephemeral=True)
        embed = discord.Embed(title="Quoted message", description=message.content[:3500] or "No text content.", color=BASE, timestamp=message.created_at)
        embed.set_author(name=str(message.author), icon_url=message.author.display_avatar.url)
        embed.add_field(name="Original", value=f"[Jump to message]({message.jump_url})")
        if message.attachments:
            embed.add_field(name="Attachments", value=f"{len(message.attachments)} attachment(s) — open the original to view.")
        embed.set_footer(text=f"#{channel.name}")
        await ctx.send(embed=embed, ephemeral=bool(ctx.interaction), allowed_mentions=discord.AllowedMentions.none())

    async def _cancel_reminder(self, user_id: int, reminder_id: str) -> bool:
        async with self._reminder_lock:
            async with self.config.user_from_id(user_id).reminders() as reminders:
                return reminders.pop(reminder_id, None) is not None

    @commands.hybrid_command()
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def remindme(self, ctx: commands.Context, duration: str, *, text: str):
        """Set a personal DM reminder, from 10 seconds to 365 days; survives restarts."""
        try:
            seconds = parse_duration_to_seconds(duration)
        except ValueError:
            return await ctx.send("Use a duration such as 30m, 2h, or 1d12h.", ephemeral=True)
        if not 10 <= seconds <= 365 * 86400 or not 1 <= len(text.strip()) <= 1500:
            return await ctx.send("Use 10 seconds–365 days and 1–1500 characters of reminder text.", ephemeral=True)
        due = (discord.utils.utcnow() + timedelta(seconds=seconds)).timestamp()
        reminder_id = uuid.uuid4().hex[:12]
        async with self._reminder_lock:
            async with self.config.user(ctx.author).reminders() as reminders:
                if len(reminders) >= 20:
                    return await ctx.send("You have 20 reminders. Cancel an old reminder first.", ephemeral=True)
                reminders[reminder_id] = {"text": text.strip(), "due": due, "attempts": 0, "state": "pending"}
        embed = discord.Embed(title="Reminder scheduled", description=f"I’ll DM you <t:{int(due)}:R>.\n\n{discord.utils.escape_mentions(text)}", color=BASE)
        embed.set_footer(text=f"ID: {reminder_id} · DMs must be open. Use reminders list/cancel after restart.")
        await ctx.send(embed=embed, view=ReminderView(self, ctx.author.id, reminder_id), ephemeral=bool(ctx.interaction), allowed_mentions=discord.AllowedMentions.none())

    @commands.hybrid_group(invoke_without_command=True, fallback="show")
    async def reminders(self, ctx: commands.Context):
        """View or cancel your personal reminders."""
        await self._show_reminders(ctx)

    @reminders.command(name="list")
    async def reminders_list(self, ctx: commands.Context):
        """Show pending reminders and delivery failures privately."""
        await self._show_reminders(ctx)

    async def _show_reminders(self, ctx: commands.Context) -> None:
        reminders = await self.config.user(ctx.author).reminders()
        embed = discord.Embed(title="Your reminders", color=BASE)
        for rid, item in sorted(reminders.items(), key=lambda pair: pair[1]["due"]):
            embed.add_field(name=f"{rid} · {item['state']}", value=f"<t:{int(item['due'])}:R> — {item['text'][:170]}", inline=False)
        if not reminders:
            embed.description = "You have no saved reminders."
        if ctx.interaction or not ctx.guild:
            await ctx.send(embed=embed, ephemeral=bool(ctx.interaction), allowed_mentions=discord.AllowedMentions.none())
        else:
            try:
                await ctx.author.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
            except discord.HTTPException:
                return await ctx.send("I couldn't DM your reminders. Use /reminders list for a private response.")
            await ctx.send("Sent your reminders by DM.")

    @reminders.command(name="cancel")
    async def reminders_cancel(self, ctx: commands.Context, reminder_id: str):
        """Cancel one of your reminders using its ID."""
        removed = await self._cancel_reminder(ctx.author.id, reminder_id)
        await ctx.send("Reminder cancelled." if removed else "No reminder with that ID belongs to you.", ephemeral=bool(ctx.interaction))

    async def _deliver_due_reminders(self) -> None:
        users = await self.config.all_users()
        now = discord.utils.utcnow().timestamp()
        for uid, data in users.items():
            for rid, snapshot in data.get("reminders", {}).items():
                if snapshot.get("state") != "pending" or snapshot.get("retry_at", snapshot["due"]) > now:
                    continue
                # Serialize delivery with cancellation and privacy deletion. Re-read stale snapshots.
                async with self._reminder_lock:
                    async with self.config.user_from_id(uid).reminders() as reminders:
                        item = reminders.get(rid)
                        if not item or item["state"] != "pending":
                            continue
                        try:
                            user = self.bot.get_user(uid) or await self.bot.fetch_user(uid)
                            await user.send(embed=discord.Embed(title="Reminder", description=item["text"], color=BASE), allowed_mentions=discord.AllowedMentions.none())
                        except (discord.Forbidden, discord.NotFound):
                            item["state"] = "failed (DM unavailable)"
                        except discord.HTTPException:
                            item["attempts"] += 1
                            if item["attempts"] >= 5:
                                item["state"] = "failed (delivery error)"
                            else:
                                item["retry_at"] = now + 60 * item["attempts"]
                        else:
                            del reminders[rid]

    async def _reminder_loop(self) -> None:
        await self.bot.wait_until_ready()
        while True:
            try:
                await self._deliver_due_reminders()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Reminder scheduler iteration failed")
            await asyncio.sleep(15)

    async def red_delete_data_for_user(self, *, requester: str, user_id: int) -> None:
        """Remove personal reminders when Red requests deletion of user data."""
        async with self._reminder_lock:
            await self.config.user_from_id(user_id).clear()

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
