"""MessageDelete - High-performance Discord bot with comprehensive moderation and utilities."""

from redbot.core import commands, Config
from redbot.core.utils.chat_formatting import humanize_timedelta
import discord
import random
import asyncio
import logging
import re
from typing import Optional, Union, Dict, List, Tuple
from datetime import datetime, timedelta
from functools import lru_cache

# Configure logging
log = logging.getLogger("red.messagedelete")

# Constants
DEFAULT_PING_AMOUNT = 5
MAX_PING_AMOUNT = 20
PING_DELAY = 0.5
GAY_PERCENTAGE_MIN_NORMAL = 0
GAY_PERCENTAGE_MAX_NORMAL = 100
GAY_PERCENTAGE_MIN_HAWK = 51
GAY_PERCENTAGE_MAX_HAWK = 150
MAX_PURGE_LIMIT = 1000
MAX_MASSBAN_USERS = 20


def mod_or_permissions(**perms):
    """Custom check: requires BOTH custom mod status AND Discord permissions."""
    async def predicate(ctx: commands.Context):
        if ctx.guild is None:
            return False
        
        # Owner always has access (bypass all checks)
        if await ctx.bot.is_owner(ctx.author):
            return True
        
        # Server owner always has access (bypass all checks)
        if ctx.author == ctx.guild.owner:
            return True
        
        # Check if user is in custom moderator list
        cog = ctx.bot.get_cog("Utilities")
        is_custom_mod = False
        if cog:
            moderators = await cog.config.guild(ctx.guild).moderators()
            is_custom_mod = ctx.author.id in moderators
        
        # Check Discord permissions
        has_perms = await commands.has_permissions(**perms).predicate(ctx)
        
        # Custom moderators need BOTH to be on the list AND have Discord permissions
        if is_custom_mod and has_perms:
            return True
        
        # Regular users just need Discord permissions
        return has_perms
    
    return commands.check(predicate)


