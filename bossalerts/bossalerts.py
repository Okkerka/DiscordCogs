from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Optional

import discord
from redbot.core import Config, commands
from redbot.core.bot import Red

log = logging.getLogger("red.bossalerts")

PARASOL_REFERENCE_TIMESTAMPS: list[int] = [
    1777953600, 1777959000, 1777964400, 1777969800,
    1777975200, 1777980600, 1777986000, 1777991400,
    1777996800, 1778002200, 1778007600, 1778013000,
    1778018400, 1778023800, 1778029200, 1778034600,
]

DOOM_REFERENCE_TIMESTAMPS: list[int] = [
    1777957200, 1777964400, 1777971600, 1777978800,
    1777986000, 1777993200, 1778000400, 1778007600,
    1778014800, 1778022000, 1778029200, 1778036400,
]

DEFAULT_ALERT_OFFSET_SECONDS: int = 300
SECONDS_PER_DAY: int = 86400
BROADCAST_SEND_DELAY: float = 0.1
AUTO_DELETE_DELAY: int = 30
SCHEDULER_IDLE_SLEEP_SECONDS: float = 60.0
MIN_ALERT_OFFSET_SECONDS: int = 60
MAX_ALERT_OFFSET_SECONDS: int = 3600


def _timestamp_to_utc_seconds_of_day(timestamp: int) -> int:
    dt = datetime.fromtimestamp(timestamp, tz=timezone.utc)
    return (dt.hour * 3600) + (dt.minute * 60) + dt.second


