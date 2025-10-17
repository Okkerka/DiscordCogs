from redbot.core import commands, Config
import discord
import asyncio
from typing import Optional, Union
from datetime import datetime, timedelta


def mod_or_permissions(**perms):
    async def predicate(ctx: commands.Context):
        # Bot Owner / Server Owner always allowed
        if await ctx.bot.is_owner(ctx.author) or (
            ctx.guild and ctx.author == ctx.guild.owner
        ):
            return True
        cog = ctx.bot.get_cog("Moderation")
        moderators = (
            await cog.config.guild(ctx.guild).moderators()
            if (ctx.guild and cog)
            else []
        )
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
        }
        self.config.register_guild(**default_guild)
        self.tempban_task = self.bot.loop.create_task(self.check_tempbans())

    def cog_unload(self):
        if self.tempban_task:
            self.tempban_task.cancel()

    async def check_tempbans(self):
        """Background task to handle automatic tempban expiration."""
        await self.bot.wait_until_ready()
        while not self.bot.is_closed():
            try:
                for guild in self.bot.guilds:
                    tempbans = await self.config.guild(guild).tempbans()
                    now = datetime.utcnow()
                    expired_ids = [
                        int(uid)
                        for uid, ban_info in tempbans.items()
                        if datetime.fromisoformat(ban_info["unban_time"]) <= now
                    ]
                    for user_id in expired_ids:
                        try:
                            user = await self.bot.fetch_user(user_id)
                            await guild.unban(user, reason="Temporary ban expired")
                            async with self.config.guild(guild).tempbans() as tb:
                                tb.pop(str(user_id), None)
                        except Exception:
                            pass
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                break
            except Exception:
                await asyncio.sleep(60)

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
        async with self.config.guild(ctx.guild).moderators() as moderators:
            if user_id not in moderators:
                moderators.append(user_id)
                await ctx.send(f"User ID `{user_id}` added to moderator list.")
            else:
                await ctx.send(f"User ID `{user_id}` is already a moderator.")

    @mods.command()
    async def remove(self, ctx, user: Union[discord.Member, int]):
        """Remove a user from the custom moderator list."""
        user_id = user.id if isinstance(user, discord.Member) else user
        async with self.config.guild(ctx.guild).moderators() as moderators:
            if user_id in moderators:
                moderators.remove(user_id)
                await ctx.send(f"User ID `{user_id}` removed from moderator list.")
            else:
                await ctx.send(f"User ID `{user_id}` is not in the moderator list.")

    @mods.command(name="list")
    async def list_(self, ctx):
        """Show all custom moderators."""
        moderators = await self.config.guild(ctx.guild).moderators()
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

        try:
            try:
                embed = discord.Embed(
                    title=f"Kicked from {ctx.guild.name}",
                    description=f"**Reason:** {reason or 'No reason provided'}",
                    color=0xFEE75C,
                )
                await member.send(embed=embed)
            except:
                pass

            await member.kick(
                reason=f"By {ctx.author}: {reason or 'No reason provided'}"
            )

            success_embed = discord.Embed(
                title="Member Kicked",
                description=f"**{member}** has been kicked.\n**Reason:** {reason or 'No reason provided'}",
                color=0x57F287,
            )
            await ctx.send(embed=success_embed)

        except discord.Forbidden:
            await ctx.send("❌ I don't have permission to kick that member.")
        except Exception as e:
            await ctx.send(f"❌ Could not kick member: {e}")

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

        try:
            try:
                embed = discord.Embed(
                    title=f"Banned from {ctx.guild.name}",
                    description=f"**Reason:** {reason or 'No reason provided'}",
                    color=0xED4245,
                )
                await user.send(embed=embed)
            except:
                pass

            await ctx.guild.ban(
                user,
                reason=f"By {ctx.author}: {reason or 'No reason provided'}",
                delete_message_days=delete_days,
            )

            success_embed = discord.Embed(
                title="Member Banned",
                description=f"**{user}** has been banned.\n**Messages deleted:** {delete_days} day(s)\n**Reason:** {reason or 'No reason provided'}",
                color=0x57F287,
            )
            await ctx.send(embed=success_embed)

        except discord.Forbidden:
            await ctx.send("❌ I don't have permission to ban that user.")
        except Exception as e:
            await ctx.send(f"❌ Could not ban user: {e}")

    @commands.command()
    @commands.guild_only()
    @mod_or_permissions(ban_members=True)
    @commands.bot_has_permissions(ban_members=True)
    async def unban(self, ctx, user_id: int, *, reason: Optional[str] = None):
        """Unban a user by their user ID."""
        try:
            user = await self.bot.fetch_user(user_id)
            await ctx.guild.unban(
                user, reason=f"By {ctx.author}: {reason or 'No reason provided'}"
            )

            # Remove from tempbans if present
            async with self.config.guild(ctx.guild).tempbans() as tempbans:
                tempbans.pop(str(user_id), None)

            success_embed = discord.Embed(
                title="Member Unbanned",
                description=f"**{user}** has been unbanned.\n**Reason:** {reason or 'No reason provided'}",
                color=0x57F287,
            )
            await ctx.send(embed=success_embed)

        except discord.NotFound:
            await ctx.send("❌ That user isn't banned or doesn't exist.")
        except discord.Forbidden:
            await ctx.send("❌ I don't have permission to unban users.")
        except Exception as e:
            await ctx.send(f"❌ Could not unban user: {e}")

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

        try:
            try:
                embed = discord.Embed(
                    title=f"Softbanned from {ctx.guild.name}",
                    description=f"You have been removed and your recent messages deleted.\n**Reason:** {reason or 'No reason provided'}\n\nYou may rejoin the server.",
                    color=0xFEE75C,
                )
                await member.send(embed=embed)
            except:
                pass

            await ctx.guild.ban(
                member,
                reason=f"Softban by {ctx.author}: {reason or 'No reason provided'}",
                delete_message_days=1,
            )
            await ctx.guild.unban(member, reason=f"Softban unban by {ctx.author}")

            success_embed = discord.Embed(
                title="Member Softbanned",
                description=f"**{member}** has been softbanned.\n**Reason:** {reason or 'No reason provided'}",
                color=0x57F287,
            )
            await ctx.send(embed=success_embed)

        except discord.Forbidden:
            await ctx.send("❌ I don't have permission to ban/unban that member.")
        except Exception as e:
            await ctx.send(f"❌ Could not softban member: {e}")

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

        try:
            try:
                embed = discord.Embed(
                    title=f"Temporarily Banned from {ctx.guild.name}",
                    description=f"**Duration:** {duration}\n**Unbanned:** <t:{int(unban_time.timestamp())}:R>\n**Reason:** {reason or 'No reason provided'}",
                    color=0xFEE75C,
                )
                await user.send(embed=embed)
            except:
                pass

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

            success_embed = discord.Embed(
                title="Member Temporarily Banned",
                description=f"**{user}** has been banned for {duration}.\n**Unbanned:** <t:{int(unban_time.timestamp())}:R>\n**Reason:** {reason or 'No reason provided'}",
                color=0x57F287,
            )
            await ctx.send(embed=success_embed)

        except discord.Forbidden:
            await ctx.send("❌ I don't have permission to ban that user.")
        except Exception as e:
            await ctx.send(f"❌ Could not tempban user: {e}")

    @commands.command()
    @commands.guild_only()
    @mod_or_permissions(ban_members=True)
    @commands.bot_has_permissions(ban_members=True)
    async def massban(self, ctx, *user_ids: int):
        """Mass ban multiple users by their IDs."""
        if not user_ids:
            await ctx.send("❌ Please provide at least one user ID.")
            return

        if len(user_ids) > 20:
            await ctx.send("❌ Maximum 20 users can be banned at once.")
            return

        banned = []
        failed = []

        status_msg = await ctx.send(f"⏳ Banning {len(user_ids)} users...")

        for user_id in user_ids:
            try:
                user = await self.bot.fetch_user(user_id)
                await ctx.guild.ban(
                    user, reason=f"Massban by {ctx.author}", delete_message_days=1
                )
                banned.append(f"{user} ({user_id})")
            except discord.NotFound:
                failed.append(f"{user_id} (Not found)")
            except discord.Forbidden:
                failed.append(f"{user_id} (Permission denied)")
            except Exception as e:
                failed.append(f"{user_id} (Error: {e})")

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

        try:
            await member.timeout(
                delta, reason=f"By {ctx.author}: {reason or 'No reason provided'}"
            )

            success_embed = discord.Embed(
                title="Member Timed Out",
                description=f"**{member}** has been timed out for {duration}.\n**Reason:** {reason or 'No reason provided'}",
                color=0x57F287,
            )
            await ctx.send(embed=success_embed)

        except discord.Forbidden:
            await ctx.send("❌ I don't have permission to timeout that member.")
        except Exception as e:
            await ctx.send(f"❌ Could not timeout member: {e}")

    @commands.command(aliases=["unmute"])
    @commands.guild_only()
    @mod_or_permissions(moderate_members=True)
    @commands.bot_has_permissions(moderate_members=True)
    async def untimeout(
        self, ctx, member: discord.Member, *, reason: Optional[str] = None
    ):
        """Remove timeout (unmute) from a member."""
        try:
            await member.timeout(
                None, reason=f"By {ctx.author}: {reason or 'No reason provided'}"
            )

            success_embed = discord.Embed(
                title="Timeout Removed",
                description=f"**{member}**'s timeout has been removed.",
                color=0x57F287,
            )
            await ctx.send(embed=success_embed)

        except discord.Forbidden:
            await ctx.send("❌ I don't have permission to remove timeouts.")
        except Exception as e:
            await ctx.send(f"❌ Could not remove timeout: {e}")

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
                success_embed = discord.Embed(
                    title="Nickname Changed",
                    description=f"**{member}**'s nickname has been changed.\n**Old:** {old_nick}\n**New:** {nickname}",
                    color=0x57F287,
                )
            else:
                success_embed = discord.Embed(
                    title="Nickname Removed",
                    description=f"**{member}**'s nickname has been removed.\n**Old nickname:** {old_nick}",
                    color=0x57F287,
                )

            await ctx.send(embed=success_embed)

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

        except discord.Forbidden:
            await ctx.send("❌ I don't have permission to edit that channel.")
        except Exception as e:
            await ctx.send(f"❌ Could not set slowmode: {e}")

    # ================= Voice Management =================
    @commands.command()
    @commands.guild_only()
    @mod_or_permissions(move_members=True)
    @commands.bot_has_permissions(move_members=True)
    async def voicekick(
        self, ctx, member: discord.Member, *, reason: Optional[str] = None
    ):
        """Kick a member from their current voice channel."""
        if not member.voice:
            await ctx.send("❌ Member is not in a voice channel.")
            return

        if member.top_role >= ctx.author.top_role and ctx.author != ctx.guild.owner:
            await ctx.send("❌ Cannot voice kick someone with equal or higher role.")
            return

        try:
            await member.move_to(
                None,
                reason=f"Voice kicked by {ctx.author}: {reason or 'No reason provided'}",
            )

            success_embed = discord.Embed(
                title="Member Voice Kicked",
                description=f"**{member}** has been kicked from voice.\n**Reason:** {reason or 'No reason provided'}",
                color=0x57F287,
            )
            await ctx.send(embed=success_embed)

        except discord.Forbidden:
            await ctx.send("❌ I don't have permission to move that member.")
        except Exception as e:
            await ctx.send(f"❌ Could not voice kick member: {e}")

        @commands.command()
        @commands.guild_only()
        @mod_or_permissions(mute_members=True)
        @commands.bot_has_permissions(mute_members=True)
        async def voicemute(
            self, ctx, member: discord.Member, *, reason: Optional[str] = None
        ):
            """Server mute a member in voice channels."""
            if member.top_role >= ctx.author.top_role and ctx.author != ctx.guild.owner:
                await ctx.send(
                    "❌ Cannot voice mute someone with equal or higher role."
                )
                return

        if not member.voice:
            await ctx.send("❌ Member is not in a voice channel.")
            return

        if member.voice.mute:
            await ctx.send("❌ Member is already voice muted.")
            return

        try:
            await member.edit(
                mute=True,
                reason=f"Voice muted by {ctx.author}: {reason or 'No reason provided'}",
            )

            success_embed = discord.Embed(
                title="Member Voice Muted",
                description=f"**{member}** has been voice muted.\n**Reason:** {reason or 'No reason provided'}",
                color=0x57F287,
            )
            await ctx.send(embed=success_embed)

        except discord.Forbidden:
            await ctx.send("❌ I don't have permission to mute that member.")
        except Exception as e:
            await ctx.send(f"❌ Could not voice mute member: {e}")

        @commands.command()
        @commands.guild_only()
        @mod_or_permissions(mute_members=True)
        @commands.bot_has_permissions(mute_members=True)
        async def voiceunmute(
            self, ctx, member: discord.Member, *, reason: Optional[str] = None
        ):
            """Server unmute a member in voice channels."""
            if not member.voice:
                await ctx.send("❌ Member is not in a voice channel.")
                return

            if not member.voice.mute:
                await ctx.send("❌ Member is not voice muted.")
                return

            try:
                await member.edit(
                    mute=False,
                    reason=f"Voice unmuted by {ctx.author}: {reason or 'No reason provided'}",
                )

                success_embed = discord.Embed(
                    title="Member Voice Unmuted",
                    description=f"**{member}** has been voice unmuted.",
                    color=0x57F287,
                )
                await ctx.send(embed=success_embed)

            except discord.Forbidden:
                await ctx.send("❌ I don't have permission to unmute that member.")
            except Exception as e:
                await ctx.send(f"❌ Could not voice unmute member: {e}")

    @commands.command()
    @commands.guild_only()
    @mod_or_permissions(mute_members=True, deafen_members=True)
    @commands.bot_has_permissions(mute_members=True, deafen_members=True)
    async def voiceban(
        self, ctx, member: discord.Member, *, reason: Optional[str] = None
    ):
        """Ban a member from speaking and listening in voice channels."""
        if member.top_role >= ctx.author.top_role and ctx.author != ctx.guild.owner:
            await ctx.send("❌ Cannot voice ban someone with equal or higher role.")
            return

        try:
            await member.edit(
                mute=True,
                deafen=True,
                reason=f"Voice banned by {ctx.author}: {reason or 'No reason provided'}",
            )

            success_embed = discord.Embed(
                title="Member Voice Banned",
                description=f"**{member}** has been voice banned (muted and deafened).\n**Reason:** {reason or 'No reason provided'}",
                color=0x57F287,
            )
            await ctx.send(embed=success_embed)

        except discord.Forbidden:
            await ctx.send("❌ I don't have permission to mute/deafen that member.")
        except Exception as e:
            await ctx.send(f"❌ Could not voice ban member: {e}")

    @commands.command()
    @commands.guild_only()
    @mod_or_permissions(mute_members=True, deafen_members=True)
    @commands.bot_has_permissions(mute_members=True, deafen_members=True)
    async def voiceunban(
        self, ctx, member: discord.Member, *, reason: Optional[str] = None
    ):
        """Unban a member from speaking and listening in voice channels."""
        try:
            await member.edit(
                mute=False,
                deafen=False,
                reason=f"Voice unbanned by {ctx.author}: {reason or 'No reason provided'}",
            )

            success_embed = discord.Embed(
                title="Member Voice Unbanned",
                description=f"**{member}** has been voice unbanned (unmuted and undeafened).",
                color=0x57F287,
            )
            await ctx.send(embed=success_embed)

        except discord.Forbidden:
            await ctx.send("❌ I don't have permission to unmute/undeafen that member.")
        except Exception as e:
            await ctx.send(f"❌ Could not voice unban member: {e}")

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

        async with self.config.guild(ctx.guild).blocked_users() as blocked_users:
            if user_id in blocked_users:
                await ctx.send(f"❌ User ID `{user_id}` is already blocked.")
                return
            blocked_users.append(user_id)

        await ctx.send(f"✅ Added user ID `{user_id}` to the message deletion list.")

    @msgblock.command(name="remove")
    async def msgblock_remove(self, ctx: commands.Context, user_id: int):
        """Remove a user from the message deletion list."""
        async with self.config.guild(ctx.guild).blocked_users() as blocked_users:
            if user_id not in blocked_users:
                await ctx.send(f"❌ User ID `{user_id}` is not blocked.")
                return
            blocked_users.remove(user_id)

        await ctx.send(
            f"✅ Removed user ID `{user_id}` from the message deletion list."
        )

    @msgblock.command(name="list")
    async def msgblock_list(self, ctx: commands.Context):
        """Show all blocked users."""
        blocked_users = await self.config.guild(ctx.guild).blocked_users()

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
        """Auto-delete messages from blocked users."""
        if not message.guild or message.author.bot:
            return

        try:
            blocked_users = await self.config.guild(message.guild).blocked_users()
            if message.author.id in blocked_users:
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
