"""MessageDelete - High-performance Discord bot with comprehensive moderation and utilities."""

from redbot.core import commands, Config
from redbot.core.utils.chat_formatting import humanize_timedelta
import discord
import random
import asyncio
import logging
import re
import traceback
from typing import Optional, Union, Dict, List, Tuple
from datetime import datetime, timedelta
from functools import lru_cache
from collections import defaultdict, deque

# Configure logging
log = logging.getLogger("red.messagedelete")

# Performance and feature constants
DEFAULT_PING_AMOUNT = 5
MAX_PING_AMOUNT = 20
PING_DELAY = 0.5
GAY_PERCENTAGE_MIN_NORMAL = 0
GAY_PERCENTAGE_MAX_NORMAL = 100
GAY_PERCENTAGE_MIN_HAWK = 51
GAY_PERCENTAGE_MAX_HAWK = 150
MAX_PURGE_LIMIT = 1000
CACHE_SIZE = 1000
SPAM_WINDOW = 10  # seconds
MAX_SPAM_MESSAGES = 5


class MessageCache:
    """Efficient message cache with memory management."""
    
    def __init__(self, max_size: int = CACHE_SIZE):
        self.cache: Dict[int, deque] = defaultdict(lambda: deque(maxlen=max_size // 100))
        self.max_guilds = 100
    
    def add_message(self, guild_id: int, user_id: int, timestamp: datetime) -> None:
        """Add message timestamp to cache."""
        if len(self.cache) > self.max_guilds:
            # Remove oldest guild cache
            oldest_guild = min(self.cache.keys())
            del self.cache[oldest_guild]
        
        self.cache[guild_id].append((user_id, timestamp))
    
    def get_user_messages(self, guild_id: int, user_id: int, window: int = SPAM_WINDOW) -> int:
        """Get message count for user in time window."""
        if guild_id not in self.cache:
            return 0
        
        cutoff = datetime.utcnow() - timedelta(seconds=window)
        count = sum(1 for uid, ts in self.cache[guild_id] if uid == user_id and ts > cutoff)
        return count


class MessageDelete(commands.Cog):
    """High-performance Discord bot with comprehensive moderation and utilities."""

    __author__ = ["YourName"]
    __version__ = "4.0.1"

    def __init__(self, bot):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=1234567890, force_registration=True)
        
        # Optimized default configuration
        default_guild = {
            "blocked_users": [],
            "hawk_users": [
                786624423721041941, 500641384835842049, 275549294969356288,
                685961799518257175, 871044256800854078, 332176051914539010
            ],
            "hawk_enabled": True,
            "gay_enabled": True,
            "warnings": {},
            "automod_enabled": False,
            "spam_threshold": MAX_SPAM_MESSAGES,
            "mass_mention_limit": 5,
            "filter_invites": False,
            "filter_links": False
        }
        self.config.register_guild(**default_guild)
        
        # Performance-optimized runtime state
        self.awaiting_hawk_response: Dict[int, int] = {}
        self.last_hawk_user: Dict[int, int] = {}
        self.message_cache = MessageCache()
        
        # Precompiled regex patterns for performance
        self.invite_pattern = re.compile(r'(?:discord\.(?:gg|io|me|li)|discordapp\.com\/invite)\/[a-zA-Z0-9]+', re.IGNORECASE)
        self.link_pattern = re.compile(r'https?:\/\/[^\s]+', re.IGNORECASE)

    def format_help_for_context(self, ctx: commands.Context) -> str:
        """Show cog version in help."""
        return f"{super().format_help_for_context(ctx)}\n\nVersion: {self.__version__}"

    @lru_cache(maxsize=256)
    def get_embed_color(self, color_type: str = "default") -> int:
        """Cached embed colors for performance."""
        colors = {
            "default": 0x2F3136,
            "success": 0x57F287,
            "error": 0xED4245,
            "warning": 0xFEE75C,
            "info": 0x5865F2
        }
        return colors.get(color_type, colors["default"])

    def create_embed(
        self, 
        title: str, 
        description: str = None, 
        color: str = "default",
        fields: List[Tuple[str, str, bool]] = None
    ) -> discord.Embed:
        """Create standardized embed with consistent styling."""
        embed = discord.Embed(
            title=title,
            description=description,
            color=self.get_embed_color(color),
            timestamp=datetime.utcnow()
        )
        
        if fields:
            for name, value, inline in fields:
                embed.add_field(name=name, value=value, inline=inline)
        
        return embed

    async def safe_send(self, destination, content: str = None, embed: discord.Embed = None, delete_after: int = None) -> Optional[discord.Message]:
        """Safely send message with comprehensive error handling."""
        try:
            return await destination.send(content=content, embed=embed, delete_after=delete_after)
        except discord.Forbidden:
            log.warning(f"Missing permissions to send message in {destination}")
        except discord.HTTPException as e:
            log.error(f"HTTP error sending message: {e}")
        except Exception as e:
            log.error(f"Unexpected error sending message: {e}")
        return None

    def check_spam(self, guild_id: int, user_id: int) -> bool:
        """Optimized spam detection with caching."""
        now = datetime.utcnow()
        self.message_cache.add_message(guild_id, user_id, now)
        return self.message_cache.get_user_messages(guild_id, user_id) >= MAX_SPAM_MESSAGES

    @commands.Cog.listener()
    async def on_command_error(self, ctx: commands.Context, error: Exception):
        """Comprehensive error handling for all commands."""
        # Ignore handled errors
        if hasattr(ctx.command, 'on_error') or hasattr(ctx.cog, f'_{ctx.command.name}_error'):
            return

        error_embed = None
        
        if isinstance(error, commands.CommandNotFound):
            return  # Ignore unknown commands
        
        elif isinstance(error, commands.MissingRequiredArgument):
            error_embed = self.create_embed(
                "Missing Argument",
                f"Missing required argument: `{error.param.name}`\n\nUse `{ctx.prefix}help {ctx.command}` for usage info.",
                "error"
            )
        
        elif isinstance(error, commands.BadArgument):
            error_embed = self.create_embed(
                "Invalid Argument",
                f"Invalid argument provided.\n\nUse `{ctx.prefix}help {ctx.command}` for usage info.",
                "error"
            )
        
        elif isinstance(error, commands.MissingPermissions):
            missing_perms = ", ".join(error.missing_permissions)
            error_embed = self.create_embed(
                "Missing Permissions",
                f"You need the following permissions: `{missing_perms}`",
                "error"
            )
        
        elif isinstance(error, commands.BotMissingPermissions):
            missing_perms = ", ".join(error.missing_permissions)
            error_embed = self.create_embed(
                "Bot Missing Permissions",
                f"I need the following permissions: `{missing_perms}`",
                "error"
            )
        
        elif isinstance(error, commands.CommandOnCooldown):
            error_embed = self.create_embed(
                "Command on Cooldown",
                f"Try again in {error.retry_after:.1f} seconds.",
                "warning"
            )
        
        elif isinstance(error, commands.NotOwner):
            error_embed = self.create_embed(
                "Owner Only",
                "This command can only be used by the bot owner.",
                "error"
            )
        
        else:
            # Log unexpected errors
            log.error(f"Unhandled error in {ctx.command}: {error}", exc_info=error)
            error_embed = self.create_embed(
                "Unexpected Error",
                "An unexpected error occurred. The incident has been logged.",
                "error"
            )

        if error_embed:
            await self.safe_send(ctx, embed=error_embed, delete_after=15)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """Optimized message processing with early returns."""
        # Early performance optimizations
        if not message.guild or message.author.bot:
            return
        
        guild_id = message.guild.id
        
        # Hawk response handling (highest priority)
        if guild_id in self.awaiting_hawk_response:
            user_id = self.awaiting_hawk_response[guild_id]
            if message.author.id == user_id:
                content_lower = message.content.lower().strip()
                if content_lower in ("yes", "no"):
                    response = "I'm a hawk too" if content_lower == "yes" else "Fuck you then"
                    await self.safe_send(message.channel, response)
                    del self.awaiting_hawk_response[guild_id]
                    return

        # Blocked user handling
        try:
            blocked_users = await self.config.guild(message.guild).blocked_users()
            if message.author.id in blocked_users:
                await message.delete()
                return
        except (discord.Forbidden, discord.HTTPException):
            pass

        # Automoderation (batch config fetch for performance)
        try:
            guild_config = await self.config.guild(message.guild).all()
            if not guild_config["automod_enabled"]:
                return

            delete_message = False
            warning_msg = None

            # Spam detection
            if self.check_spam(guild_id, message.author.id):
                delete_message = True
                warning_msg = f"{message.author.mention}, please slow down your messages!"

            # Mass mentions check
            elif len(message.mentions) >= guild_config["mass_mention_limit"]:
                delete_message = True 
                warning_msg = f"{message.author.mention}, too many mentions in one message!"

            # Invite filter
            elif guild_config["filter_invites"] and self.invite_pattern.search(message.content):
                delete_message = True
                warning_msg = f"{message.author.mention}, invite links are not allowed!"

            # Link filter  
            elif guild_config["filter_links"] and self.link_pattern.search(message.content):
                delete_message = True
                warning_msg = f"{message.author.mention}, external links are not allowed!"

            if delete_message:
                await message.delete()
                if warning_msg:
                    await self.safe_send(message.channel, warning_msg, delete_after=5)

        except Exception as e:
            log.error(f"Error in automod processing: {e}")

    # ==================== Core Moderation Commands ====================
    
    @commands.command()
    @commands.guild_only()
    @commands.has_permissions(kick_members=True)
    @commands.bot_has_permissions(kick_members=True)
    async def kick(self, ctx: commands.Context, member: discord.Member, *, reason: Optional[str] = None):
        """Kick a member from the server."""
        if not await self._can_moderate(ctx, member):
            return
        
        reason = reason or "No reason provided"
        
        try:
            # Attempt to notify user
            try:
                embed = self.create_embed(
                    f"Kicked from {ctx.guild.name}",
                    f"**Reason:** {reason}",
                    "warning"
                )
                await member.send(embed=embed)
            except (discord.Forbidden, discord.HTTPException):
                pass

            await member.kick(reason=f"{ctx.author}: {reason}")
            
            success_embed = self.create_embed(
                "Member Kicked",
                f"**{member}** has been kicked.\n**Reason:** {reason}",
                "success"
            )
            await self.safe_send(ctx, embed=success_embed)
            
        except discord.Forbidden:
            error_embed = self.create_embed("Error", "I don't have permission to kick that member.", "error")
            await self.safe_send(ctx, embed=error_embed)
        except discord.HTTPException as e:
            error_embed = self.create_embed("Error", f"Failed to kick member: {e}", "error")
            await self.safe_send(ctx, embed=error_embed)

    @commands.command()
    @commands.guild_only()
    @commands.has_permissions(ban_members=True)
    @commands.bot_has_permissions(ban_members=True)
    async def ban(self, ctx: commands.Context, member: Union[discord.Member, discord.User], *, reason: Optional[str] = None):
        """Ban a user from the server."""
        if isinstance(member, discord.Member) and not await self._can_moderate(ctx, member):
            return
        
        reason = reason or "No reason provided"
        
        try:
            # Attempt to notify user
            try:
                embed = self.create_embed(
                    f"Banned from {ctx.guild.name}",
                    f"**Reason:** {reason}",
                    "error"
                )
                await member.send(embed=embed)
            except (discord.Forbidden, discord.HTTPException, AttributeError):
                pass

            await ctx.guild.ban(member, reason=f"{ctx.author}: {reason}", delete_message_days=1)
            
            success_embed = self.create_embed(
                "Member Banned",
                f"**{member}** has been banned.\n**Reason:** {reason}",
                "success"
            )
            await self.safe_send(ctx, embed=success_embed)
            
        except discord.Forbidden:
            error_embed = self.create_embed("Error", "I don't have permission to ban that user.", "error")
            await self.safe_send(ctx, embed=error_embed)
        except discord.HTTPException as e:
            error_embed = self.create_embed("Error", f"Failed to ban user: {e}", "error")
            await self.safe_send(ctx, embed=error_embed)

    @commands.command()
    @commands.guild_only()
    @commands.has_permissions(ban_members=True)
    @commands.bot_has_permissions(ban_members=True)
    async def unban(self, ctx: commands.Context, user_id: int, *, reason: Optional[str] = None):
        """Unban a user from the server."""
        reason = reason or "No reason provided"
        
        try:
            user = await self.bot.fetch_user(user_id)
            await ctx.guild.unban(user, reason=f"{ctx.author}: {reason}")
            
            success_embed = self.create_embed(
                "Member Unbanned",
                f"**{user}** has been unbanned.\n**Reason:** {reason}",
                "success"
            )
            await self.safe_send(ctx, embed=success_embed)
            
        except discord.NotFound:
            error_embed = self.create_embed("Error", "That user is not banned or doesn't exist.", "error")
            await self.safe_send(ctx, embed=error_embed)
        except discord.Forbidden:
            error_embed = self.create_embed("Error", "I don't have permission to unban users.", "error")
            await self.safe_send(ctx, embed=error_embed)
        except discord.HTTPException as e:
            error_embed = self.create_embed("Error", f"Failed to unban user: {e}", "error")
            await self.safe_send(ctx, embed=error_embed)

    @commands.command(aliases=["mute"])
    @commands.guild_only()
    @commands.has_permissions(moderate_members=True)
    @commands.bot_has_permissions(moderate_members=True)
    async def timeout(self, ctx: commands.Context, member: discord.Member, duration: str, *, reason: Optional[str] = None):
        """Timeout a member for a specified duration."""
        if not await self._can_moderate(ctx, member):
            return
        
        # Parse duration with better error handling
        try:
            delta = self._parse_duration(duration)
        except ValueError as e:
            error_embed = self.create_embed("Invalid Duration", str(e), "error")
            await self.safe_send(ctx, embed=error_embed)
            return
        
        reason = reason or "No reason provided"
        
        try:
            await member.timeout(delta, reason=f"{ctx.author}: {reason}")
            
            success_embed = self.create_embed(
                "Member Timed Out",
                f"**{member}** has been timed out for {duration}.\n**Reason:** {reason}",
                "success"
            )
            await self.safe_send(ctx, embed=success_embed)
            
        except discord.Forbidden:
            error_embed = self.create_embed("Error", "I don't have permission to timeout that member.", "error")
            await self.safe_send(ctx, embed=error_embed)
        except discord.HTTPException as e:
            error_embed = self.create_embed("Error", f"Failed to timeout member: {e}", "error")
            await self.safe_send(ctx, embed=error_embed)

    @commands.command(aliases=["unmute"])
    @commands.guild_only()
    @commands.has_permissions(moderate_members=True)
    @commands.bot_has_permissions(moderate_members=True)
    async def untimeout(self, ctx: commands.Context, member: discord.Member, *, reason: Optional[str] = None):
        """Remove a timeout from a member."""
        reason = reason or "No reason provided"
        
        try:
            await member.timeout(None, reason=f"{ctx.author}: {reason}")
            
            success_embed = self.create_embed(
                "Timeout Removed",
                f"**{member}**'s timeout has been removed.",
                "success"
            )
            await self.safe_send(ctx, embed=success_embed)
            
        except discord.Forbidden:
            error_embed = self.create_embed("Error", "I don't have permission to remove timeouts.", "error")
            await self.safe_send(ctx, embed=error_embed)
        except discord.HTTPException as e:
            error_embed = self.create_embed("Error", f"Failed to remove timeout: {e}", "error")
            await self.safe_send(ctx, embed=error_embed)

    @commands.command(aliases=["clear", "clean"])
    @commands.guild_only()
    @commands.has_permissions(manage_messages=True)
    @commands.bot_has_permissions(manage_messages=True, read_message_history=True)
    async def purge(self, ctx: commands.Context, amount: int, member: Optional[discord.Member] = None):
        """Delete messages. Can target specific user or purge all messages."""
        if amount < 1:
            error_embed = self.create_embed("Invalid Amount", "Amount must be at least 1.", "error")
            await self.safe_send(ctx, embed=error_embed)
            return
        
        if amount > MAX_PURGE_LIMIT:
            error_embed = self.create_embed("Amount Too Large", f"Cannot purge more than {MAX_PURGE_LIMIT} messages at once.", "error")
            await self.safe_send(ctx, embed=error_embed)
            return
        
        try:
            def check(m):
                return m.author == member if member else True
            
            deleted = await ctx.channel.purge(limit=amount + 1, check=check)
            count = len(deleted) - 1  # Subtract command message
            
            target_text = f" from **{member}**" if member else ""
            success_embed = self.create_embed(
                "Messages Purged",
                f"Deleted {count} message(s){target_text}.",
                "success"
            )
            msg = await self.safe_send(ctx, embed=success_embed)
            
            # Auto-delete confirmation after 5 seconds
            if msg:
                await msg.delete(delay=5)
            
        except discord.Forbidden:
            error_embed = self.create_embed("Error", "I don't have permission to delete messages.", "error")
            await self.safe_send(ctx, embed=error_embed)
        except discord.HTTPException as e:
            error_embed = self.create_embed("Error", f"Failed to purge messages: {e}", "error")
            await self.safe_send(ctx, embed=error_embed)

    # ==================== Channel Management ====================
    
    @commands.command()
    @commands.guild_only()
    @commands.has_permissions(manage_channels=True)
    @commands.bot_has_permissions(manage_channels=True)
    async def lock(self, ctx: commands.Context, channel: Optional[discord.TextChannel] = None):
        """Lock a channel so members cannot send messages."""
        channel = channel or ctx.channel
        overwrite = channel.overwrites_for(ctx.guild.default_role)
        
        if overwrite.send_messages is False:
            error_embed = self.create_embed("Already Locked", f"{channel.mention} is already locked.", "warning")
            await self.safe_send(ctx, embed=error_embed)
            return
        
        overwrite.send_messages = False
        
        try:
            await channel.set_permissions(ctx.guild.default_role, overwrite=overwrite, reason=f"Locked by {ctx.author}")
            success_embed = self.create_embed("Channel Locked", f"{channel.mention} has been locked.", "success")
            await self.safe_send(ctx, embed=success_embed)
            
        except discord.Forbidden:
            error_embed = self.create_embed("Error", "I don't have permission to lock that channel.", "error")
            await self.safe_send(ctx, embed=error_embed)
        except discord.HTTPException as e:
            error_embed = self.create_embed("Error", f"Failed to lock channel: {e}", "error")
            await self.safe_send(ctx, embed=error_embed)

    @commands.command()
    @commands.guild_only()
    @commands.has_permissions(manage_channels=True)
    @commands.bot_has_permissions(manage_channels=True)
    async def unlock(self, ctx: commands.Context, channel: Optional[discord.TextChannel] = None):
        """Unlock a channel so members can send messages."""
        channel = channel or ctx.channel
        overwrite = channel.overwrites_for(ctx.guild.default_role)
        
        if overwrite.send_messages is not False:
            error_embed = self.create_embed("Not Locked", f"{channel.mention} is not locked.", "warning")
            await self.safe_send(ctx, embed=error_embed)
            return
        
        overwrite.send_messages = None
        
        try:
            await channel.set_permissions(ctx.guild.default_role, overwrite=overwrite, reason=f"Unlocked by {ctx.author}")
            success_embed = self.create_embed("Channel Unlocked", f"{channel.mention} has been unlocked.", "success")
            await self.safe_send(ctx, embed=success_embed)
            
        except discord.Forbidden:
            error_embed = self.create_embed("Error", "I don't have permission to unlock that channel.", "error")
            await self.safe_send(ctx, embed=error_embed)
        except discord.HTTPException as e:
            error_embed = self.create_embed("Error", f"Failed to unlock channel: {e}", "error")
            await self.safe_send(ctx, embed=error_embed)

    @commands.command()
    @commands.guild_only()
    @commands.has_permissions(manage_channels=True)
    @commands.bot_has_permissions(manage_channels=True)
    async def slowmode(self, ctx: commands.Context, seconds: int, channel: Optional[discord.TextChannel] = None):
        """Set slowmode for a channel."""
        channel = channel or ctx.channel
        
        if seconds < 0 or seconds > 21600:
            error_embed = self.create_embed("Invalid Duration", "Slowmode must be between 0 and 21600 seconds (6 hours).", "error")
            await self.safe_send(ctx, embed=error_embed)
            return
        
        try:
            await channel.edit(slowmode_delay=seconds, reason=f"Set by {ctx.author}")
            
            if seconds == 0:
                embed_title = "Slowmode Disabled"
                embed_desc = f"Slowmode disabled in {channel.mention}."
            else:
                embed_title = "Slowmode Set"
                embed_desc = f"Slowmode set to {seconds} seconds in {channel.mention}."
            
            success_embed = self.create_embed(embed_title, embed_desc, "success")
            await self.safe_send(ctx, embed=success_embed)
                
        except discord.Forbidden:
            error_embed = self.create_embed("Error", "I don't have permission to edit that channel.", "error")
            await self.safe_send(ctx, embed=error_embed)
        except discord.HTTPException as e:
            error_embed = self.create_embed("Error", f"Failed to set slowmode: {e}", "error")
            await self.safe_send(ctx, embed=error_embed)

    # ==================== Utility Functions ====================
    
    async def _can_moderate(self, ctx: commands.Context, member: discord.Member) -> bool:
        """Check if user can moderate target member."""
        if member.top_role >= ctx.author.top_role and ctx.author != ctx.guild.owner:
            error_embed = self.create_embed("Insufficient Permissions", "You cannot moderate someone with a role equal to or higher than yours.", "error")
            await self.safe_send(ctx, embed=error_embed)
            return False
        
        if member.top_role >= ctx.guild.me.top_role:
            error_embed = self.create_embed("Insufficient Permissions", "I cannot moderate someone with a role equal to or higher than mine.", "error")
            await self.safe_send(ctx, embed=error_embed)
            return False
        
        if member == ctx.guild.owner:
            error_embed = self.create_embed("Cannot Moderate Owner", "Cannot moderate the server owner.", "error")
            await self.safe_send(ctx, embed=error_embed)
            return False
        
        return True

    def _parse_duration(self, duration: str) -> timedelta:
        """Parse duration string into timedelta object."""
        time_units = {"s": 1, "m": 60, "h": 3600, "d": 86400}
        
        if len(duration) < 2:
            raise ValueError("Duration must be at least 2 characters (e.g., '5m')")
        
        unit = duration[-1].lower()
        if unit not in time_units:
            raise ValueError("Invalid time unit. Use: s (seconds), m (minutes), h (hours), d (days)")
        
        try:
            amount = int(duration[:-1])
        except ValueError:
            raise ValueError("Invalid duration format. Example: 10m, 2h, 1d")
        
        if amount <= 0:
            raise ValueError("Duration must be a positive number")
        
        seconds = amount * time_units[unit]
        if seconds > 2419200:  # 28 days
            raise ValueError("Maximum timeout duration is 28 days")
        
        return timedelta(seconds=seconds)

    # ==================== Information Commands ====================

    @commands.command(aliases=["ui", "whois"])
    @commands.guild_only()
    async def userinfo(self, ctx: commands.Context, member: Optional[discord.Member] = None):
        """Show detailed information about a user."""
        member = member or ctx.author
        
        roles = [role.mention for role in member.roles if role != ctx.guild.default_role]
        roles_display = ", ".join(roles[:10])  # Limit to prevent embed overflow
        if len(roles) > 10:
            roles_display += f" ... and {len(roles) - 10} more"
        
        fields = [
            ("User ID", str(member.id), True),
            ("Nickname", member.nick or "None", True),
            ("Bot Account", "Yes" if member.bot else "No", True),
            ("Account Created", f"<t:{int(member.created_at.timestamp())}:R>", True),
            ("Joined Server", f"<t:{int(member.joined_at.timestamp())}:R>", True),
            ("Top Role", member.top_role.mention, True),
        ]
        
        if member.premium_since:
            fields.append(("Boosting Since", f"<t:{int(member.premium_since.timestamp())}:R>", True))
        
        if roles_display:
            fields.append((f"Roles ({len(roles)})", roles_display, False))
        
        embed = self.create_embed(f"User Information - {member}", fields=fields)
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.color = member.color if member.color != discord.Color.default() else self.get_embed_color()
        
        await self.safe_send(ctx, embed=embed)

    @commands.command(aliases=["si", "guildinfo"])
    @commands.guild_only()
    async def serverinfo(self, ctx: commands.Context):
        """Show information about the server."""
        guild = ctx.guild
        
        # Calculate member stats
        online_members = sum(1 for m in guild.members if m.status != discord.Status.offline)
        bot_count = sum(1 for m in guild.members if m.bot)
        
        fields = [
            ("Server ID", str(guild.id), True),
            ("Owner", guild.owner.mention if guild.owner else "Unknown", True),
            ("Created", f"<t:{int(guild.created_at.timestamp())}:R>", True),
            ("Members", f"{guild.member_count:,}", True),
            ("Online", f"{online_members:,}", True),
            ("Bots", f"{bot_count:,}", True),
            ("Roles", f"{len(guild.roles):,}", True),
            ("Channels", f"{len(guild.channels):,}", True),
            ("Boosts", f"{guild.premium_subscription_count} (Level {guild.premium_tier})", True),
            ("Verification Level", str(guild.verification_level).replace('_', ' ').title(), True),
        ]
        
        embed = self.create_embed(f"Server Information - {guild.name}", fields=fields)
        
        if guild.icon:
            embed.set_thumbnail(url=guild.icon.url)
        
        await self.safe_send(ctx, embed=embed)

    @commands.command(aliases=["av", "pfp"])
    @commands.guild_only()
    async def avatar(self, ctx: commands.Context, member: Optional[discord.Member] = None):
        """Show a user's avatar."""
        member = member or ctx.author
        
        embed = self.create_embed(
            f"{member.display_name}'s Avatar",
            f"[Download Link]({member.display_avatar.url})"
        )
        embed.set_image(url=member.display_avatar.url)
        
        await self.safe_send(ctx, embed=embed)

    @commands.command()
    @commands.guild_only()
    async def ping(self, ctx: commands.Context):
        """Check the bot's latency."""
        latency_ms = round(self.bot.latency * 1000)
        
        if latency_ms < 100:
            color = "success"
            status = "Excellent"
        elif latency_ms < 300:
            color = "warning"
            status = "Good"
        else:
            color = "error"
            status = "Poor"
        
        embed = self.create_embed(
            "Bot Latency",
            f"**{latency_ms}ms** ({status})",
            color
        )
        
        await self.safe_send(ctx, embed=embed)

    # ==================== Fun Commands ====================

    @commands.command(aliases=["8ball"])
    async def eightball(self, ctx: commands.Context, *, question: str):
        """Ask the magic 8-ball a question."""
        responses = [
            "It is certain", "Without a doubt", "Yes definitely", "You may rely on it",
            "As I see it, yes", "Most likely", "Outlook good", "Yes", "Signs point to yes",
            "Reply hazy, try again", "Ask again later", "Better not tell you now",
            "Cannot predict now", "Concentrate and ask again", "Don't count on it",
            "My reply is no", "My sources say no", "Outlook not so good", "Very doubtful"
        ]
        
        fields = [
            ("Question", question, False),
            ("Answer", random.choice(responses), False)
        ]
        
        embed = self.create_embed("Magic 8-Ball", fields=fields, color="info")
        await self.safe_send(ctx, embed=embed)

    @commands.command()
    @commands.guild_only()
    async def poll(self, ctx: commands.Context, *, question: str):
        """Create a simple yes/no poll."""
        embed = self.create_embed(
            "Poll",
            question,
            "info"
        )
        embed.set_footer(text=f"Poll created by {ctx.author}")
        
        message = await self.safe_send(ctx, embed=embed)
        if message:
            await message.add_reaction("✅")
            await message.add_reaction("❌")

    @commands.command()
    @commands.guild_only()
    async def choose(self, ctx: commands.Context, *choices):
        """Let the bot choose between multiple options."""
        if len(choices) < 2:
            error_embed = self.create_embed("Not Enough Options", "Please provide at least 2 choices separated by spaces.", "error")
            await self.safe_send(ctx, embed=error_embed)
            return
        
        choice = random.choice(choices)
        
        fields = [
            ("My Choice", f"**{choice}**", False),
            ("All Options", ", ".join(choices), False)
        ]
        
        embed = self.create_embed("Random Choice", fields=fields, color="info")
        await self.safe_send(ctx, embed=embed)

    @commands.command()
    @commands.guild_only()
    async def coinflip(self, ctx: commands.Context):
        """Flip a coin."""
        result = random.choice(["Heads", "Tails"])
        
        embed = self.create_embed(
            "Coin Flip",
            f"**{result}**",
            "info"
        )
        
        await self.safe_send(ctx, embed=embed)

    @commands.command()
    @commands.guild_only()
    async def dice(self, ctx: commands.Context, sides: int = 6):
        """Roll a dice with specified number of sides."""
        if sides < 2:
            error_embed = self.create_embed("Invalid Dice", "Dice must have at least 2 sides.", "error")
            await self.safe_send(ctx, embed=error_embed)
            return
        
        if sides > 100:
            error_embed = self.create_embed("Too Many Sides", "Maximum 100 sides allowed.", "error")
            await self.safe_send(ctx, embed=error_embed)
            return
        
        result = random.randint(1, sides)
        
        embed = self.create_embed(
            "Dice Roll",
            f"You rolled **{result}** on a {sides}-sided dice!",
            "info"
        )
        
        await self.safe_send(ctx, embed=embed)

    # ==================== Warning System ====================

    @commands.command()
    @commands.guild_only()
    @commands.has_permissions(moderate_members=True)
    async def warn(self, ctx: commands.Context, member: discord.Member, *, reason: str):
        """Warn a member."""
        if not await self._can_moderate(ctx, member):
            return
        
        async with self.config.guild(ctx.guild).warnings() as warnings:
            if str(member.id) not in warnings:
                warnings[str(member.id)] = []
            
            warning = {
                "reason": reason,
                "moderator": ctx.author.id,
                "timestamp": datetime.utcnow().isoformat()
            }
            warnings[str(member.id)].append(warning)
            warn_count = len(warnings[str(member.id)])
        
        # Notify user
        try:
            user_embed = self.create_embed(
                f"Warning in {ctx.guild.name}",
                f"**Reason:** {reason}\n**Total Warnings:** {warn_count}",
                "warning"
            )
            await member.send(embed=user_embed)
        except (discord.Forbidden, discord.HTTPException):
            pass
        
        success_embed = self.create_embed(
            "Member Warned",
            f"**{member}** has been warned. This is warning #{warn_count}.\n**Reason:** {reason}",
            "success"
        )
        await self.safe_send(ctx, embed=success_embed)

    @commands.command()
    @commands.guild_only()
    @commands.has_permissions(moderate_members=True)
    async def warnings(self, ctx: commands.Context, member: discord.Member):
        """View all warnings for a member."""
        all_warnings = await self.config.guild(ctx.guild).warnings()
        user_warnings = all_warnings.get(str(member.id), [])
        
        if not user_warnings:
            embed = self.create_embed("No Warnings", f"**{member}** has no warnings.", "success")
            await self.safe_send(ctx, embed=embed)
            return
        
        fields = []
        for i, warning in enumerate(user_warnings, 1):
            mod = ctx.guild.get_member(warning["moderator"])
            mod_name = mod.mention if mod else f"Unknown (ID: {warning['moderator']})"
            timestamp = datetime.fromisoformat(warning["timestamp"])
            
            fields.append((
                f"Warning #{i}",
                f"**Reason:** {warning['reason']}\n**Moderator:** {mod_name}\n**Date:** <t:{int(timestamp.timestamp())}:R>",
                False
            ))
        
        embed = self.create_embed(f"Warnings for {member}", fields=fields, color="warning")
        embed.set_footer(text=f"Total warnings: {len(user_warnings)}")
        
        await self.safe_send(ctx, embed=embed)

    @commands.command()
    @commands.guild_only()
    @commands.has_permissions(administrator=True)
    async def clearwarnings(self, ctx: commands.Context, member: discord.Member):
        """Clear all warnings for a member."""
        async with self.config.guild(ctx.guild).warnings() as warnings:
            if str(member.id) in warnings:
                count = len(warnings[str(member.id)])
                del warnings[str(member.id)]
                
                success_embed = self.create_embed(
                    "Warnings Cleared",
                    f"Cleared {count} warning(s) for **{member}**.",
                    "success"
                )
                await self.safe_send(ctx, embed=success_embed)
            else:
                error_embed = self.create_embed("No Warnings", f"**{member}** has no warnings to clear.", "warning")
                await self.safe_send(ctx, embed=error_embed)

    # ==================== AutoMod Configuration ====================

    @commands.group(name="automod", invoke_without_command=True)
    @commands.has_permissions(administrator=True)
    @commands.guild_only()
    async def automod(self, ctx: commands.Context):
        """Configure automatic moderation settings."""
        await ctx.send_help(ctx.command)

    @automod.command(name="toggle")
    async def automod_toggle(self, ctx: commands.Context):
        """Toggle automod on/off."""
        current = await self.config.guild(ctx.guild).automod_enabled()
        new_status = not current
        await self.config.guild(ctx.guild).automod_enabled.set(new_status)
        
        status = "enabled" if new_status else "disabled"
        color = "success" if new_status else "warning"
        
        embed = self.create_embed("AutoMod Updated", f"AutoMod has been **{status}**.", color)
        await self.safe_send(ctx, embed=embed)

    @automod.command(name="invites")
    async def automod_invites(self, ctx: commands.Context):
        """Toggle invite link filtering."""
        current = await self.config.guild(ctx.guild).filter_invites()
        new_status = not current
        await self.config.guild(ctx.guild).filter_invites.set(new_status)
        
        status = "enabled" if new_status else "disabled"
        color = "success" if new_status else "warning"
        
        embed = self.create_embed("Invite Filter Updated", f"Invite filtering has been **{status}**.", color)
        await self.safe_send(ctx, embed=embed)

    @automod.command(name="links")
    async def automod_links(self, ctx: commands.Context):
        """Toggle external link filtering."""
        current = await self.config.guild(ctx.guild).filter_links()
        new_status = not current
        await self.config.guild(ctx.guild).filter_links.set(new_status)
        
        status = "enabled" if new_status else "disabled"
        color = "success" if new_status else "warning"
        
        embed = self.create_embed("Link Filter Updated", f"Link filtering has been **{status}**.", color)
        await self.safe_send(ctx, embed=embed)

    @automod.command(name="mentions")
    async def automod_mentions(self, ctx: commands.Context, limit: int):
        """Set the mass mention limit (1-10)."""
        if limit < 1 or limit > 10:
            error_embed = self.create_embed("Invalid Limit", "Mention limit must be between 1 and 10.", "error")
            await self.safe_send(ctx, embed=error_embed)
            return
        
        await self.config.guild(ctx.guild).mass_mention_limit.set(limit)
        
        success_embed = self.create_embed("Mention Limit Updated", f"Mass mention limit set to **{limit}**.", "success")
        await self.safe_send(ctx, embed=success_embed)

    @automod.command(name="settings")
    async def automod_settings(self, ctx: commands.Context):
        """View current automod settings."""
        config = await self.config.guild(ctx.guild).all()
        
        def format_status(enabled: bool) -> str:
            return "Enabled" if enabled else "Disabled"
        
        fields = [
            ("AutoMod Status", format_status(config["automod_enabled"]), True),
            ("Filter Invites", format_status(config["filter_invites"]), True),
            ("Filter Links", format_status(config["filter_links"]), True),
            ("Spam Threshold", f"{config['spam_threshold']} msgs/10s", True),
            ("Mass Mention Limit", str(config["mass_mention_limit"]), True),
        ]
        
        embed = self.create_embed("AutoMod Settings", fields=fields, color="info")
        await self.safe_send(ctx, embed=embed)

    # ==================== Message Block Commands ====================
    
    @commands.group(name="msgblock", invoke_without_command=True)
    @commands.is_owner()
    @commands.guild_only()
    async def msgblock(self, ctx: commands.Context):
        """Manage users whose messages are automatically deleted."""
        await ctx.send_help(ctx.command)

    @msgblock.command(name="add")
    async def msgblock_add(self, ctx: commands.Context, user_id: int):
        """Add a user to the message deletion list."""
        if user_id <= 0:
            error_embed = self.create_embed("Invalid User ID", "User ID must be a positive number.", "error")
            await self.safe_send(ctx, embed=error_embed)
            return
        
        async with self.config.guild(ctx.guild).blocked_users() as blocked_users:
            if user_id in blocked_users:
                error_embed = self.create_embed("Already Blocked", f"User ID `{user_id}` is already blocked.", "warning")
                await self.safe_send(ctx, embed=error_embed)
                return
            
            blocked_users.append(user_id)
        
        success_embed = self.create_embed("User Blocked", f"Added user ID `{user_id}` to the message deletion list.", "success")
        await self.safe_send(ctx, embed=success_embed)

    @msgblock.command(name="remove")
    async def msgblock_remove(self, ctx: commands.Context, user_id: int):
        """Remove a user from the message deletion list."""
        async with self.config.guild(ctx.guild).blocked_users() as blocked_users:
            if user_id not in blocked_users:
                error_embed = self.create_embed("Not Blocked", f"User ID `{user_id}` is not blocked.", "warning")
                await self.safe_send(ctx, embed=error_embed)
                return
            
            blocked_users.remove(user_id)
        
        success_embed = self.create_embed("User Unblocked", f"Removed user ID `{user_id}` from the message deletion list.", "success")
        await self.safe_send(ctx, embed=success_embed)

    @msgblock.command(name="list")
    async def msgblock_list(self, ctx: commands.Context):
        """Show all blocked users."""
        blocked_users = await self.config.guild(ctx.guild).blocked_users()
        
        if not blocked_users:
            embed = self.create_embed("No Blocked Users", "The message deletion list is empty.", "info")
            await self.safe_send(ctx, embed=embed)
            return
        
        user_list = []
        for user_id in blocked_users:
            member = ctx.guild.get_member(user_id)
            if member:
                user_list.append(f"• {member.mention} (`{user_id}`)")
            else:
                user_list.append(f"• `{user_id}` (Not in server)")
        
        # Split into chunks if too long
        description = "\n".join(user_list)
        if len(description) > 4000:
            description = description[:4000] + f"\n... and {len(blocked_users) - description.count('•')} more"
        
        embed = self.create_embed("Blocked Users", description, "info")
        embed.set_footer(text=f"Total: {len(blocked_users)} user(s)")
        
        await self.safe_send(ctx, embed=embed)

    # ==================== Hidden Fun Commands ====================

    @commands.command(hidden=True)
    async def thanos(self, ctx: commands.Context):
        """Display Thanos image."""
        embed = discord.Embed(color=0x800080)
        embed.set_image(url="https://cdn.discordapp.com/attachments/1069748983293022249/1425583704532848721/6LpanIV.png")
        await self.safe_send(ctx, embed=embed)

    @commands.command(hidden=True)
    @commands.guild_only()
    async def hawk(self, ctx: commands.Context, user: Optional[discord.Member] = None):
        """Ask a user if they're a hawk."""
        hawk_enabled = await self.config.guild(ctx.guild).hawk_enabled()
        if not hawk_enabled:
            embed = discord.Embed(color=0xED4245)
            embed.set_image(url="https://cdn.discordapp.com/attachments/1069748983293022249/1425831928644501624/4rMETw3.gif?ex=68ef9c76&is=68ee4af6&hm=39b6924ec16d99466f581f6f85427430d72d646729aa82566aa87e2b4ad24b3f&")
            embed.description = "The hawk command is currently disabled."
            await self.safe_send(ctx, embed=embed)
            return
        
        hawk_users = await self.config.guild(ctx.guild).hawk_users()
        
        if user is None:
            if not hawk_users:
                error_embed = self.create_embed("No Hawk Users", "No users in the hawk list!", "error")
                await self.safe_send(ctx, embed=error_embed)
                return
            
            available_users = hawk_users.copy()
            if len(hawk_users) > 1 and ctx.guild.id in self.last_hawk_user:
                last_user = self.last_hawk_user[ctx.guild.id]
                if last_user in available_users:
                    available_users.remove(last_user)
            
            random_user_id = random.choice(available_users)
            user = ctx.guild.get_member(random_user_id)
            
            if not user:
                error_embed = self.create_embed("User Not Found", f"User ID `{random_user_id}` is not in this server.", "error")
                await self.safe_send(ctx, embed=error_embed)
                return
            
            self.last_hawk_user[ctx.guild.id] = random_user_id
        
        self.awaiting_hawk_response[ctx.guild.id] = user.id
        allowed_mentions = discord.AllowedMentions(users=True)
        await self.safe_send(ctx, f"{user.mention} Are you a hawk?", allowed_mentions=allowed_mentions)

    @commands.command(hidden=True)
    @commands.guild_only()
    async def gay(self, ctx: commands.Context, user: Optional[discord.Member] = None):
        """Check how gay someone is."""
        gay_enabled = await self.config.guild(ctx.guild).gay_enabled()
        if not gay_enabled:
            embed = discord.Embed(color=0xED4245)
            embed.set_image(url="https://cdn.discordapp.com/attachments/1069748983293022249/1425831928644501624/4rMETw3.gif?ex=68ef9c76&is=68ee4af6&hm=39b6924ec16d99466f581f6f85427430d72d646729aa82566aa87e2b4ad24b3f&")
            embed.description = "The gay command is currently disabled."
            await self.safe_send(ctx, embed=embed)
            return
        
        if user is None:
            error_embed = self.create_embed("Missing User", "Please mention a user!", "error")
            await self.safe_send(ctx, embed=error_embed)
            return
        
        hawk_users = await self.config.guild(ctx.guild).hawk_users()
        
        if user.id in hawk_users:
            percentage = random.randint(GAY_PERCENTAGE_MIN_HAWK, GAY_PERCENTAGE_MAX_HAWK)
        else:
            percentage = random.randint(GAY_PERCENTAGE_MIN_NORMAL, GAY_PERCENTAGE_MAX_NORMAL)
        
        allowed_mentions = discord.AllowedMentions(users=True)
        await self.safe_send(ctx, f"{user.mention} is {percentage}% gay", allowed_mentions=allowed_mentions)

    @commands.command(hidden=True)
    @commands.is_owner()
    @commands.guild_only()
    async def spamping(self, ctx: commands.Context, user: discord.Member, amount: Optional[int] = DEFAULT_PING_AMOUNT):
        """Ping a user multiple times."""
        if amount < 1:
            error_embed = self.create_embed("Invalid Amount", "Amount must be at least 1.", "error")
            await self.safe_send(ctx, embed=error_embed)
            return
        
        if amount > MAX_PING_AMOUNT:
            error_embed = self.create_embed("Amount Too High", f"Amount cannot exceed {MAX_PING_AMOUNT}.", "error")
            await self.safe_send(ctx, embed=error_embed)
            return
        
        allowed_mentions = discord.AllowedMentions(users=True)
        
        start_embed = self.create_embed("Spam Ping Started", f"Pinging {user.mention} {amount} time(s)...", "info")
        await self.safe_send(ctx, embed=start_embed, allowed_mentions=allowed_mentions)
        
        successful_pings = 0
        for i in range(amount):
            try:
                await self.safe_send(ctx, user.mention, allowed_mentions=allowed_mentions)
                successful_pings += 1
                if i < amount - 1:
                    await asyncio.sleep(PING_DELAY)
            except Exception as e:
                log.error(f"Error during spamping: {e}")
                break
        
        end_embed = self.create_embed(
            "Spam Ping Complete",
            f"Finished pinging {user.mention} ({successful_pings}/{amount} successful).",
            "success"
        )
        await self.safe_send(ctx, embed=end_embed, allowed_mentions=allowed_mentions)

    # ==================== Hawk Management ====================

    @commands.command(hidden=True)
    @commands.is_owner()
    @commands.guild_only()
    async def addhawk(self, ctx: commands.Context, user_id: int):
        """Add a user to the hawk list."""
        if user_id <= 0:
            error_embed = self.create_embed("Invalid User ID", "User ID must be a positive number.", "error")
            await self.safe_send(ctx, embed=error_embed)
            return
        
        async with self.config.guild(ctx.guild).hawk_users() as hawk_users:
            if user_id in hawk_users:
                error_embed = self.create_embed("Already in List", f"User ID `{user_id}` is already in the hawk list.", "warning")
                await self.safe_send(ctx, embed=error_embed)
                return
            hawk_users.append(user_id)
        
        success_embed = self.create_embed("Hawk Added", f"Added user ID `{user_id}` to the hawk list.", "success")
        await self.safe_send(ctx, embed=success_embed)

    @commands.command(hidden=True)
    @commands.is_owner()
    @commands.guild_only()
    async def removehawk(self, ctx: commands.Context, user_id: int):
        """Remove a user from the hawk list."""
        async with self.config.guild(ctx.guild).hawk_users() as hawk_users:
            if user_id not in hawk_users:
                error_embed = self.create_embed("Not in List", f"User ID `{user_id}` is not in the hawk list.", "warning")
                await self.safe_send(ctx, embed=error_embed)
                return
            hawk_users.remove(user_id)
        
        success_embed = self.create_embed("Hawk Removed", f"Removed user ID `{user_id}` from the hawk list.", "success")
        await self.safe_send(ctx, embed=success_embed)

    @commands.command(hidden=True)
    @commands.is_owner()
    @commands.guild_only()
    async def listhawk(self, ctx: commands.Context):
        """List all users in the hawk list."""
        hawk_users = await self.config.guild(ctx.guild).hawk_users()
        
        if not hawk_users:
            embed = self.create_embed("No Hawk Users", "The hawk list is empty.", "info")
            await self.safe_send(ctx, embed=embed)
            return
        
        user_list = []
        for user_id in hawk_users:
            member = ctx.guild.get_member(user_id)
            if member:
                user_list.append(f"• {member.mention} (`{user_id}`)")
            else:
                user_list.append(f"• `{user_id}` (Not in server)")
        
        description = "\n".join(user_list)
        if len(description) > 4000:
            description = description[:4000] + f"\n... and {len(hawk_users) - description.count('•')} more"
        
        embed = self.create_embed("Hawk Users", description, "info")
        embed.set_footer(text=f"Total: {len(hawk_users)} user(s)")
        
        await self.safe_send(ctx, embed=embed)

    @commands.command(hidden=True)
    @commands.is_owner()
    @commands.guild_only()
    async def disablehawk(self, ctx: commands.Context):
        """Toggle the hawk command on/off."""
        hawk_enabled = await self.config.guild(ctx.guild).hawk_enabled()
        new_status = not hawk_enabled
        await self.config.guild(ctx.guild).hawk_enabled.set(new_status)
        
        status_text = "enabled" if new_status else "disabled"
        
        if new_status:
            # Show the green "enabled" image
            embed = discord.Embed(color=0x57F287)
            embed.set_image(url="https://cdn.discordapp.com/attachments/1069748983293022249/1425831721160540281/NzusuSn.png?ex=68ef9c44&is=68ee4ac4&hm=e97e9983b9d353846965007409b69c50f696589f21fe423e257d6e43e61972cb&")
            embed.description = f"Hawk command is now **{status_text}**."
        else:
            # Just show text for disabled
            embed = self.create_embed("Hawk Command Updated", f"Hawk command is now **{status_text}**.", "warning")
        
        await self.safe_send(ctx, embed=embed)

    @commands.command(hidden=True)
    @commands.is_owner()
    @commands.guild_only()
    async def disablegay(self, ctx: commands.Context):
        """Toggle the gay command on/off."""
        gay_enabled = await self.config.guild(ctx.guild).gay_enabled()
        new_status = not gay_enabled
        await self.config.guild(ctx.guild).gay_enabled.set(new_status)
        
        status_text = "enabled" if new_status else "disabled"
        
        if new_status:
            # Show the green "enabled" image
            embed = discord.Embed(color=0x57F287)
            embed.set_image(url="https://cdn.discordapp.com/attachments/1069748983293022249/1425831721160540281/NzusuSn.png?ex=68ef9c44&is=68ee4ac4&hm=e97e9983b9d353846965007409b69c50f696589f21fe423e257d6e43e61972cb&")
            embed.description = f"Gay command is now **{status_text}**."
        else:
            # Just show text for disabled
            embed = self.create_embed("Gay Command Updated", f"Gay command is now **{status_text}**.", "warning")
        
        await self.safe_send(ctx, embed=embed)


async def setup(bot):
    """Load the MessageDelete cog."""
    await bot.add_cog(MessageDelete(bot))