class BossAlerts(commands.Cog):
    """Scheduled boss alerts based on official reference timestamps."""

    def __init__(self, bot: Red) -> None:
        self.bot = bot
        self.config = Config.get_conf(
            self,
            identifier=0xB055A1E7,
            force_registration=True,
        )
        self.config.register_guild(
            parasol_channel=None,
            parasol_role=None,
            parasol_enabled=True,
            parasol_offset=DEFAULT_ALERT_OFFSET_SECONDS,
            doom_channel=None,
            doom_role=None,
            doom_enabled=True,
            doom_offset=DEFAULT_ALERT_OFFSET_SECONDS,
        )
        self._scheduler_tasks: dict[str, asyncio.Task[None]] = {}

    async def cog_load(self) -> None:
        self._scheduler_tasks["parasol"] = asyncio.create_task(
            self._scheduler_loop("parasol")
        )
        self._scheduler_tasks["doom"] = asyncio.create_task(
            self._scheduler_loop("doom")
        )

    def cog_unload(self) -> None:
        for task in self._scheduler_tasks.values():
            task.cancel()
        self._scheduler_tasks.clear()

    def _get_reference_timestamps(self, boss_key: str) -> list[int]:
        match boss_key:
            case "parasol":
                return PARASOL_REFERENCE_TIMESTAMPS
            case "doom":
                return DOOM_REFERENCE_TIMESTAMPS
            case _:
                raise ValueError(f"Unsupported boss key: {boss_key}")

    def _get_label(self, boss_key: str) -> str:
        match boss_key:
            case "parasol":
                return "Interluminary Parasol"
            case "doom":
                return "Doom of Caeranthil (World Serpent)"
            case _:
                raise ValueError(f"Unsupported boss key: {boss_key}")

    def _get_daily_schedule_seconds(self, boss_key: str) -> list[int]:
        reference_timestamps = self._get_reference_timestamps(boss_key)
        seconds_of_day = sorted(
            {_timestamp_to_utc_seconds_of_day(ts) for ts in reference_timestamps}
        )
        return seconds_of_day

    def _get_guild_offset(self, boss_key: str, guild_config: dict) -> int:
        offset_key = f"{boss_key}_offset"
        offset = guild_config.get(offset_key)
        return int(offset) if isinstance(offset, int) else DEFAULT_ALERT_OFFSET_SECONDS

    def _get_next_spawn_timestamp(self, boss_key: str, offset: int) -> int:
        now = int(time.time())
        now_dt = datetime.fromtimestamp(now, tz=timezone.utc)
        today_midnight = int(
            datetime(
                year=now_dt.year,
                month=now_dt.month,
                day=now_dt.day,
                tzinfo=timezone.utc,
            ).timestamp()
        )

        daily_schedule_seconds = self._get_daily_schedule_seconds(boss_key)

        for seconds_of_day in daily_schedule_seconds:
            candidate_spawn = today_midnight + seconds_of_day
            if candidate_spawn - offset > now:
                return candidate_spawn

        return today_midnight + SECONDS_PER_DAY + daily_schedule_seconds[0]

    async def _delete_messages_after(
        self,
        delay: int,
        *messages: discord.Message,
    ) -> None:
        await asyncio.sleep(delay)
        for msg in messages:
            try:
                await msg.delete()
            except Exception:
                pass

    async def _delete_message_after(self, delay: float, message: discord.Message) -> None:
        await asyncio.sleep(delay)
        try:
            await message.delete()
        except Exception:
            pass

    async def _scheduler_loop(self, boss_key: str) -> None:
        await self.bot.wait_until_ready()
        boss_label = self._get_label(boss_key)

        while True:
            try:
                all_guilds = await self.config.all_guilds()
                # Every guild has its own lead time, so the next wake-up is the
                # earliest alert that is still ahead of us.
                pending = []
                for guild_id, guild_config in all_guilds.items():
                    if not guild_config.get(f"{boss_key}_enabled", True):
                        continue
                    offset = self._get_guild_offset(boss_key, guild_config)
                    spawn_ts = self._get_next_spawn_timestamp(boss_key, offset)
                    pending.append((spawn_ts - offset, guild_id, guild_config, spawn_ts, offset))

                if not pending:
                    await asyncio.sleep(SCHEDULER_IDLE_SLEEP_SECONDS)
                    continue

                pending.sort(key=lambda item: item[0])
                sleep_for = pending[0][0] - time.time()
                if sleep_for > 0:
                    await asyncio.sleep(sleep_for)

                now = time.time()
                for alert_at, guild_id, guild_config, spawn_ts, offset in pending:
                    # Only the guilds whose own lead time has come are alerted.
                    if alert_at > now + BROADCAST_SEND_DELAY:
                        continue
                    await self._send_alert(
                        boss_key, boss_label, guild_id, guild_config, spawn_ts, offset
                    )
                await asyncio.sleep(1)
            except asyncio.CancelledError:
                return
            except Exception as exc:
                log.error(
                    "Unexpected error in %s scheduler loop: %s",
                    boss_key,
                    exc,
                    exc_info=True,
                )
                await asyncio.sleep(60)

    async def _send_alert(
        self,
        boss_key: str,
        boss_label: str,
        guild_id: int,
        guild_config: dict,
        spawn_ts: int,
        offset: int,
    ) -> None:
        """Send one guild's alert at its own lead time."""
        channel_key = f"{boss_key}_channel"
        role_key = f"{boss_key}_role"

        channel_id = guild_config.get(channel_key)
        role_id = guild_config.get(role_key)

        if not isinstance(channel_id, int):
            return

        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return

        channel = guild.get_channel(channel_id)
        if not isinstance(channel, discord.TextChannel):
            return

        role = guild.get_role(role_id) if isinstance(role_id, int) else None
        role_mention = f"{role.mention} " if role is not None else ""
        allowed_mentions = discord.AllowedMentions(roles=[role] if role is not None else [])

        try:
            msg = await channel.send(
                f"{role_mention}{boss_label} spawns <t:{spawn_ts}:R> (<t:{spawn_ts}:t>).",
                allowed_mentions=allowed_mentions,
            )
            delete_after = (spawn_ts + offset) - time.time()
            if delete_after > 0:
                asyncio.create_task(self._delete_message_after(delete_after, msg))
        except discord.Forbidden:
            log.warning(
                "Missing send permission in guild=%s channel=%s",
                guild_id,
                channel_id,
            )
        except discord.HTTPException as exc:
            log.error(
                "Failed to send alert in guild=%s channel=%s: %s",
                guild_id,
                channel_id,
                exc,
            )

        await asyncio.sleep(BROADCAST_SEND_DELAY)

    @commands.Cog.listener()
    async def on_guild_remove(self, guild: discord.Guild) -> None:
        await self.config.guild(guild).parasol_channel.set(None)
        await self.config.guild(guild).parasol_role.set(None)
        await self.config.guild(guild).parasol_enabled.set(True)
        await self.config.guild(guild).parasol_offset.set(DEFAULT_ALERT_OFFSET_SECONDS)
        await self.config.guild(guild).doom_channel.set(None)
        await self.config.guild(guild).doom_role.set(None)
        await self.config.guild(guild).doom_enabled.set(True)
        await self.config.guild(guild).doom_offset.set(DEFAULT_ALERT_OFFSET_SECONDS)
        log.info("Cleaned up boss alert config for removed guild %s", guild.id)

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel) -> None:
        if not isinstance(channel, discord.TextChannel):
            return

        for boss_key in ("parasol", "doom"):
            channel_key = f"{boss_key}_channel"
            config = self.config.guild(channel.guild)
            current_channel_id = await getattr(config, channel_key)()
            if current_channel_id == channel.id:
                await getattr(config, channel_key).set(None)
                log.info(
                    "Auto-cleared %s channel for guild %s after channel deletion",
                    boss_key,
                    channel.guild.id,
                )

    async def _set_alert_target(
        self,
        ctx: commands.Context,
        boss_key: str,
        channel: Optional[discord.TextChannel],
        role: Optional[discord.Role],
    ) -> None:
        if ctx.guild is None:
            response = await ctx.send("This command can only be used in a server.")
            asyncio.create_task(self._delete_messages_after(AUTO_DELETE_DELAY, ctx.message, response))
            return

        target_channel = channel or ctx.channel
        if not isinstance(target_channel, discord.TextChannel):
            response = await ctx.send("Target channel must be a text channel.")
            asyncio.create_task(self._delete_messages_after(AUTO_DELETE_DELAY, ctx.message, response))
            return

        guild_config = self.config.guild(ctx.guild)

        match boss_key:
            case "parasol":
                await guild_config.parasol_channel.set(target_channel.id)
                await guild_config.parasol_role.set(role.id if role else None)
                label = "Interluminary Parasol"
            case "doom":
                await guild_config.doom_channel.set(target_channel.id)
                await guild_config.doom_role.set(role.id if role else None)
                label = "Doom of Caeranthil"
            case _:
                raise ValueError(f"Unsupported boss key: {boss_key}")

        offset = await getattr(guild_config, f"{boss_key}_offset")()
        next_spawn_ts = self._get_next_spawn_timestamp(boss_key, offset)
        next_minutes = int((next_spawn_ts - time.time()) / 60)
        role_text = f" Role: {role.mention}." if role is not None else ""

        response = await ctx.send(
            f"{label} alerts set in {target_channel.mention}.{role_text}\n"
            f"Alerts fire {offset // 60} minutes before each spawn.\n"
            f"Next spawn: <t:{next_spawn_ts}:F> ({next_minutes} min)."
        )
        asyncio.create_task(self._delete_messages_after(AUTO_DELETE_DELAY, ctx.message, response))

    async def _set_offset(self, ctx: commands.Context, boss_key: str, minutes: int) -> None:
        """Store the lead time this server wants for its alerts."""
        if ctx.guild is None:
            response = await ctx.send("This command can only be used in a server.")
            asyncio.create_task(self._delete_messages_after(AUTO_DELETE_DELAY, ctx.message, response))
            return

        offset = minutes * 60
        if not MIN_ALERT_OFFSET_SECONDS <= offset <= MAX_ALERT_OFFSET_SECONDS:
            response = await ctx.send(
                f"Choose a lead time between {MIN_ALERT_OFFSET_SECONDS // 60} and "
                f"{MAX_ALERT_OFFSET_SECONDS // 60} minutes."
            )
            asyncio.create_task(self._delete_messages_after(AUTO_DELETE_DELAY, ctx.message, response))
            return

        guild_config = self.config.guild(ctx.guild)
        await getattr(guild_config, f"{boss_key}_offset").set(offset)
        label = self._get_label(boss_key)
        next_spawn_ts = self._get_next_spawn_timestamp(boss_key, offset)

        response = await ctx.send(
            f"{label} alerts now fire {minutes} minutes before each spawn.\n"
            f"Next spawn: <t:{next_spawn_ts}:F> (<t:{next_spawn_ts}:R>)."
        )
        asyncio.create_task(self._delete_messages_after(AUTO_DELETE_DELAY, ctx.message, response))

    async def _toggle_alert(
        self,
        ctx: commands.Context,
        boss_key: str,
        state: bool,
    ) -> None:
        if ctx.guild is None:
            response = await ctx.send("This command can only be used in a server.")
            asyncio.create_task(self._delete_messages_after(AUTO_DELETE_DELAY, ctx.message, response))
            return

        guild_config = self.config.guild(ctx.guild)
        enabled_key = f"{boss_key}_enabled"
        await getattr(guild_config, enabled_key).set(state)

        label = self._get_label(boss_key)
        status_text = "enabled" if state else "disabled"
        response = await ctx.send(f"{label} alerts {status_text}.")
        asyncio.create_task(self._delete_messages_after(AUTO_DELETE_DELAY, ctx.message, response))

    async def _clear_alert_target(self, ctx: commands.Context, boss_key: str) -> None:
        if ctx.guild is None:
            response = await ctx.send("This command can only be used in a server.")
            asyncio.create_task(self._delete_messages_after(AUTO_DELETE_DELAY, ctx.message, response))
            return

        guild_config = self.config.guild(ctx.guild)

        match boss_key:
            case "parasol":
                await guild_config.parasol_channel.set(None)
                await guild_config.parasol_role.set(None)
                await guild_config.parasol_enabled.set(True)
                await guild_config.parasol_offset.set(DEFAULT_ALERT_OFFSET_SECONDS)
                label = "Interluminary Parasol"
            case "doom":
                await guild_config.doom_channel.set(None)
                await guild_config.doom_role.set(None)
                await guild_config.doom_enabled.set(True)
                await guild_config.doom_offset.set(DEFAULT_ALERT_OFFSET_SECONDS)
                label = "Doom of Caeranthil"
            case _:
                raise ValueError(f"Unsupported boss key: {boss_key}")

        response = await ctx.send(f"{label} alerts cleared.")
        asyncio.create_task(self._delete_messages_after(AUTO_DELETE_DELAY, ctx.message, response))

    async def _show_status(self, ctx: commands.Context, boss_key: str) -> None:
        if ctx.guild is None:
            response = await ctx.send("This command can only be used in a server.")
            asyncio.create_task(self._delete_messages_after(AUTO_DELETE_DELAY, ctx.message, response))
            return

        guild_config = self.config.guild(ctx.guild)

        match boss_key:
            case "parasol":
                channel_id = await guild_config.parasol_channel()
                role_id = await guild_config.parasol_role()
                enabled = await guild_config.parasol_enabled()
                offset = await guild_config.parasol_offset()
                label = "Parasol"
            case "doom":
                channel_id = await guild_config.doom_channel()
                role_id = await guild_config.doom_role()
                enabled = await guild_config.doom_enabled()
                offset = await guild_config.doom_offset()
                label = "Doom"
            case _:
                raise ValueError(f"Unsupported boss key: {boss_key}")

        channel_text = f"<#{channel_id}>" if isinstance(channel_id, int) else "not set"
        role_text = f"<@&{role_id}>" if isinstance(role_id, int) else "not set"
        enabled_text = "on" if enabled else "off"
        next_spawn_ts = self._get_next_spawn_timestamp(boss_key, offset)
        next_minutes = int((next_spawn_ts - time.time()) / 60)

        response = await ctx.send(
            f"{label} alerts: {enabled_text}\n"
            f"Channel: {channel_text} | Role: {role_text}\n"
            f"Offset: {offset // 60} minutes before spawn\n"
            f"Next spawn: <t:{next_spawn_ts}:F> ({next_minutes} min)."
        )
        asyncio.create_task(self._delete_messages_after(AUTO_DELETE_DELAY, ctx.message, response))

    async def _show_next(self, ctx: commands.Context, boss_key: str) -> None:
        guild_config = self.config.guild(ctx.guild) if ctx.guild else None
        offset = DEFAULT_ALERT_OFFSET_SECONDS
        if guild_config is not None:
            offset = await getattr(guild_config, f"{boss_key}_offset")()

        next_spawn_ts = self._get_next_spawn_timestamp(boss_key, offset)
        label = self._get_label(boss_key)

        response = await ctx.send(
            f"Next {label} spawn: <t:{next_spawn_ts}:F> (<t:{next_spawn_ts}:R>)."
        )
        asyncio.create_task(self._delete_messages_after(AUTO_DELETE_DELAY, ctx.message, response))

    async def _show_list(self, ctx: commands.Context, boss_key: str) -> None:
        label = self._get_label(boss_key)
        daily_seconds = self._get_daily_schedule_seconds(boss_key)
        now = int(time.time())
        now_dt = datetime.fromtimestamp(now, tz=timezone.utc)
        today_midnight = int(
            datetime(
                year=now_dt.year,
                month=now_dt.month,
                day=now_dt.day,
                tzinfo=timezone.utc,
            ).timestamp()
        )

        lines = []
        for seconds in daily_seconds:
            ts = today_midnight + seconds
            if ts > now:
                lines.append(f"<t:{ts}:F> (<t:{ts}:R>)")

        if not lines:
            tomorrow_midnight = today_midnight + SECONDS_PER_DAY
            for seconds in daily_seconds:
                ts = tomorrow_midnight + seconds
                lines.append(f"<t:{ts}:F> (<t:{ts}:R>)")

        response = await ctx.send(
            f"**{label} daily schedule** (next 24 hours):\n" + "\n".join(lines)
        )
        asyncio.create_task(self._delete_messages_after(AUTO_DELETE_DELAY, ctx.message, response))

    async def _sync_scheduler(self, ctx: commands.Context, boss_key: str) -> None:
        label = self._get_label(boss_key)
        task = self._scheduler_tasks.get(boss_key)

        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        self._scheduler_tasks[boss_key] = asyncio.create_task(
            self._scheduler_loop(boss_key)
        )

        offset = await getattr(
            self.config.guild(ctx.guild) if ctx.guild else self.config,
            f"{boss_key}_offset",
            lambda: DEFAULT_ALERT_OFFSET_SECONDS,
        )()
        next_spawn_ts = self._get_next_spawn_timestamp(boss_key, offset)
        minutes_remaining = int((next_spawn_ts - time.time()) / 60)

        response = await ctx.send(
            f"{label} scheduler synced.\n"
            f"Next spawn: <t:{next_spawn_ts}:F> (<t:{next_spawn_ts}:R>)\n"
            f"Minutes remaining: {minutes_remaining}"
        )
        asyncio.create_task(self._delete_messages_after(AUTO_DELETE_DELAY, ctx.message, response))

    @commands.group(name="parasol")
    @commands.guild_only()
    async def parasol(self, ctx: commands.Context) -> None:
        """Parasol alert commands."""

    @parasol.group(name="ping")
    @commands.guild_only()
    async def parasol_ping(self, ctx: commands.Context) -> None:
        """Parasol ping configuration."""

    @parasol_ping.command(name="add")
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    async def parasol_ping_add(
        self,
        ctx: commands.Context,
        channel: Optional[discord.TextChannel] = None,
        role: Optional[discord.Role] = None,
    ) -> None:
        await self._set_alert_target(ctx, "parasol", channel, role)

    @parasol_ping.command(name="remove")
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    async def parasol_ping_remove(self, ctx: commands.Context) -> None:
        await self._clear_alert_target(ctx, "parasol")

    @parasol_ping.command(name="status")
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    async def parasol_ping_status(self, ctx: commands.Context) -> None:
        await self._show_status(ctx, "parasol")

    @parasol_ping.command(name="on")
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    async def parasol_ping_on(self, ctx: commands.Context) -> None:
        await self._toggle_alert(ctx, "parasol", True)

    @parasol_ping.command(name="off")
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    async def parasol_ping_off(self, ctx: commands.Context) -> None:
        await self._toggle_alert(ctx, "parasol", False)

    @parasol_ping.command(name="offset")
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    async def parasol_ping_offset(self, ctx: commands.Context, minutes: int) -> None:
        """Set how many minutes before a spawn the alert fires."""
        await self._set_offset(ctx, "parasol", minutes)

    @parasol.command(name="next")
    @commands.guild_only()
    async def parasol_next(self, ctx: commands.Context) -> None:
        """Show next Parasol spawn."""
        await self._show_next(ctx, "parasol")

    @parasol.command(name="list")
    @commands.guild_only()
    async def parasol_list(self, ctx: commands.Context) -> None:
        """Show all upcoming Parasol spawns."""
        await self._show_list(ctx, "parasol")

    @parasol.command(name="sync")
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    async def parasol_sync(self, ctx: commands.Context) -> None:
        """Force sync the Parasol scheduler."""
        await self._sync_scheduler(ctx, "parasol")

    @commands.group(name="doom")
    @commands.guild_only()
    async def doom(self, ctx: commands.Context) -> None:
        """Doom alert commands."""

    @doom.group(name="ping")
    @commands.guild_only()
    async def doom_ping(self, ctx: commands.Context) -> None:
        """Doom ping configuration."""

    @doom_ping.command(name="add")
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    async def doom_ping_add(
        self,
        ctx: commands.Context,
        channel: Optional[discord.TextChannel] = None,
        role: Optional[discord.Role] = None,
    ) -> None:
        await self._set_alert_target(ctx, "doom", channel, role)

    @doom_ping.command(name="remove")
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    async def doom_ping_remove(self, ctx: commands.Context) -> None:
        await self._clear_alert_target(ctx, "doom")

    @doom_ping.command(name="status")
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    async def doom_ping_status(self, ctx: commands.Context) -> None:
        await self._show_status(ctx, "doom")

    @doom_ping.command(name="on")
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    async def doom_ping_on(self, ctx: commands.Context) -> None:
        await self._toggle_alert(ctx, "doom", True)

    @doom_ping.command(name="off")
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    async def doom_ping_off(self, ctx: commands.Context) -> None:
        await self._toggle_alert(ctx, "doom", False)

    @doom_ping.command(name="offset")
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    async def doom_ping_offset(self, ctx: commands.Context, minutes: int) -> None:
        """Set how many minutes before a spawn the alert fires."""
        await self._set_offset(ctx, "doom", minutes)

    @doom.command(name="next")
    @commands.guild_only()
    async def doom_next(self, ctx: commands.Context) -> None:
        """Show next Doom spawn."""
        await self._show_next(ctx, "doom")

    @doom.command(name="list")
    @commands.guild_only()
    async def doom_list(self, ctx: commands.Context) -> None:
        """Show all upcoming Doom spawns."""
        await self._show_list(ctx, "doom")

    @doom.command(name="sync")
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    async def doom_sync(self, ctx: commands.Context) -> None:
        """Force sync the Doom scheduler."""
        await self._sync_scheduler(ctx, "doom")

    @parasol_ping_add.error
    @parasol_ping_remove.error
    @parasol_ping_status.error
    @parasol_ping_on.error
    @parasol_ping_off.error
    @parasol_ping_offset.error
    @doom_ping_add.error
    @doom_ping_remove.error
    @doom_ping_status.error
    @doom_ping_on.error
    @doom_ping_off.error
    @doom_ping_offset.error
    @parasol_sync.error
    @doom_sync.error
    async def bossalerts_command_error(
        self,
        ctx: commands.Context,
        error: commands.CommandError,
    ) -> None:
        if isinstance(error, commands.MissingPermissions):
            response = await ctx.send("You need Manage Server to use this command.")
            asyncio.create_task(self._delete_messages_after(AUTO_DELETE_DELAY, ctx.message, response))
            return
        if isinstance(error, commands.BadArgument):
            response = await ctx.send("Invalid channel or role.")
            asyncio.create_task(self._delete_messages_after(AUTO_DELETE_DELAY, ctx.message, response))
            return
        if isinstance(error, commands.TooManyArguments):
            response = await ctx.send("Too many arguments.")
            asyncio.create_task(self._delete_messages_after(AUTO_DELETE_DELAY, ctx.message, response))
            return
        raise error