class MessageDelete(commands.Cog):
    """High-performance Discord bot with comprehensive moderation and utilities."""

    __author__ = ["YourName"]
    __version__ = "5.3.0"
    __cog_name__ = "Utilities"

    def __init__(self, bot):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=1234567890, force_registration=True)
        
        default_guild = {
            "blocked_users": [],
            "moderators": [],
            "hawk_users": [
                786624423721041941, 500641384835842049, 275549294969356288,
                685961799518257175, 871044256800854078, 332176051914539010
            ],
            "hawk_enabled": True,
            "gay_enabled": True,
            "warnings": {},
            "tempbans": {}
        }
        self.config.register_guild(**default_guild)
        
        # Runtime state
        self.awaiting_hawk_response: Dict[int, int] = {}
        self.last_hawk_user: Dict[int, int] = {}
        
        # Start background task
        self.tempban_task = self.bot.loop.create_task(self.check_tempbans())

    def cog_unload(self):
        """Cleanup when cog is unloaded."""
        if self.tempban_task:
            self.tempban_task.cancel()

    def format_help_for_context(self, ctx: commands.Context) -> str:
        """Show cog version in help."""
        return f"{super().format_help_for_context(ctx)}\n\nVersion: {self.__version__}"

    async def check_tempbans(self):
        """Background task to check and remove expired tempbans."""
        await self.bot.wait_until_ready()
        while not self.bot.is_closed():
            try:
                for guild in self.bot.guilds:
                    tempbans = await self.config.guild(guild).tempbans()
                    now = datetime.utcnow()
                    
                    to_unban = []
                    for user_id_str, ban_info in tempbans.items():
                        unban_time = datetime.fromisoformat(ban_info["unban_time"])
                        if now >= unban_time:
                            to_unban.append(int(user_id_str))
                    
                    for user_id in to_unban:
                        try:
                            user = await self.bot.fetch_user(user_id)
                            await guild.unban(user, reason="Temporary ban expired")
                            log.info(f"Auto-unbanned user {user_id} from guild {guild.id}")
                            
                            async with self.config.guild(guild).tempbans() as tempbans_dict:
                                del tempbans_dict[str(user_id)]
                        except Exception as e:
                            log.error(f"Error auto-unbanning user {user_id}: {e}")
                
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error(f"Error in tempban checker: {e}")
                await asyncio.sleep(60)

    @lru_cache(maxsize=128)
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

    @commands.Cog.listener()
    async def on_command_error(self, ctx: commands.Context, error: Exception):
        """Comprehensive error handling for all commands."""
        if hasattr(ctx.command, 'on_error'):
            return

        error_embed = None
        
        if isinstance(error, commands.CommandNotFound):
            return
        
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
        
        elif isinstance(error, commands.CheckFailure):
            error_embed = self.create_embed(
                "Permission Denied",
                "You don't have permission to use this command.",
                "error"
            )
        
        else:
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
        """Handle hawk responses and blocked user messages."""
        if not message.guild or message.author.bot:
            return
        
        guild_id = message.guild.id
        
        # Hawk response handling
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
        except (discord.Forbidden, discord.HTTPException) as e:
            log.error(f"Error deleting blocked user message: {e}")

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
        time_units = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
        
        if len(duration) < 2:
            raise ValueError("Duration must be at least 2 characters (e.g., '5m')")
        
        unit = duration[-1].lower()
        if unit not in time_units:
            raise ValueError("Invalid time unit. Use: s (seconds), m (minutes), h (hours), d (days), w (weeks)")
        
        try:
            amount = int(duration[:-1])
        except ValueError:
            raise ValueError("Invalid duration format. Example: 10m, 2h, 1d")
        
        if amount <= 0:
            raise ValueError("Duration must be a positive number")
        
        return timedelta(seconds=amount * time_units[unit])

    # ==================== Moderator Management ====================

    @commands.group(name="modset", invoke_without_command=True)
    @commands.guild_only()
    @commands.admin_or_permissions(administrator=True)
    async def modset(self, ctx: commands.Context):
        """Manage custom moderator permissions for this server.
        
        Users added to the moderator list must still have the required Discord permissions
        to use moderation commands. This provides an additional layer of verification.
        """
        await ctx.send_help(ctx.command)

    @modset.command(name="add")
    async def modset_add(self, ctx: commands.Context, user: Union[discord.Member, int]):
        """Add a user to the custom moderator list.
        
        Note: Users still need the appropriate Discord permissions to use mod commands.
        
        **Arguments:**
        - `<user>` - User to add (mention or ID)
        
        **Examples:**
        - `[p]modset add @User`
        - `[p]modset add 123456789012345678`
        
        **Required Permissions:**
        - You: Administrator
        """
        user_id = user.id if isinstance(user, discord.Member) else user
        
        async with self.config.guild(ctx.guild).moderators() as moderators:
            if user_id in moderators:
                error_embed = self.create_embed("Already a Moderator", f"User ID `{user_id}` is already in the moderator list.", "warning")
                await self.safe_send(ctx, embed=error_embed)
                return
            
            moderators.append(user_id)
        
        user_mention = user.mention if isinstance(user, discord.Member) else f"User ID `{user_id}`"
        success_embed = self.create_embed(
            "Moderator Added",
            f"{user_mention} has been added to the custom moderator list.\n\nNote: They still need appropriate Discord permissions to use mod commands.",
            "success"
        )
        await self.safe_send(ctx, embed=success_embed)

    @modset.command(name="remove")
    async def modset_remove(self, ctx: commands.Context, user: Union[discord.Member, int]):
        """Remove a user from the custom moderator list.
        
        **Arguments:**
        - `<user>` - User to remove (mention or ID)
        
        **Examples:**
        - `[p]modset remove @User`
        - `[p]modset remove 123456789012345678`
        
        **Required Permissions:**
        - You: Administrator
        """
        user_id = user.id if isinstance(user, discord.Member) else user
        
        async with self.config.guild(ctx.guild).moderators() as moderators:
            if user_id not in moderators:
                error_embed = self.create_embed("Not a Moderator", f"User ID `{user_id}` is not in the moderator list.", "warning")
                await self.safe_send(ctx, embed=error_embed)
                return
            
            moderators.remove(user_id)
        
        user_mention = user.mention if isinstance(user, discord.Member) else f"User ID `{user_id}`"
        success_embed = self.create_embed(
            "Moderator Removed",
            f"{user_mention} has been removed from the custom moderator list.",
            "success"
        )
        await self.safe_send(ctx, embed=success_embed)

    @modset.command(name="list")
    async def modset_list(self, ctx: commands.Context):
        """Show all users in the custom moderator list.
        
        **Examples:**
        - `[p]modset list`
        
        **Required Permissions:**
        - You: Administrator
        """
        moderators = await self.config.guild(ctx.guild).moderators()
        
        if not moderators:
            embed = self.create_embed("No Custom Moderators", "The custom moderator list is empty.", "info")
            await self.safe_send(ctx, embed=embed)
            return
        
        mod_list = []
        for user_id in moderators:
            member = ctx.guild.get_member(user_id)
            if member:
                mod_list.append(f"• {member.mention} (`{member}` - `{user_id}`)")
            else:
                mod_list.append(f"• `{user_id}` (Not in server)")
        
        description = "\n".join(mod_list)
        if len(description) > 4000:
            description = description[:4000] + f"\n... and {len(moderators) - description.count('•')} more"
        
        embed = self.create_embed("Custom Moderators", description, "info")
        embed.set_footer(text=f"Total: {len(moderators)} moderator(s)")
        
        await self.safe_send(ctx, embed=embed)

    # ==================== Core Moderation Commands ====================
    
    @commands.command()
    @commands.guild_only()
    @mod_or_permissions(kick_members=True)
    @commands.bot_has_permissions(kick_members=True)
    async def kick(self, ctx: commands.Context, member: discord.Member, *, reason: Optional[str] = None):
        """Kick a member from the server.
        
        Removes a member from the server without banning them. They can rejoin with a new invite.
        
        **Arguments:**
        - `<member>` - The member to kick (mention, ID, or name)
        - `[reason]` - Optional reason for the kick (shown in audit log and DM)
        
        **Examples:**
        - `[p]kick @BadUser Spamming in chat`
        - `[p]kick 123456789012345678 Breaking rules`
        
        **Required Permissions:**
        - You: Kick Members (+ Custom Moderator if applicable)
        - Bot: Kick Members
        """
        if not await self._can_moderate(ctx, member):
            return
        
        reason = reason or "No reason provided"
        
        try:
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
    @mod_or_permissions(ban_members=True)
    @commands.bot_has_permissions(ban_members=True)
    async def ban(self, ctx: commands.Context, user: Union[discord.Member, int], delete_days: int = 1, *, reason: Optional[str] = None):
        """Ban a user from the server and optionally delete their recent messages.
        
        Permanently bans a user from the server. They cannot rejoin unless unbanned.
        
        **Arguments:**
        - `<user>` - The user to ban (mention, ID, or name - works even if not in server)
        - `[delete_days]` - Days of messages to delete (0-7, default: 1)
        - `[reason]` - Optional reason for the ban
        
        **Examples:**
        - `[p]ban @Spammer 7 Persistent rule violations`
        - `[p]ban 123456789012345678 0 Alt account`
        
        **Required Permissions:**
        - You: Ban Members (+ Custom Moderator if applicable)
        - Bot: Ban Members
        """
        # Handle user ID as int
        if isinstance(user, int):
            try:
                user = await self.bot.fetch_user(user)
            except discord.NotFound:
                error_embed = self.create_embed("Error", "User not found.", "error")
                await self.safe_send(ctx, embed=error_embed)
                return
        
        if isinstance(user, discord.Member) and not await self._can_moderate(ctx, user):
            return
        
        if not 0 <= delete_days <= 7:
            error_embed = self.create_embed("Invalid Days", "Delete days must be between 0 and 7.", "error")
            await self.safe_send(ctx, embed=error_embed)
            return
        
        reason = reason or "No reason provided"
        
        try:
            try:
                embed = self.create_embed(
                    f"Banned from {ctx.guild.name}",
                    f"**Reason:** {reason}",
                    "error"
                )
                await user.send(embed=embed)
            except (discord.Forbidden, discord.HTTPException, AttributeError):
                pass

            await ctx.guild.ban(user, reason=f"{ctx.author}: {reason}", delete_message_days=delete_days)
            
            success_embed = self.create_embed(
                "Member Banned",
                f"**{user}** has been banned.\n**Messages deleted:** {delete_days} day(s)\n**Reason:** {reason}",
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
    @mod_or_permissions(ban_members=True)
    @commands.bot_has_permissions(ban_members=True)
    async def softban(self, ctx: commands.Context, member: discord.Member, *, reason: Optional[str] = None):
        """Kick a user and delete 1 day's worth of their messages.
        
        Bans then immediately unbans a user to delete their recent messages while allowing them to rejoin.
        
        **Arguments:**
        - `<member>` - The member to softban
        - `[reason]` - Optional reason for the softban
        
        **Examples:**
        - `[p]softban @Spammer Posted inappropriate content`
        
        **Required Permissions:**
        - You: Ban Members (+ Custom Moderator if applicable)
        - Bot: Ban Members
        """
        if not await self._can_moderate(ctx, member):
            return
        
        reason = reason or "No reason provided"
        
        try:
            try:
                embed = self.create_embed(
                    f"Softbanned from {ctx.guild.name}",
                    f"You have been removed and your messages deleted.\n**Reason:** {reason}\n\nYou may rejoin the server.",
                    "warning"
                )
                await member.send(embed=embed)
            except (discord.Forbidden, discord.HTTPException):
                pass

            await ctx.guild.ban(member, reason=f"Softban by {ctx.author}: {reason}", delete_message_days=1)
            await ctx.guild.unban(member, reason=f"Softban unban by {ctx.author}")
            
            success_embed = self.create_embed(
                "Member Softbanned",
                f"**{member}** has been softbanned (kicked with message deletion).\n**Reason:** {reason}",
                "success"
            )
            await self.safe_send(ctx, embed=success_embed)
            
        except discord.Forbidden:
            error_embed = self.create_embed("Error", "I don't have permission to ban/unban that member.", "error")
            await self.safe_send(ctx, embed=error_embed)
        except discord.HTTPException as e:
            error_embed = self.create_embed("Error", f"Failed to softban member: {e}", "error")
            await self.safe_send(ctx, embed=error_embed)

    @commands.command()
    @commands.guild_only()
    @mod_or_permissions(ban_members=True)
    @commands.bot_has_permissions(ban_members=True)
    async def tempban(self, ctx: commands.Context, user: Union[discord.Member, int], duration: str, *, reason: Optional[str] = None):
        """Temporarily ban a user from the server.
        
        Bans a user for a specified time period. They will be automatically unbanned when it expires.
        
        **Arguments:**
        - `<user>` - The user to temporarily ban
        - `<duration>` - Duration of the ban (e.g., 30m, 2h, 1d, 7d)
        - `[reason]` - Optional reason for the ban
        
        **Examples:**
        - `[p]tempban @BadUser 24h Needs cooldown period`
        - `[p]tempban 123456789012345678 7d Temporary suspension`
        
        **Required Permissions:**
        - You: Ban Members (+ Custom Moderator if applicable)
        - Bot: Ban Members
        """
        # Handle user ID as int
        if isinstance(user, int):
            try:
                user = await self.bot.fetch_user(user)
            except discord.NotFound:
                error_embed = self.create_embed("Error", "User not found.", "error")
                await self.safe_send(ctx, embed=error_embed)
                return
        
        if isinstance(user, discord.Member) and not await self._can_moderate(ctx, user):
            return
        
        try:
            delta = self._parse_duration(duration)
        except ValueError as e:
            error_embed = self.create_embed("Invalid Duration", str(e), "error")
            await self.safe_send(ctx, embed=error_embed)
            return
        
        reason = reason or "No reason provided"
        unban_time = datetime.utcnow() + delta
        
        try:
            try:
                embed = self.create_embed(
                    f"Temporarily Banned from {ctx.guild.name}",
                    f"**Duration:** {duration}\n**Unbanned:** <t:{int(unban_time.timestamp())}:R>\n**Reason:** {reason}",
                    "warning"
                )
                await user.send(embed=embed)
            except (discord.Forbidden, discord.HTTPException, AttributeError):
                pass

            await ctx.guild.ban(user, reason=f"Tempban by {ctx.author}: {reason}", delete_message_days=1)
            
            async with self.config.guild(ctx.guild).tempbans() as tempbans:
                tempbans[str(user.id)] = {
                    "unban_time": unban_time.isoformat(),
                    "reason": reason,
                    "moderator": ctx.author.id
                }
            
            success_embed = self.create_embed(
                "Member Temporarily Banned",
                f"**{user}** has been banned for {duration}.\n**Unbanned:** <t:{int(unban_time.timestamp())}:R>\n**Reason:** {reason}",
                "success"
            )
            await self.safe_send(ctx, embed=success_embed)
            
        except discord.Forbidden:
            error_embed = self.create_embed("Error", "I don't have permission to ban that user.", "error")
            await self.safe_send(ctx, embed=error_embed)
        except discord.HTTPException as e:
            error_embed = self.create_embed("Error", f"Failed to tempban user: {e}", "error")
            await self.safe_send(ctx, embed=error_embed)

    @commands.command()
    @commands.guild_only()
    @mod_or_permissions(ban_members=True)
    @commands.bot_has_permissions(ban_members=True)
    async def unban(self, ctx: commands.Context, user_id: int, *, reason: Optional[str] = None):
        """Unban a user from the server.
        
        Removes a ban, allowing the user to rejoin the server.
        
        **Arguments:**
        - `<user_id>` - The ID of the user to unban
        - `[reason]` - Optional reason for the unban
        
        **Examples:**
        - `[p]unban 123456789012345678 Appeal accepted`
        - `[p]unban 987654321098765432 Ban was a mistake`
        
        **Required Permissions:**
        - You: Ban Members (+ Custom Moderator if applicable)
        - Bot: Ban Members
        """
        reason = reason or "No reason provided"
        
        try:
            user = await self.bot.fetch_user(user_id)
            await ctx.guild.unban(user, reason=f"{ctx.author}: {reason}")
            
            # Remove from tempbans if present
            async with self.config.guild(ctx.guild).tempbans() as tempbans:
                if str(user_id) in tempbans:
                    del tempbans[str(user_id)]
            
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

    @commands.command()
    @commands.guild_only()
    @mod_or_permissions(ban_members=True)
    @commands.bot_has_permissions(ban_members=True)
    async def massban(self, ctx: commands.Context, *user_ids: int, reason: str = None):
        """Mass ban multiple users from the server.
        
        Bans multiple users at once. Useful for handling raids or multiple rule breakers.
        
        **Arguments:**
        - `<user_ids>` - Space-separated list of user IDs to ban (max 20)
        - `[reason]` - Optional reason for the bans
        
        **Examples:**
        - `[p]massban 123456789 987654321 555555555 Raid participants`
        
        **Required Permissions:**
        - You: Ban Members (+ Custom Moderator if applicable)
        - Bot: Ban Members
        """
        if len(user_ids) == 0:
            error_embed = self.create_embed("No Users Provided", "Please provide at least one user ID.", "error")
            await self.safe_send(ctx, embed=error_embed)
            return
        
        if len(user_ids) > MAX_MASSBAN_USERS:
            error_embed = self.create_embed("Too Many Users", f"Maximum {MAX_MASSBAN_USERS} users can be banned at once.", "error")
            await self.safe_send(ctx, embed=error_embed)
            return
        
        reason = reason or "Mass ban - No reason provided"
        
        banned = []
        failed = []
        
        status_msg = await self.safe_send(ctx, f"Banning {len(user_ids)} users...")
        
        for user_id in user_ids:
            try:
                user = await self.bot.fetch_user(user_id)
                await ctx.guild.ban(user, reason=f"Massban by {ctx.author}: {reason}", delete_message_days=1)
                banned.append(f"{user} ({user_id})")
            except discord.NotFound:
                failed.append(f"{user_id} (Not found)")
            except discord.Forbidden:
                failed.append(f"{user_id} (Permission denied)")
            except discord.HTTPException as e:
                failed.append(f"{user_id} (Error: {e})")
        
        if status_msg:
            await status_msg.delete()
        
        fields = []
        if banned:
            banned_text = "\n".join(banned[:10])
            if len(banned) > 10:
                banned_text += f"\n... and {len(banned) - 10} more"
            fields.append(("Successfully Banned", banned_text, False))
        
        if failed:
            failed_text = "\n".join(failed[:10])
            if len(failed) > 10:
                failed_text += f"\n... and {len(failed) - 10} more"
            fields.append(("Failed", failed_text, False))
        
        result_embed = self.create_embed(
            "Mass Ban Complete",
            f"**Total:** {len(user_ids)} | **Banned:** {len(banned)} | **Failed:** {len(failed)}\n**Reason:** {reason}",
            "success" if len(banned) > 0 else "warning",
            fields=fields
        )
        await self.safe_send(ctx, embed=result_embed)

    @commands.command(aliases=["mute"])
    @commands.guild_only()
    @mod_or_permissions(moderate_members=True)
    @commands.bot_has_permissions(moderate_members=True)
    async def timeout(self, ctx: commands.Context, member: discord.Member, duration: str, *, reason: Optional[str] = None):
        """Timeout a member for a specified duration.
        
        Prevents a member from sending messages, reacting, or speaking in voice channels.
        
        **Arguments:**
        - `<member>` - The member to timeout
        - `<duration>` - Duration (e.g., 10m, 2h, 1d - max 28 days)
        - `[reason]` - Optional reason
        
        **Examples:**
        - `[p]timeout @User 10m Spamming`
        - `[p]mute @User 1h Inappropriate behavior`
        
        **Required Permissions:**
        - You: Moderate Members (+ Custom Moderator if applicable)
        - Bot: Moderate Members
        """
        if not await self._can_moderate(ctx, member):
            return
        
        try:
            delta = self._parse_duration(duration)
        except ValueError as e:
            error_embed = self.create_embed("Invalid Duration", str(e), "error")
            await self.safe_send(ctx, embed=error_embed)
            return
        
        if delta.total_seconds() > 2419200:  # 28 days
            error_embed = self.create_embed("Duration Too Long", "Maximum timeout duration is 28 days.", "error")
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
    @mod_or_permissions(moderate_members=True)
    @commands.bot_has_permissions(moderate_members=True)
    async def untimeout(self, ctx: commands.Context, member: discord.Member, *, reason: Optional[str] = None):
        """Remove a timeout from a member.
        
        Restores a member's ability to send messages and participate normally.
        
        **Arguments:**
        - `<member>` - The member to remove timeout from
        - `[reason]` - Optional reason
        
        **Examples:**
        - `[p]untimeout @User Apologized`
        - `[p]unmute @User`
        
        **Required Permissions:**
        - You: Moderate Members (+ Custom Moderator if applicable)
        - Bot: Moderate Members
        """
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
    @mod_or_permissions(manage_messages=True)
    @commands.bot_has_permissions(manage_messages=True, read_message_history=True)
    async def purge(self, ctx: commands.Context, amount: int, member: Optional[discord.Member] = None):
        """Delete multiple messages at once.
        
        Can delete all messages or only messages from a specific user.
        
        **Arguments:**
        - `<amount>` - Number of messages to delete (1-1000)
        - `[member]` - Optional: Only delete messages from this member
        
        **Examples:**
        - `[p]purge 50` - Delete last 50 messages
        - `[p]purge 100 @Spammer` - Delete last 100 messages from a specific user
        
        **Required Permissions:**
        - You: Manage Messages (+ Custom Moderator if applicable)
        - Bot: Manage Messages, Read Message History
        """
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
            count = len(deleted) - 1
            
            target_text = f" from **{member}**" if member else ""
            success_embed = self.create_embed(
                "Messages Purged",
                f"Deleted {count} message(s){target_text}.",
                "success"
            )
            msg = await self.safe_send(ctx, embed=success_embed)
            
            if msg:
                await msg.delete(delay=5)
            
        except discord.Forbidden:
            error_embed = self.create_embed("Error", "I don't have permission to delete messages.", "error")
            await self.safe_send(ctx, embed=error_embed)
        except discord.HTTPException as e:
            error_embed = self.create_embed("Error", f"Failed to purge messages: {e}", "error")
            await self.safe_send(ctx, embed=error_embed)

    @commands.command()
    @commands.guild_only()
    @mod_or_permissions(manage_nicknames=True)
    @commands.bot_has_permissions(manage_nicknames=True)
    async def rename(self, ctx: commands.Context, member: discord.Member, *, nickname: str = None):
        """Change a member's server nickname.
        
        Sets or removes a member's nickname on the server.
        
        **Arguments:**
        - `<member>` - The member to rename
        - `[nickname]` - New nickname (leave empty to remove nickname)
        
        **Examples:**
        - `[p]rename @User NewNickname`
        - `[p]rename @User` - Remove nickname
        
        **Required Permissions:**
        - You: Manage Nicknames (+ Custom Moderator if applicable)
        - Bot: Manage Nicknames
        """
        if not await self._can_moderate(ctx, member):
            return
        
        old_nick = member.nick or member.name
        
        try:
            await member.edit(nick=nickname, reason=f"Nickname changed by {ctx.author}")
            
            if nickname:
                success_embed = self.create_embed(
                    "Nickname Changed",
                    f"**{member}**'s nickname has been changed.\n**Old:** {old_nick}\n**New:** {nickname}",
                    "success"
                )
            else:
                success_embed = self.create_embed(
                    "Nickname Removed",
                    f"**{member}**'s nickname has been removed.\n**Old nickname:** {old_nick}",
                    "success"
                )
            
            await self.safe_send(ctx, embed=success_embed)
            
        except discord.Forbidden:
            error_embed = self.create_embed("Error", "I don't have permission to change that member's nickname.", "error")
            await self.safe_send(ctx, embed=error_embed)
        except discord.HTTPException as e:
            error_embed = self.create_embed("Error", f"Failed to change nickname: {e}", "error")
            await self.safe_send(ctx, embed=error_embed)

    @commands.command()
    @commands.guild_only()
    @mod_or_permissions(move_members=True)
    @commands.bot_has_permissions(move_members=True)
    async def voicekick(self, ctx: commands.Context, member: discord.Member, *, reason: Optional[str] = None):
        """Kick a member from their current voice channel.
        
        Disconnects a member from the voice channel they're currently in.
        
        **Arguments:**
        - `<member>` - The member to kick from voice
        - `[reason]` - Optional reason
        
        **Examples:**
        - `[p]voicekick @User Disrupting voice chat`
        
        **Required Permissions:**
        - You: Move Members (+ Custom Moderator if applicable)
        - Bot: Move Members
        """
        if not await self._can_moderate(ctx, member):
            return
        
        if not member.voice:
            error_embed = self.create_embed("Error", f"**{member}** is not in a voice channel.", "error")
            await self.safe_send(ctx, embed=error_embed)
            return
        
        reason = reason or "No reason provided"
        
        try:
            await member.move_to(None, reason=f"Voice kicked by {ctx.author}: {reason}")
            
            success_embed = self.create_embed(
                "Member Voice Kicked",
                f"**{member}** has been kicked from voice.\n**Reason:** {reason}",
                "success"
            )
            await self.safe_send(ctx, embed=success_embed)
            
        except discord.Forbidden:
            error_embed = self.create_embed("Error", "I don't have permission to move that member.", "error")
            await self.safe_send(ctx, embed=error_embed)
        except discord.HTTPException as e:
            error_embed = self.create_embed("Error", f"Failed to voice kick member: {e}", "error")
            await self.safe_send(ctx, embed=error_embed)

    @commands.command()
    @commands.guild_only()
    @mod_or_permissions(mute_members=True, deafen_members=True)
    @commands.bot_has_permissions(mute_members=True, deafen_members=True)
    async def voiceban(self, ctx: commands.Context, member: discord.Member, *, reason: Optional[str] = None):
        """Ban a user from speaking and listening in voice channels.
        
        Server mutes and deafens a member, preventing them from using voice chat.
        
        **Arguments:**
        - `<member>` - The member to voice ban
        - `[reason]` - Optional reason
        
        **Examples:**
        - `[p]voiceban @User Abusing voice chat`
        
        **Required Permissions:**
        - You: Mute Members, Deafen Members (+ Custom Moderator if applicable)
        - Bot: Mute Members, Deafen Members
        """
        if not await self._can_moderate(ctx, member):
            return
        
        reason = reason or "No reason provided"
        
        try:
            await member.edit(mute=True, deafen=True, reason=f"Voice banned by {ctx.author}: {reason}")
            
            success_embed = self.create_embed(
                "Member Voice Banned",
                f"**{member}** has been voice banned (muted and deafened).\n**Reason:** {reason}",
                "success"
            )
            await self.safe_send(ctx, embed=success_embed)
            
        except discord.Forbidden:
            error_embed = self.create_embed("Error", "I don't have permission to mute/deafen that member.", "error")
            await self.safe_send(ctx, embed=error_embed)
        except discord.HTTPException as e:
            error_embed = self.create_embed("Error", f"Failed to voice ban member: {e}", "error")
            await self.safe_send(ctx, embed=error_embed)

    @commands.command()
    @commands.guild_only()
    @mod_or_permissions(mute_members=True, deafen_members=True)
    @commands.bot_has_permissions(mute_members=True, deafen_members=True)
    async def voiceunban(self, ctx: commands.Context, member: discord.Member, *, reason: Optional[str] = None):
        """Unban a user from speaking and listening in voice channels.
        
        Removes server mute and deafen, restoring voice chat access.
        
        **Arguments:**
        - `<member>` - The member to voice unban
        - `[reason]` - Optional reason
        
        **Examples:**
        - `[p]voiceunban @User`
        
        **Required Permissions:**
        - You: Mute Members, Deafen Members (+ Custom Moderator if applicable)
        - Bot: Mute Members, Deafen Members
        """
        reason = reason or "No reason provided"
        
        try:
            await member.edit(mute=False, deafen=False, reason=f"Voice unbanned by {ctx.author}: {reason}")
            
            success_embed = self.create_embed(
                "Member Voice Unbanned",
                f"**{member}** has been voice unbanned (unmuted and undeafened).",
                "success"
            )
            await self.safe_send(ctx, embed=success_embed)
            
        except discord.Forbidden:
            error_embed = self.create_embed("Error", "I don't have permission to unmute/undeafen that member.", "error")
            await self.safe_send(ctx, embed=error_embed)
        except discord.HTTPException as e:
            error_embed = self.create_embed("Error", f"Failed to voice unban member: {e}", "error")
            await self.safe_send(ctx, embed=error_embed)

    # ==================== Channel Management ====================
    
    @commands.command()
    @commands.guild_only()
    @mod_or_permissions(manage_channels=True)
    @commands.bot_has_permissions(manage_channels=True)
    async def lock(self, ctx: commands.Context, channel: Optional[discord.TextChannel] = None):
        """Lock a channel so members cannot send messages.
        
        Prevents @everyone from sending messages in the channel.
        
        **Arguments:**
        - `[channel]` - Channel to lock (defaults to current channel)
        
        **Examples:**
        - `[p]lock`
        - `[p]lock #general`
        
        **Required Permissions:**
        - You: Manage Channels (+ Custom Moderator if applicable)
        - Bot: Manage Channels
        """
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
    @mod_or_permissions(manage_channels=True)
    @commands.bot_has_permissions(manage_channels=True)
    async def unlock(self, ctx: commands.Context, channel: Optional[discord.TextChannel] = None):
        """Unlock a channel so members can send messages.
        
        Allows @everyone to send messages in the channel again.
        
        **Arguments:**
        - `[channel]` - Channel to unlock (defaults to current channel)
        
        **Examples:**
        - `[p]unlock`
        - `[p]unlock #general`
        
        **Required Permissions:**
        - You: Manage Channels (+ Custom Moderator if applicable)
        - Bot: Manage Channels
        """
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
    @mod_or_permissions(manage_channels=True)
    @commands.bot_has_permissions(manage_channels=True)
    async def slowmode(self, ctx: commands.Context, seconds: int, channel: Optional[discord.TextChannel] = None):
        """Set slowmode for a channel.
        
        Adds a delay between messages that users can send.
        
        **Arguments:**
        - `<seconds>` - Slowmode delay in seconds (0-21600, 0 to disable)
        - `[channel]` - Channel to apply slowmode (defaults to current channel)
        
        **Examples:**
        - `[p]slowmode 5` - 5 second slowmode
        - `[p]slowmode 0` - Disable slowmode
        - `[p]slowmode 30 #general`
        
        **Required Permissions:**
        - You: Manage Channels (+ Custom Moderator if applicable)
        - Bot: Manage Channels
        """
        channel = channel or ctx.channel
        
        if not 0 <= seconds <= 21600:
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

    # ==================== Warning System ====================

    @commands.command()
    @commands.guild_only()
    @mod_or_permissions(moderate_members=True)
    async def warn(self, ctx: commands.Context, member: discord.Member, *, reason: str):
        """Issue a warning to a member.
        
        Adds a warning to a member's record that can be viewed later.
        
        **Arguments:**
        - `<member>` - The member to warn
        - `<reason>` - Reason for the warning
        
        **Examples:**
        - `[p]warn @User Breaking rule #3`
        - `[p]warn @User Inappropriate language`
        
        **Required Permissions:**
        - You: Moderate Members (+ Custom Moderator if applicable)
        """
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
    @mod_or_permissions(moderate_members=True)
    async def warnings(self, ctx: commands.Context, member: discord.Member):
        """View all warnings for a member.
        
        Displays a member's warning history.
        
        **Arguments:**
        - `<member>` - The member to check warnings for
        
        **Examples:**
        - `[p]warnings @User`
        
        **Required Permissions:**
        - You: Moderate Members (+ Custom Moderator if applicable)
        """
        all_warnings = await self.config.guild(ctx.guild).warnings()
        user_warnings = all_warnings.get(str(member.id), [])
        
        if not user_warnings:
            embed = self.create_embed("No Warnings", f"**{member}** has no warnings.", "success")
            await self.safe_send(ctx, embed=embed)
            return
        
        fields = []
        for i, warning in enumerate(user_warnings[-10:], 1):  # Show last 10
            mod = ctx.guild.get_member(warning["moderator"])
            mod_name = mod.mention if mod else f"Unknown (ID: {warning['moderator']})"
            timestamp = datetime.fromisoformat(warning["timestamp"])
            
            fields.append((
                f"Warning #{len(user_warnings) - 10 + i if len(user_warnings) > 10 else i}",
                f"**Reason:** {warning['reason']}\n**Moderator:** {mod_name}\n**Date:** <t:{int(timestamp.timestamp())}:R>",
                False
            ))
        
        embed = self.create_embed(f"Warnings for {member}", fields=fields, color="warning")
        embed.set_footer(text=f"Total warnings: {len(user_warnings)} | Showing last 10")
        
        await self.safe_send(ctx, embed=embed)

    @commands.command()
    @commands.guild_only()
    @commands.admin_or_permissions(administrator=True)
    async def clearwarnings(self, ctx: commands.Context, member: discord.Member):
        """Clear all warnings for a member.
        
        Removes all warnings from a member's record.
        
        **Arguments:**
        - `<member>` - The member to clear warnings for
        
        **Examples:**
        - `[p]clearwarnings @User`
        
        **Required Permissions:**
        - You: Administrator
        """
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

    # ==================== Information Commands ====================

    @commands.command(aliases=["ui", "whois"])
    @commands.guild_only()
    async def userinfo(self, ctx: commands.Context, member: Optional[discord.Member] = None):
        """Show detailed information about a member.
        
        Displays account creation date, join date, roles, and other member information.
        
        **Arguments:**
        - `[member]` - Member to get info about (defaults to yourself)
        
        **Examples:**
        - `[p]userinfo`
        - `[p]userinfo @User`
        - `[p]whois @User`
        """
        member = member or ctx.author
        
        roles = [role.mention for role in member.roles if role != ctx.guild.default_role]
        roles_display = ", ".join(roles[:10])
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
        """Show information about the server.
        
        Displays server stats including member count, roles, channels, and more.
        
        **Examples:**
        - `[p]serverinfo`
        - `[p]si`
        """
        guild = ctx.guild
        
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
        """Show a user's avatar in high quality.
        
        Displays the full-size avatar with a download link.
        
        **Arguments:**
        - `[member]` - Member to get avatar from (defaults to yourself)
        
        **Examples:**
        - `[p]avatar`
        - `[p]avatar @User`
        - `[p]av @User`
        """
        member = member or ctx.author
        
        embed = self.create_embed(
            f"{member.display_name}'s Avatar",
            f"[Download Link]({member.display_avatar.url})"
        )
        embed.set_image(url=member.display_avatar.url)
        
        await self.safe_send(ctx, embed=embed)

    @commands.command(aliases=["latency", "botping"])
    @commands.guild_only()
    async def status(self, ctx: commands.Context):
        """Check the bot's latency and status.
        
        Shows websocket latency and bot performance metrics.
        
        **Examples:**
        - `[p]status`
        - `[p]latency`
        """
        latency_ms = round(self.bot.latency * 1000)
        
        if latency_ms < 100:
            color = "success"
            status_text = "Excellent"
        elif latency_ms < 300:
            color = "warning"
            status_text = "Good"
        else:
            color = "error"
            status_text = "Poor"
        
        embed = self.create_embed(
            "Bot Status",
            f"**Latency:** {latency_ms}ms ({status_text})",
            color
        )
        
        await self.safe_send(ctx, embed=embed)

    # ==================== Fun Commands ====================

    @commands.command(aliases=["8ball"])
    async def eightball(self, ctx: commands.Context, *, question: str):
        """Ask the magic 8-ball a question.
        
        Get mystical answers to your yes/no questions.
        
        **Arguments:**
        - `<question>` - Your question
        
        **Examples:**
        - `[p]8ball Will I win the lottery?`
        - `[p]eightball Should I study today?`
        """
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
        """Create a simple yes/no poll.
        
        Creates a poll message with yes/no reaction buttons.
        
        **Arguments:**
        - `<question>` - The poll question
        
        **Examples:**
        - `[p]poll Should we add a new channel?`
        """
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
        """Let the bot randomly choose between options.
        
        Provide multiple options and the bot will pick one randomly.
        
        **Arguments:**
        - `<choices>` - Space-separated list of options (minimum 2)
        
        **Examples:**
        - `[p]choose pizza burger tacos`
        - `[p]choose yes no maybe`
        """
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
        """Flip a coin.
        
        Simple coin flip - heads or tails.
        
        **Examples:**
        - `[p]coinflip`
        """
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
        """Roll a dice with a specified number of sides.
        
        **Arguments:**
        - `[sides]` - Number of sides on the dice (2-100, default: 6)
        
        **Examples:**
        - `[p]dice` - Roll a standard 6-sided dice
        - `[p]dice 20` - Roll a 20-sided dice
        """
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

    # ==================== Fun Commands (Extras) ====================

    @commands.command()
    async def thanos(self, ctx: commands.Context):
        """Display Thanos image.
        
        **Examples:**
        - `[p]thanos`
        """
        embed = discord.Embed(color=0x800080)
        embed.set_image(url="https://cdn.discordapp.com/attachments/1069748983293022249/1425583704532848721/6LpanIV.png")
        await self.safe_send(ctx, embed=embed)

    @commands.command()
    @commands.guild_only()
    async def hawk(self, ctx: commands.Context, user: Optional[discord.Member] = None):
        """Ask a user if they're a hawk.
        
        Randomly selects a user from the hawk list if no user is specified.
        
        **Arguments:**
        - `[user]` - Optional: Specific user to ask
        
        **Examples:**
        - `[p]hawk` - Ask random hawk user
        - `[p]hawk @User` - Ask specific user
        """
        hawk_enabled = await self.config.guild(ctx.guild).hawk_enabled()
        if not hawk_enabled:
            embed = discord.Embed(color=0xED4245)
            embed.set_image(url="https://cdn.discordapp.com/attachments/1069748983293022249/1425831928644501624/4rMETw3.gif")
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

    @commands.command()
    @commands.guild_only()
    async def gay(self, ctx: commands.Context, user: Optional[discord.Member] = None):
        """Check how gay someone is.
        
        Generates a random percentage for the specified user.
        
        **Arguments:**
        - `<user>` - User to check
        
        **Examples:**
        - `[p]gay @User`
        """
        gay_enabled = await self.config.guild(ctx.guild).gay_enabled()
        if not gay_enabled:
            embed = discord.Embed(color=0xED4245)
            embed.set_image(url="https://cdn.discordapp.com/attachments/1069748983293022249/1425831928644501624/4rMETw3.gif")
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

    @commands.command()
    @commands.is_owner()
    @commands.guild_only()
    async def spamping(self, ctx: commands.Context, user: discord.Member, amount: Optional[int] = DEFAULT_PING_AMOUNT):
        """Ping a user multiple times.
        
        Sends multiple ping messages to annoy a user (owner only).
        
        **Arguments:**
        - `<user>` - User to spam ping
        - `[amount]` - Number of pings (default: 5, max: 20)
        
        **Examples:**
        - `[p]spamping @User 10`
        
        **Required Permissions:**
        - You: Bot Owner
        """
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

    # ==================== Message Block Commands ====================
    
    @commands.group(name="msgblock", invoke_without_command=True)
    @commands.is_owner()
    @commands.guild_only()
    async def msgblock(self, ctx: commands.Context):
        """Manage users whose messages are automatically deleted.
        
        Use subcommands to add, remove, or list blocked users.
        """
        await ctx.send_help(ctx.command)

    @msgblock.command(name="add")
    async def msgblock_add(self, ctx: commands.Context, user_id: int):
        """Add a user to the message deletion list.
        
        **Arguments:**
        - `<user_id>` - Discord user ID to block
        
        **Examples:**
        - `[p]msgblock add 123456789012345678`
        """
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
        """Remove a user from the message deletion list.
        
        **Arguments:**
        - `<user_id>` - Discord user ID to unblock
        
        **Examples:**
        - `[p]msgblock remove 123456789012345678`
        """
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
        """Show all blocked users.
        
        **Examples:**
        - `[p]msgblock list`
        """
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
        
        description = "\n".join(user_list)
        if len(description) > 4000:
            description = description[:4000] + f"\n... and {len(blocked_users) - description.count('•')} more"
        
        embed = self.create_embed("Blocked Users", description, "info")
        embed.set_footer(text=f"Total: {len(blocked_users)} user(s)")
        
        await self.safe_send(ctx, embed=embed)

    # ==================== Hawk Management (Hidden) ====================

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
            embed = discord.Embed(color=0x57F287)
            embed.set_image(url="https://cdn.discordapp.com/attachments/1069748983293022249/1425831721160540281/NzusuSn.png")
            embed.description = f"Hawk command is now **{status_text}**."
        else:
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
            embed = discord.Embed(color=0x57F287)
            embed.set_image(url="https://cdn.discordapp.com/attachments/1069748983293022249/1425831721160540281/NzusuSn.png")
            embed.description = f"Gay command is now **{status_text}**."
        else:
            embed = self.create_embed("Gay Command Updated", f"Gay command is now **{status_text}**.", "warning")
        
        await self.safe_send(ctx, embed=embed)


async def setup(bot):
    """Load the MessageDelete cog."""
    await bot.add_cog(MessageDelete(bot))
