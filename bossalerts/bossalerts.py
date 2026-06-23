from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

import discord
from redbot.core import Config, commands
from redbot.core.bot import Red

log = logging.getLogger("red.bossalerts")

PARASOL_TIMESTAMPS: list[int] = [
    1777953600,
    1777959000,
    1777964400,
    1777969800,
    1777975200,
    1777980600,
    1777986000,
    1777991400,
    1777996800,
    1778002200,
    1778007600,
    1778013000,
    1778018400,
    1778023800,
    1778029200,
    1778034600,
]

DOOM_TIMESTAMPS: list[int] = [
    1777957200,
    1777964400,
    1777971600,
    1777978800,
    1777986000,
    1777993200,
    1778000400,
    1778007600,
    1778014800,
    1778022000,
    1778029200,
    1778036400,
]

ALERT_OFFSET_SECONDS: int = 300


class BossAlerts(commands.Cog):
    """Scheduled boss alerts."""

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
            doom_channel=None,
            doom_role=None,
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

    def _get_timestamps(self, boss_key: str) -> list[int]:
        match boss_key:
            case "parasol":
                return PARASOL_TIMESTAMPS
            case "doom":
                return DOOM_TIMESTAMPS
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

    async def _scheduler_loop(self, boss_key: str) -> None:
        await self.bot.wait_until_ready()

        timestamps = self._get_timestamps(boss_key)
        boss_label = self._get_label(boss_key)

        while True:
            now = time.time()
            upcoming_spawns = [
                spawn_ts
                for spawn_ts in timestamps
                if (spawn_ts - ALERT_OFFSET_SECONDS) > now
            ]

            if not upcoming_spawns:
                log.info("%s: no remaining scheduled spawns", boss_key)
                return

            next_spawn_ts = min(upcoming_spawns)
            alert_ts = next_spawn_ts - ALERT_OFFSET_SECONDS
            sleep_for = alert_ts - now

            if sleep_for > 0:
                await asyncio.sleep(sleep_for)

            await self._broadcast_alert(boss_key, boss_label, next_spawn_ts)
            await asyncio.sleep(1)

    async def _broadcast_alert(
        self,
        boss_key: str,
        boss_label: str,
        spawn_ts: int,
    ) -> None:
        channel_key = f"{boss_key}_channel"
        role_key = f"{boss_key}_role"

        all_guilds = await self.config.all_guilds()
        for guild_id, guild_config in all_guilds.items():
            channel_id = guild_config.get(channel_key)
            role_id = guild_config.get(role_key)

            if not isinstance(channel_id, int):
                continue

            guild = self.bot.get_guild(guild_id)
            if guild is None:
                continue

            channel = guild.get_channel(channel_id)
            if not isinstance(channel, discord.TextChannel):
                continue

            role_mention = f"<@&{role_id}> " if isinstance(role_id, int) else ""

            try:
                await channel.send(
                    f"{role_mention}{boss_label} spawns <t:{spawn_ts}:R> (<t:{spawn_ts}:t>)."
                )
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

    async def _set_alert_target(
        self,
        ctx: commands.Context,
        boss_key: str,
        channel: Optional[discord.TextChannel],
        role: Optional[discord.Role],
    ) -> None:
        if ctx.guild is None:
            await ctx.send("This command can only be used in a server.")
            return

        target_channel = channel or ctx.channel
        if not isinstance(target_channel, discord.TextChannel):
            await ctx.send("Target channel must be a text channel.")
            return

        guild_config = self.config.guild(ctx.guild)

        match boss_key:
            case "parasol":
                await guild_config.parasol_channel.set(target_channel.id)
                await guild_config.parasol_role.set(role.id if role else None)
                label = "Interluminary Parasol"
                timestamps = PARASOL_TIMESTAMPS
            case "doom":
                await guild_config.doom_channel.set(target_channel.id)
                await guild_config.doom_role.set(role.id if role else None)
                label = "Doom of Caeranthil"
                timestamps = DOOM_TIMESTAMPS
            case _:
                raise ValueError(f"Unsupported boss key: {boss_key}")

        role_text = f" Role: {role.mention}." if role is not None else ""
        schedule_text = " | ".join(f"<t:{ts}:t>" for ts in timestamps)

        await ctx.send(
            f"{label} alerts set in {target_channel.mention}.{role_text}\n"
            f"Alerts fire 5 minutes before each spawn.\n"
            f"Spawns: {schedule_text}"
        )

    async def _clear_alert_target(self, ctx: commands.Context, boss_key: str) -> None:
        if ctx.guild is None:
            await ctx.send("This command can only be used in a server.")
            return

        guild_config = self.config.guild(ctx.guild)

        match boss_key:
            case "parasol":
                await guild_config.parasol_channel.set(None)
                await guild_config.parasol_role.set(None)
                label = "Interluminary Parasol"
            case "doom":
                await guild_config.doom_channel.set(None)
                await guild_config.doom_role.set(None)
                label = "Doom of Caeranthil"
            case _:
                raise ValueError(f"Unsupported boss key: {boss_key}")

        await ctx.send(f"{label} alerts disabled.")

    async def _show_status(self, ctx: commands.Context, boss_key: str) -> None:
        if ctx.guild is None:
            await ctx.send("This command can only be used in a server.")
            return

        guild_config = self.config.guild(ctx.guild)

        match boss_key:
            case "parasol":
                channel_id = await guild_config.parasol_channel()
                role_id = await guild_config.parasol_role()
                label = "Parasol"
            case "doom":
                channel_id = await guild_config.doom_channel()
                role_id = await guild_config.doom_role()
                label = "Doom"
            case _:
                raise ValueError(f"Unsupported boss key: {boss_key}")

        channel_text = f"<#{channel_id}>" if isinstance(channel_id, int) else "not set"
        role_text = f"<@&{role_id}>" if isinstance(role_id, int) else "not set"

        await ctx.send(f"{label} alerts -> channel: {channel_text} | role: {role_text}")

    @commands.group(name="parasol")
    @commands.guild_only()
    async def parasol(self, ctx: commands.Context) -> None:
        """Parasol alert commands."""

    @parasol.group(name="ping")
    @commands.guild_only()
    async def parasol_ping(self, ctx: commands.Context) -> None:
        """Parasol ping commands."""

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

    @commands.group(name="doom")
    @commands.guild_only()
    async def doom(self, ctx: commands.Context) -> None:
        """Doom alert commands."""

    @doom.group(name="ping")
    @commands.guild_only()
    async def doom_ping(self, ctx: commands.Context) -> None:
        """Doom ping commands."""

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

    @parasol_ping_add.error
    @parasol_ping_remove.error
    @parasol_ping_status.error
    @doom_ping_add.error
    @doom_ping_remove.error
    @doom_ping_status.error
    async def bossalerts_command_error(
        self,
        ctx: commands.Context,
        error: commands.CommandError,
    ) -> None:
        if isinstance(error, commands.MissingPermissions):
            await ctx.send("You need Manage Server to use this command.")
            return
        if isinstance(error, commands.BadArgument):
            await ctx.send("Invalid channel or role.")
            return
        if isinstance(error, commands.TooManyArguments):
            await ctx.send("Too many arguments.")
            return
        raise error