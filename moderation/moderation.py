from redbot.core import commands, Config
import discord
import asyncio
from typing import Optional, Union, List
from datetime import datetime, timedelta
import logging

log = logging.getLogger("red.moderation")


def mod_or_permissions(**perms):
    async def predicate(ctx: commands.Context):
        # Bot Owner / Server Owner always allowed
        if await ctx.bot.is_owner(ctx.author) or (
            ctx.guild and ctx.author == ctx.guild.owner
        ):
            return True
        cog = ctx.bot.get_cog("Moderation")
        # Use cached moderators instead of database call
        moderators = cog._get_cached_moderators(ctx.guild.id) if (ctx.guild and cog) else []
        if ctx.guild and ctx.author.id in moderators:
            # mods: Must also have permissions
            return await commands.has_permissions(**perms).predicate(ctx)
        # Anyone not in mods: denied
        return False

    return commands.check(predicate)


class Moderation(commands.Cog):
    """Moderation"""

    __cog_name__ = "Moderation"

    def __init__(self, bot):
        self.bot = bot
        self.config = Config.get_conf(
            self, identifier=987654321, force_registration=True
        )
        default_guild = {
            "blocked_users": [],
            "moderators": [],
            "warnings": {},
            "tempbans": {},
            "modlog_channel": None,
        }
        self.config.register_guild(**default_guild)
        
        # In-memory caches for performance
        self._blocked_cache = {}  # {guild_id: set(user_ids)}
        self._moderator_cache = {}  # {guild_id: set(user_ids)}
        self._cache_ready = False
        
        # Background tasks
        self.tempban_task = None
        self.bot.loop.create_task(self._initialize_caches())

    async def _initialize_caches(self):
        """Load all data into memory on startup."""
        await self.bot.wait_until_ready()
        try:
            for guild in self.bot.guilds:
                blocked = await self.config.guild(guild).blocked_users()
                mods = await self.config.guild(guild).moderators()
                self._blocked_cache[guild.id] = set(blocked)
                self._moderator_cache[guild.id] = set(mods)
            self._cache_ready = True
            log.info("Moderation caches initialized")
        except Exception as e:
            log.error(f"Failed to initialize caches: {e}")
        
        # Start tempban task after cache is ready
        self.tempban_task = self.bot.loop.create_task(self.check_tempbans())

    def _get_cached_blocked(self, guild_id: int) -> set:
        """Get blocked users from cache."""
        return self._blocked_cache.get(guild_id, set())

    def _get_cached_moderators(self, guild_id: int) -> set:
        """Get moderators from cache."""
        return self._moderator_cache.get(guild_id, set())

    async def _update_blocked_cache(self, guild_id: int, user_id: int, add: bool = True):
        """Update blocked users cache and database."""
        if guild_id not in self._blocked_cache:
            self._blocked_cache[guild_id] = set()
        
        if add:
            self._blocked_cache[guild_id].add(user_id)
        else:
            self._blocked_cache[guild_id].discard(user_id)
        
        # Update database
        async with self.config.guild_from_id(guild_id).blocked_users() as blocked:
            if add and user_id not in blocked:
                blocked.append(user_id)
            elif not add and user_id in blocked:
                blocked.remove(user_id)

    async def _update_moderator_cache(self, guild_id: int, user_id: int, add: bool = True):
        """Update moderators cache and database."""
        if guild_id not in self._moderator_cache:
            self._moderator_cache[guild_id] = set()
        
        if add:
            self._moderator_cache[guild_id].add(user_id)
        else:
            self._moderator_cache[guild_id].discard(user_id)
        
        # Update database
        async with self.config.guild_from_id(guild_id).moderators() as mods:
            if add and user_id not in mods:
                mods.append(user_id)
            elif not add and user_id in mods:
                mods.remove(user_id)

    def cog_unload(self):
        if self.tempban_task:
            self.tempban_task.cancel()

    async def check_tempbans(self):
        """Optimized background task for tempban expiration."""
        await self.bot.wait_until_ready()
        while not self.bot.is_closed():
            try:
                tasks = []
                now = datetime.utcnow()
                
                for guild in self.bot.guilds:
                    tempbans = await self.config.guild(guild).tempbans()
                    if not tempbans:
                        continue
                    
                    for uid, ban_info in list(tempbans.items()):
                        try:
                            unban_time = datetime.fromisoformat(ban_info["unban_time"])
                            if unban_time <= now:
                                tasks.append(self._process_tempban_expiry(guild, int(uid)))
                        except (ValueError, KeyError):
                            continue
                
                # Process all expired bans in parallel
                if tasks:
                    await asyncio.gather(*tasks, return_exceptions=True)
                
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error(f"Error in tempban checker: {e}")
                await asyncio.sleep(60)

    async def _process_tempban_expiry(self, guild: discord.Guild, user_id: int):
        """Handle individual tempban expiry."""
        try:
            user = await self.bot.fetch_user(user_id)
            await guild.unban(user, reason="Temporary ban expired")
            
            async with self.config.guild(guild).tempbans() as tb:
                ban_info = tb.pop(str(user_id), None)
            
            # Log to modlog
            if ban_info:
                await self._log_action(
                    guild,
                    "Tempban Expired",
                    f"**User:** {user.mention} ({user.id})\n**Original Reason:** {ban_info.get('reason', 'Unknown')}",
                    0x57F287,
                    user
                )
        except discord.NotFound:
            # User not banned, clean up database
            async with self.config.guild(guild).tempbans() as tb:
                tb.pop(str(user_id), None)
        except Exception as e:
            log.error(f"Failed to process tempban expiry for {user_id}: {e}")

    async def _log_action(
        self,
        guild: discord.Guild,
        action: str,
        description: str,
        color: int,
        target: Union[discord.User, discord.Member] = None,
        moderator: discord.Member = None,
    ):
        """Send moderation action to log channel."""
        try:
            channel_id = await self.config.guild(guild).modlog_channel()
            if not channel_id:
                return
            
            channel = guild.get_channel(channel_id)
            if not channel:
                return
            
            embed = discord.Embed(
                title=f"🔨 {action}",
                description=description,
                color=color,
                timestamp=datetime.utcnow()
            )
            
            if target:
                embed.set_thumbnail(url=target.display_avatar.url)
            
            if moderator:
                embed.set_footer(
                    text=f"Moderator: {moderator}",
                    icon_url=moderator.display_avatar.url
                )
            
            await channel.send(embed=embed)
        except Exception as e:
            log.error(f"Failed to log action to modlog: {e}")

    async def _execute_mod_action(
        self,
        ctx: commands.Context,
        target: Union[discord.Member, discord.User],
        action_name: str,
        action_func,
        reason: Optional[str] = None,
        dm_message: Optional[str] = None,
        success_message: Optional[str] = None,
        dm_color: int = 0xFEE75C,
        success_color: int = 0x57F287,
    ):
        """Unified handler for moderation actions to reduce code duplication."""
        reason = reason or "No reason provided"
        
        # Try to DM user
        dm_sent = False
        if dm_message:
            try:
                embed = discord.Embed(
                    title=f"{action_name} in {ctx.guild.name}",
                    description=dm_message.format(reason=reason),
                    color=dm_color,
                )
                await target.send(embed=embed)
                dm_sent = True
            except (discord.Forbidden, discord.HTTPException):
                pass
        
        # Execute the action
        try:
            await action_func()
        except discord.Forbidden:
            await ctx.send(f"❌ I don't have permission to {action_name.lower()} that user.")
            return False
        except Exception as e:
            await ctx.send(f"❌ Could not {action_name.lower()} user: {e}")
            return False
        
        # Send success message
        if success_message:
            embed = discord.Embed(
                title=action_name,
                description=success_message.format(target=target, reason=reason),
                color=success_color,
            )
            await ctx.send(embed=embed)
        
        # Log to modlog
        log_desc = success_message.format(target=f"{target.mention} ({target.id})", reason=reason)
        if not dm_sent and dm_message:
            log_desc += "\n⚠️ Could not DM user"
        
        await self._log_action(ctx.guild, action_name, log_desc, success_color, target, ctx.author)
        return True

    # ================= Mod Log Management =================
    @commands.group(invoke_without_command=True)
    @commands.guild_only()
    @commands.admin_or_permissions(administrator=True)
    async def modlog(self, ctx: commands.Context):
        """Configure moderation logging."""
        channel_id = await self.config.guild(ctx.guild).modlog_channel()
        if channel_id:
            channel = ctx.guild.get_channel(channel_id)
            if channel:
                await ctx.send(f"Mod log channel is currently set to {channel.mention}")
            else:
                await ctx.send("Mod log channel is set but the channel no longer exists.")
        else:
            await ctx.send("No mod log channel is configured. Use `modlog set` to configure one.")

    @modlog.command(name="set")
    async def modlog_set(self, ctx: commands.Context, channel: discord.TextChannel):
        """Set the moderation log channel."""
        await self.config.guild(ctx.guild).modlog_channel.set(channel.id)
        await ctx.send(f"✅ Mod log channel set to {channel.mention}")

    @modlog.command(name="disable")
    async def modlog_disable(self, ctx: commands.Context):
        """Disable moderation logging."""
        await self.config.guild(ctx.guild).modlog_channel.set(None)
        await ctx.send("✅ Mod log channel disabled.")

    # ================= mods Management =================
    @commands.group(invoke_without_command=True)
    @commands.guild_only()
    @commands.admin_or_permissions(administrator=True)
    async def mods(self, ctx: commands.Context):
        """Manage custom moderators for this server."""
        await ctx.send_help(ctx.command)

    @mods.command()
    async def add(self, ctx, user: Union[discord.Member, int]):
        """Add a user to the custom moderator list."""
        user_id = user.id if isinstance(user, discord.Member) else user
        
        if user_id in self._get_cached_moderators(ctx.guild.id):
            await ctx.send(f"User ID `{user_id}` is already a moderator.")
            return
        
        await self._update_moderator_cache(ctx.guild.id, user_id, add=True)
        await ctx.send(f"User ID `{user_id}` added to moderator list.")

    @mods.command()
    async def remove(self, ctx, user: Union[discord.Member, int]):
        """Remove a user from the custom moderator list."""
        user_id = user.id if isinstance(user, discord.Member) else user
        
        if user_id not in self._get_cached_moderators(ctx.guild.id):
            await ctx.send(f"User ID `{user_id}` is not in the moderator list.")
            return
        
        await self._update_moderator_cache(ctx.guild.id, user_id, add=False)
        await ctx.send(f"User ID `{user_id}` removed from moderator list.")

    @mods.command(name="list")
    async def list_(self, ctx):
        """Show all custom moderators."""
        moderators = list(self._get_cached_moderators(ctx.guild.id))
        if not moderators:
            await ctx.send("No custom moderators set.")
            return

        names = []
        for uid in moderators:
            member = ctx.guild.get_member(uid)
            if member:
                names.append(f"• {member} (`{uid}`)")
            else:
                names.append(f"• `{uid}` (not in server)")

        embed = discord.Embed(
            title="Custom Moderators", description="\n".join(names), color=0x5865F2
        )
        embed.set_footer(text=f"Total: {len(moderators)} moderator(s)")
        await ctx.send(embed=embed)

    # ================= Member Management =================
    @commands.command()
    @commands.guild_only()
    @mod_or_permissions(kick_members=True)
    @commands.bot_has_permissions(kick_members=True)
    async def kick(self, ctx, member: discord.Member, *, reason: Optional[str] = None):
        """Kick a member from the server."""
        if member == ctx.guild.owner:
            await ctx.send("❌ Cannot kick the server owner.")
            return

        if member.top_role >= ctx.author.top_role and ctx.author != ctx.guild.owner:
            await ctx.send("❌ Cannot kick someone with equal or higher role.")
            return

        await self._execute_mod_action(
            ctx,
            member,
            "Member Kicked",
            lambda: member.kick(reason=f"By {ctx.author}: {reason or 'No reason provided'}"),
            reason,
            dm_message="**Reason:** {reason}",
            success_message="**{target}** has been kicked.\n**Reason:** {reason}",
        )

    @commands.command()
    @commands.guild_only()
    @mod_or_permissions(ban_members=True)
    @commands.bot_has_permissions(ban_members=True)
    async def ban(
        self,
        ctx,
        user: Union[discord.Member, int],
        delete_days: int = 1,
        *,
        reason: Optional[str] = None,
    ):
        """Ban a user from the server and delete message history."""
        if isinstance(user, int):
            try:
                user = await self.bot.fetch_user(user)
            except discord.NotFound:
                await ctx.send("❌ User not found.")
                return

        if isinstance(user, discord.Member):
            if user == ctx.guild.owner:
                await ctx.send("❌ Cannot ban the server owner.")
                return

            if user.top_role >= ctx.author.top_role and ctx.author != ctx.guild.owner:
                await ctx.send("❌ Cannot ban someone with equal or higher role.")
                return

        if not 0 <= delete_days <= 7:
            await ctx.send("❌ Delete days must be between 0 and 7.")
            return

        await self._execute_mod_action(
            ctx,
            user,
            "Member Banned",
            lambda: ctx.guild.ban(
                user,
                reason=f"By {ctx.author}: {reason or 'No reason provided'}",
                delete_message_days=delete_days,
            ),
            reason,
            dm_message="**Reason:** {reason}",
            success_message=f"**{{target}}** has been banned.\n**Messages deleted:** {delete_days} day(s)\n**Reason:** {{reason}}",
            dm_color=0xED4245,
        )

    @commands.command()
    @commands.guild_only()
    @mod_or_permissions(ban_members=True)
    @commands.bot_has_permissions(ban_members=True)
    async def unban(self, ctx, user_id: int, *, reason: Optional[str] = None):
        """Unban a user by their user ID."""
        try:
            user = await self.bot.fetch_user(user_id)
        except discord.NotFound:
            await ctx.send("❌ User not found.")
            return

        # Remove from tempbans if present
        async with self.config.guild(ctx.guild).tempbans() as tempbans:
            tempbans.pop(str(user_id), None)

        await self._execute_mod_action(
            ctx,
            user,
            "Member Unbanned",
            lambda: ctx.guild.unban(
                user, reason=f"By {ctx.author}: {reason or 'No reason provided'}"
            ),
            reason,
            dm_message=None,
            success_message="**{target}** has been unbanned.\n**Reason:** {reason}",
        )

    @commands.command()
    @commands.guild_only()
    @mod_or_permissions(ban_members=True)
    @commands.bot_has_permissions(ban_members=True)
    async def softban(
        self, ctx, member: discord.Member, *, reason: Optional[str] = None
    ):
        """Softban a member (ban then immediately unban to delete messages)."""
        if member == ctx.guild.owner:
            await ctx.send("❌ Cannot softban the server owner.")
            return

        if member.top_role >= ctx.author.top_role and ctx.author != ctx.guild.owner:
            await ctx.send("❌ Cannot softban someone with equal or higher role.")
            return

        async def softban_action():
            await ctx.guild.ban(
                member,
                reason=f"Softban by {ctx.author}: {reason or 'No reason provided'}",
                delete_message_days=1,
            )
            await ctx.guild.unban(member, reason=f"Softban unban by {ctx.author}")

        await self._execute_mod_action(
            ctx,
            member,
            "Member Softbanned",
            softban_action,
            reason,
            dm_message="You have been removed and your recent messages deleted.\n**Reason:** {reason}\n\nYou may rejoin the server.",
            success_message="**{target}** has been softbanned.\n**Reason:** {reason}",
        )

    @commands.command()
    @commands.guild_only()
    @mod_or_permissions(ban_members=True)
    @commands.bot_has_permissions(ban_members=True)
    async def tempban(
        self,
        ctx,
        user: Union[discord.Member, int],
        duration: str,
        *,
        reason: Optional[str] = None,
    ):
        """Temporarily ban a user for a specified duration (e.g., 30m, 2h, 1d, 7d)."""
        if isinstance(user, int):
            try:
                user = await self.bot.fetch_user(user)
            except discord.NotFound:
                await ctx.send("❌ User not found.")
                return

        if isinstance(user, discord.Member):
            if user == ctx.guild.owner:
                await ctx.send("❌ Cannot tempban the server owner.")
                return

            if user.top_role >= ctx.author.top_role and ctx.author != ctx.guild.owner:
                await ctx.send("❌ Cannot tempban someone with equal or higher role.")
                return

        try:
            delta = self._parse_duration(duration)
            unban_time = datetime.utcnow() + delta
        except ValueError:
            await ctx.send("❌ Invalid duration. Use format like: 30m, 2h, 1d, 7d")
            return

        async def tempban_action():
            await ctx.guild.ban(
                user,
                reason=f"Tempban by {ctx.author}: {reason or 'No reason provided'}",
                delete_message_days=1,
            )
            async with self.config.guild(ctx.guild).tempbans() as tempbans:
                tempbans[str(user.id)] = {
                    "unban_time": unban_time.isoformat(),
                    "reason": reason or "No reason provided",
                    "moderator": ctx.author.id,
                }

        success = await self._execute_mod_action(
            ctx,
            user,
            "Member Temporarily Banned",
            tempban_action,
            reason,
            dm_message=f"**Duration:** {duration}\n**Unbanned:** <t:{int(unban_time.timestamp())}:R>\n**Reason:** {{reason}}",
            success_message=f"**{{target}}** has been banned for {duration}.\n**Unbanned:** <t:{int(unban_time.timestamp())}:R>\n**Reason:** {{reason}}",
        )

    @commands.command()
    @commands.guild_only()
    @mod_or_permissions(ban_members=True)
    @commands.bot_has_permissions(ban_members=True)
    async def massban(self, ctx, *user_ids: int):
        """Mass ban multiple users by their IDs (parallelized for speed)."""
        if not user_ids:
            await ctx.send("❌ Please provide at least one user ID.")
            return

        if len(user_ids) > 20:
            await ctx.send("❌ Maximum 20 users can be banned at once.")
            return

        status_msg = await ctx.send(f"⏳ Banning {len(user_ids)} users...")

        # Parallel ban execution
        async def ban_user(user_id: int):
            try:
                user = await self.bot.fetch_user(user_id)
                await ctx.guild.ban(
                    user, reason=f"Massban by {ctx.author}", delete_message_days=1
                )
                return (True, f"{user} ({user_id})")
            except discord.NotFound:
                return (False, f"{user_id} (Not found)")
            except discord.Forbidden:
                return (False, f"{user_id} (Permission denied)")
            except Exception as e:
                return (False, f"{user_id} (Error: {e})")

        results = await asyncio.gather(*[ban_user(uid) for uid in user_ids])
        
        banned = [r[1] for r in results if r[0]]
        failed = [r[1] for r in results if not r[0]]

        await status_msg.delete()

        embed = discord.Embed(
            title="Mass Ban Complete",
            description=f"**Total:** {len(user_ids)} | **Banned:** {len(banned)} | **Failed:** {len(failed)}",
            color=0x57F287 if len(banned) > 0 else 0xFEE75C,
        )

        if banned:
            banned_text = "\n".join(banned[:10])
            if len(banned) > 10:
                banned_text += f"\n... and {len(banned) - 10} more"
            embed.add_field(name="Successfully Banned", value=banned_text, inline=False)

        if failed:
            failed_text = "\n".join(failed[:10])
            if len(failed) > 10:
                failed_text += f"\n... and {len(failed) - 10} more"
            embed.add_field(name="Failed", value=failed_text, inline=False)

        await ctx.send(embed=embed)
        
        # Log to modlog
        if banned:
            await self._log_action(
                ctx.guild,
                "Mass Ban",
                f"**Banned:** {len(banned)} users\n**Failed:** {len(failed)} users",
                0x57F287,
                moderator=ctx.author
            )

    @commands.command(aliases=["mute"])
    @commands.guild_only()
    @mod_or_permissions(moderate_members=True)
    @commands.bot_has_permissions(moderate_members=True)
    async def timeout(
        self,
        ctx,
        member: discord.Member,
        duration: str,
        *,
        reason: Optional[str] = None,
    ):
        """Timeout (mute) a member for a specified duration."""
        if member == ctx.guild.owner:
            await ctx.send("❌ Cannot timeout the server owner.")
            return

        if member.top_role >= ctx.author.top_role and ctx.author != ctx.guild.owner:
            await ctx.send("❌ Cannot timeout someone with equal or higher role.")
            return

        try:
            delta = self._parse_duration(duration)
            if delta.total_seconds() > 2419200:  # 28 days max
                await ctx.send("❌ Maximum timeout duration is 28 days.")
                return
        except ValueError:
            await ctx.send("❌ Invalid duration. Use format like: 10m, 2h, 1d")
            return

        await self._execute_mod_action(
            ctx,
            member,
            "Member Timed Out",
            lambda: member.timeout(
                delta, reason=f"By {ctx.author}: {reason or 'No reason provided'}"
            ),
            reason,
            dm_message=f"You have been timed out for {duration}.\n**Reason:** {{reason}}",
            success_message=f"**{{target}}** has been timed out for {duration}.\n**Reason:** {{reason}}",
        )

    @commands.command(aliases=["unmute"])
    @commands.guild_only()
    @mod_or_permissions(moderate_members=True)
    @commands.bot_has_permissions(moderate_members=True)
    async def untimeout(
        self, ctx, member: discord.Member, *, reason: Optional[str] = None
    ):
        """Remove timeout (unmute) from a member."""
        await self._execute_mod_action(
            ctx,
            member,
            "Timeout Removed",
            lambda: member.timeout(
                None, reason=f"By {ctx.author}: {reason or 'No reason provided'}"
            ),
            reason,
            dm_message=None,
            success_message="**{target}**'s timeout has been removed.",
        )

    @commands.command()
    @commands.guild_only()
    @mod_or_permissions(manage_nicknames=True)
    @commands.bot_has_permissions(manage_nicknames=True)
    async def rename(self, ctx, member: discord.Member, *, nickname: str = None):
        """Change a member's nickname. Leave blank to reset."""
        if member.top_role >= ctx.author.top_role and ctx.author != ctx.guild.owner:
            await ctx.send("❌ Cannot rename someone with equal or higher role.")
            return

        old_nick = member.nick or member.name

        try:
            await member.edit(nick=nickname, reason=f"Nickname changed by {ctx.author}")

            if nickname:
                desc = f"**{member.mention}**'s nickname has been changed.\n**Old:** {old_nick}\n**New:** {nickname}"
                title = "Nickname Changed"
            else:
                desc = f"**{member.mention}**'s nickname has been removed.\n**Old nickname:** {old_nick}"
                title = "Nickname Removed"

            success_embed = discord.Embed(
                title=title,
                description=desc,
                color=0x57F287,
            )
            await ctx.send(embed=success_embed)
            
            await self._log_action(ctx.guild, title, desc, 0x57F287, member, ctx.author)

        except discord.Forbidden:
            await ctx.send(
                "❌ I don't have permission to change that member's nickname."
            )
        except Exception as e:
            await ctx.send(f"❌ Could not change nickname: {e}")

    # ================= Message Management =================
    @commands.command(aliases=["clear", "clean"])
    @commands.guild_only()
    @mod_or_permissions(manage_messages=True)
    @commands.bot_has_permissions(manage_messages=True, read_message_history=True)
    async def purge(self, ctx, amount: int, member: Optional[discord.Member] = None):
        """Delete multiple messages at once. Optionally from a specific user."""
        if amount < 1 or amount > 1000:
            await ctx.send("❌ Amount must be between 1 and 1000.")
            return

        try:

            def check(m):
                return m.author == member if member else True

            deleted = await ctx.channel.purge(limit=amount + 1, check=check)
            count = len(deleted) - 1

            target_text = f" from **{member}**" if member else ""
            success_embed = discord.Embed(
                title="Messages Purged",
                description=f"Deleted {count} message(s){target_text}.",
                color=0x57F287,
            )
            msg = await ctx.send(embed=success_embed)
            await msg.delete(delay=5)
            
            # Log to modlog
            await self._log_action(
                ctx.guild,
                "Messages Purged",
                f"**Channel:** {ctx.channel.mention}\n**Amount:** {count} messages{target_text}",
                0x57F287,
                moderator=ctx.author
            )

        except discord.Forbidden:
            await ctx.send("❌ I don't have permission to delete messages.")
        except Exception as e:
            await ctx.send(f"❌ Could not purge messages: {e}")

    # ================= Channel Management =================
    @commands.command()
    @commands.guild_only()
    @mod_or_permissions(manage_channels=True)
    @commands.bot_has_permissions(manage_channels=True)
    async def lock(self, ctx, channel: Optional[discord.TextChannel] = None):
        """Lock a channel so members cannot send messages."""
        channel = channel or ctx.channel
        overwrite = channel.overwrites_for(ctx.guild.default_role)

        if overwrite.send_messages is False:
            await ctx.send("❌ Channel is already locked.")
            return

        overwrite.send_messages = False

        try:
            await channel.set_permissions(
                ctx.guild.default_role,
                overwrite=overwrite,
                reason=f"Locked by {ctx.author}",
            )

            success_embed = discord.Embed(
                title="Channel Locked",
                description=f"{channel.mention} has been locked.",
                color=0x57F287,
            )
            await ctx.send(embed=success_embed)
            
            await self._log_action(
                ctx.guild,
                "Channel Locked",
                f"**Channel:** {channel.mention}",
                0x57F287,
                moderator=ctx.author
            )

        except discord.Forbidden:
            await ctx.send("❌ I don't have permission to lock that channel.")
        except Exception as e:
            await ctx.send(f"❌ Could not lock channel: {e}")

    @commands.command()
    @commands.guild_only()
    @mod_or_permissions(manage_channels=True)
    @commands.bot_has_permissions(manage_channels=True)
    async def unlock(self, ctx, channel: Optional[discord.TextChannel] = None):
        """Unlock a channel so members can send messages."""
        channel = channel or ctx.channel
        overwrite = channel.overwrites_for(ctx.guild.default_role)

        if overwrite.send_messages is not False:
            await ctx.send("❌ Channel is not locked.")
            return

        overwrite.send_messages = None

        try:
            await channel.set_permissions(
                ctx.guild.default_role,
                overwrite=overwrite,
                reason=f"Unlocked by {ctx.author}",
            )

            success_embed = discord.Embed(
                title="Channel Unlocked",
                description=f"{channel.mention} has been unlocked.",
                color=0x57F287,
            )
            await ctx.send(embed=success_embed)
            
            await self._log_action(
                ctx.guild,
                "Channel Unlocked",
                f"**Channel:** {channel.mention}",
                0x57F287,
                moderator=ctx.author
            )

        except discord.Forbidden:
            await ctx.send("❌ I don't have permission to unlock that channel.")
        except Exception as e:
            await ctx.send(f"❌ Could not unlock channel: {e}")

    @commands.command()
    @commands.guild_only()
    @mod_or_permissions(manage_channels=True)
    @commands.bot_has_permissions(manage_channels=True)
    async def slowmode(
        self, ctx, seconds: int, channel: Optional[discord.TextChannel] = None
    ):
        """Set slowmode for a channel (0-21600 seconds, 0 to disable)."""
        channel = channel or ctx.channel

        if not 0 <= seconds <= 21600:
            await ctx.send("❌ Slowmode must be between 0 and 21600 seconds (6 hours).")
            return

        try:
            await channel.edit(slowmode_delay=seconds, reason=f"Set by {ctx.author}")

            if seconds == 0:
                embed_title = "Slowmode Disabled"
                embed_desc = f"Slowmode disabled in {channel.mention}."
            else:
                embed_title = "Slowmode Set"
                embed_desc = f"Slowmode set to {seconds} seconds in {channel.mention}."

            success_embed = discord.Embed(
                title=embed_title, description=embed_desc, color=0x57F287
            )
            await ctx.send(embed=success_embed)
            
            await self._log_action(
                ctx.guild,
                embed_title,
                f"**Channel:** {channel.mention}\n**Delay:** {seconds} seconds",
                0x57F287,
                moderator=ctx.author
            )

        except discord.Forbidden:
            await ctx.send("❌ I don't have permission to edit that channel.")
        except Exception as e:
            await ctx.send(f"❌ Could not set slowmode: {e}")

    # ============ Voice Management ============

    @commands.command()
    @commands.guild_only()
    @mod_or_permissions(move_members=True)
    @commands.bot_has_permissions(move_members=True)
    async def voicekick(
        self, ctx, member: discord.Member, *, reason: Optional[str] = None
    ):
        """Kick a member from their current voice channel."""
        if not member.voice:
            return await ctx.send("❌ Member is not in a voice channel.")
        if member.top_role >= ctx.author.top_role and ctx.author != ctx.guild.owner:
            return await ctx.send("❌ Cannot kick someone with equal or higher role.")
        
        await self._execute_mod_action(
            ctx,
            member,
            "Member Voice Kicked",
            lambda: member.move_to(
                None,
                reason=f"Voice kicked by {ctx.author}: {reason or 'No reason provided'}",
            ),
            reason,
            dm_message=None,
            success_message="**{target}** kicked from voice.\n**Reason:** {reason}",
        )

    @commands.command()
    @commands.guild_only()
    @mod_or_permissions(mute_members=True)
    @commands.bot_has_permissions(mute_members=True)
    async def voicemute(
        self, ctx, member: discord.Member, *, reason: Optional[str] = None
    ):
        """Server mute a member in voice channels."""
        if not member.voice:
            return await ctx.send("❌ Member is not in a voice channel.")
        if member.voice.mute:
            return await ctx.send("❌ Member is already voice muted.")
        if member.top_role >= ctx.author.top_role and ctx.author != ctx.guild.owner:
            return await ctx.send("❌ Cannot mute someone with equal or higher role.")
        
        await self._execute_mod_action(
            ctx,
            member,
            "Member Voice Muted",
            lambda: member.edit(
                mute=True,
                reason=f"Voice muted by {ctx.author}: {reason or 'No reason provided'}",
            ),
            reason,
            dm_message=None,
            success_message="**{target}** voice muted.\n**Reason:** {reason}",
        )

    @commands.command()
    @commands.guild_only()
    @mod_or_permissions(mute_members=True)
    @commands.bot_has_permissions(mute_members=True)
    async def voiceunmute(
        self, ctx, member: discord.Member, *, reason: Optional[str] = None
    ):
        """Server unmute a member in voice channels."""
        if not member.voice:
            return await ctx.send("❌ Member is not in a voice channel.")
        if not member.voice.mute:
            return await ctx.send("❌ Member is not voice muted.")
        
        await self._execute_mod_action(
            ctx,
            member,
            "Member Voice Unmuted",
            lambda: member.edit(
                mute=False,
                reason=f"Voice unmuted by {ctx.author}: {reason or 'No reason provided'}",
            ),
            reason,
            dm_message=None,
            success_message="**{target}** voice unmuted.",
        )

    @commands.command()
    @commands.guild_only()
    @mod_or_permissions(mute_members=True, deafen_members=True)
    @commands.bot_has_permissions(mute_members=True, deafen_members=True)
    async def voiceban(
        self, ctx, member: discord.Member, *, reason: Optional[str] = None
    ):
        """Mute and deafen someone in voice channels."""
        if member.top_role >= ctx.author.top_role and ctx.author != ctx.guild.owner:
            return await ctx.send("❌ Cannot ban someone with equal or higher role.")
        
        await self._execute_mod_action(
            ctx,
            member,
            "Member Voice Banned",
            lambda: member.edit(
                mute=True,
                deafen=True,
                reason=f"Voice banned by {ctx.author}: {reason or 'No reason provided'}",
            ),
            reason,
            dm_message=None,
            success_message="**{target}** has been voice banned (muted and deafened).\n**Reason:** {reason}",
        )

    @commands.command()
    @commands.guild_only()
    @mod_or_permissions(mute_members=True, deafen_members=True)
    @commands.bot_has_permissions(mute_members=True, deafen_members=True)
    async def voiceunban(
        self, ctx, member: discord.Member, *, reason: Optional[str] = None
    ):
        """Unban a member from speaking and listening in voice channels."""
        await self._execute_mod_action(
            ctx,
            member,
            "Member Voice Unbanned",
            lambda: member.edit(
                mute=False,
                deafen=False,
                reason=f"Voice unbanned by {ctx.author}: {reason or 'No reason provided'}",
            ),
            reason,
            dm_message=None,
            success_message="**{target}** has been voice unbanned (unmuted and undeafened).",
        )

    # ================= Warning System =================
    @commands.command()
    @commands.guild_only()
    @mod_or_permissions(moderate_members=True)
    async def warn(self, ctx, member: discord.Member, *, reason: str):
        """Issue a warning to a member."""
        if member.top_role >= ctx.author.top_role and ctx.author != ctx.guild.owner:
            await ctx.send("❌ Cannot warn someone with equal or higher role.")
            return

        async with self.config.guild(ctx.guild).warnings() as warnings:
            if str(member.id) not in warnings:
                warnings[str(member.id)] = []

            warning = {
                "reason": reason,
                "moderator": ctx.author.id,
                "timestamp": datetime.utcnow().isoformat(),
            }
            warnings[str(member.id)].append(warning)
            warn_count = len(warnings[str(member.id)])

        try:
            user_embed = discord.Embed(
                title=f"Warning in {ctx.guild.name}",
                description=f"**Reason:** {reason}\n**Total Warnings:** {warn_count}",
                color=0xFEE75C,
            )
            await member.send(embed=user_embed)
        except:
            pass

        success_embed = discord.Embed(
            title="Member Warned",
            description=f"**{member}** has been warned. This is warning #{warn_count}.\n**Reason:** {reason}",
            color=0x57F287,
        )
        await ctx.send(embed=success_embed)
        
        # Log to modlog
        await self._log_action(
            ctx.guild,
            "Member Warned",
            f"**User:** {member.mention} ({member.id})\n**Warning #:** {warn_count}\n**Reason:** {reason}",
            0xFEE75C,
            member,
            ctx.author
        )

    @commands.command()
    @commands.guild_only()
    @mod_or_permissions(moderate_members=True)
    async def warnings(self, ctx, member: discord.Member):
        """View all warnings for a member."""
        all_warnings = await self.config.guild(ctx.guild).warnings()
        user_warnings = all_warnings.get(str(member.id), [])

        if not user_warnings:
            embed = discord.Embed(
                title="No Warnings",
                description=f"**{member}** has no warnings.",
                color=0x57F287,
            )
            await ctx.send(embed=embed)
            return

        embed = discord.Embed(title=f"Warnings for {member}", color=0xFEE75C)

        for i, warning in enumerate(user_warnings[-10:], 1):  # Show last 10
            mod = ctx.guild.get_member(warning["moderator"])
            mod_name = mod.mention if mod else f"Unknown (ID: {warning['moderator']})"
            timestamp = datetime.fromisoformat(warning["timestamp"])

            embed.add_field(
                name=f"Warning #{len(user_warnings) - 10 + i if len(user_warnings) > 10 else i}",
                value=f"**Reason:** {warning['reason']}\n**Moderator:** {mod_name}\n**Date:** <t:{int(timestamp.timestamp())}:R>",
                inline=False,
            )

        embed.set_footer(text=f"Total warnings: {len(user_warnings)} | Showing last 10")
        await ctx.send(embed=embed)

    @commands.command()
    @commands.guild_only()
    @commands.admin_or_permissions(administrator=True)
    async def clearwarnings(self, ctx, member: discord.Member):
        """Clear all warnings for a member."""
        async with self.config.guild(ctx.guild).warnings() as warnings:
            if str(member.id) in warnings:
                count = len(warnings[str(member.id)])
                del warnings[str(member.id)]

                success_embed = discord.Embed(
                    title="Warnings Cleared",
                    description=f"Cleared {count} warning(s) for **{member}**.",
                    color=0x57F287,
                )
                await ctx.send(embed=success_embed)
                
                await self._log_action(
                    ctx.guild,
                    "Warnings Cleared",
                    f"**User:** {member.mention} ({member.id})\n**Warnings Cleared:** {count}",
                    0x57F287,
                    member,
                    ctx.author
                )
            else:
                await ctx.send("❌ No warnings to clear for that member.")

    # ================= Message Blocking =================
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
            await ctx.send("❌ User ID must be a positive number.")
            return

        if user_id in self._get_cached_blocked(ctx.guild.id):
            await ctx.send(f"❌ User ID `{user_id}` is already blocked.")
            return
        
        await self._update_blocked_cache(ctx.guild.id, user_id, add=True)
        await ctx.send(f"✅ Added user ID `{user_id}` to the message deletion list.")

    @msgblock.command(name="remove")
    async def msgblock_remove(self, ctx: commands.Context, user_id: int):
        """Remove a user from the message deletion list."""
        if user_id not in self._get_cached_blocked(ctx.guild.id):
            await ctx.send(f"❌ User ID `{user_id}` is not blocked.")
            return
        
        await self._update_blocked_cache(ctx.guild.id, user_id, add=False)
        await ctx.send(f"✅ Removed user ID `{user_id}` from the message deletion list.")

    @msgblock.command(name="list")
    async def msgblock_list(self, ctx: commands.Context):
        """Show all blocked users."""
        blocked_users = list(self._get_cached_blocked(ctx.guild.id))

        if not blocked_users:
            embed = discord.Embed(
                title="No Blocked Users",
                description="The message deletion list is empty.",
                color=0x5865F2,
            )
            await ctx.send(embed=embed)
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
            description = (
                description[:4000]
                + f"\n... and {len(blocked_users) - description.count('•')} more"
            )

        embed = discord.Embed(
            title="Blocked Users", description=description, color=0x5865F2
        )
        embed.set_footer(text=f"Total: {len(blocked_users)} user(s)")
        await ctx.send(embed=embed)

    # ================= Event Handlers =================
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """Auto-delete messages from blocked users (cached check)."""
        if not message.guild or message.author.bot or not self._cache_ready:
            return

        try:
            # Use cached set for O(1) lookup instead of database query
            if message.author.id in self._get_cached_blocked(message.guild.id):
                await message.delete()
        except:
            pass

    # ================= Utility Functions =================
    def _parse_duration(self, duration: str) -> timedelta:
        """Parse duration string into timedelta object."""
        time_units = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}

        if len(duration) < 2:
            raise ValueError("Duration too short")

        unit = duration[-1].lower()
        if unit not in time_units:
            raise ValueError("Invalid time unit")

        try:
            amount = int(duration[:-1])
        except ValueError:
            raise ValueError("Invalid duration format")

        if amount <= 0:
            raise ValueError("Duration must be positive")

        return timedelta(seconds=amount * time_units[unit])


async def setup(bot):
    """Load the Moderation cog."""
    await bot.add_cog(Moderation(bot))
