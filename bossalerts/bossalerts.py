from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

import discord
from redbot.core import Config, commands
from redbot.core.bot import Red

log = logging.getLogger("red.bossalerts")

PARACOL_TIMESTAMPS: list[int] = [
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

ALERT_OFFSET: int = 300  # 5 minutes before spawn


class BossAlerts(commands.Cog):
    """Sends ping notifications before Interluminary Parasol and Doom of Caeranthil spawns."""

    def __init__(self, bot: Red) -> None:
        self.bot = bot
        self.config = Config.get_conf(
            self, identifier=0xB055A1E7, force_registration=True
        )
        self.config.register_guild(
            parasol_channel=None,
            parasol_role=None,
            doom_channel=None,
            doom_role=None,
        )
        self._tasks: list[asyncio.Task] = []
        self.bot.loop.create_task(self._wait_and_start())

    async def _wait_and_start(self) -> None:
        await self.bot.wait_until_ready()
        self._tasks.append(self.bot.loop.create_task(self._scheduler_loop("parasol")))
        self._tasks.append(self.bot.loop.create_task(self._scheduler_loop("doom")))

    def cog_unload(self) -> None:
        for task in self._tasks:
            task.cancel()

    async def _scheduler_loop(self, boss_key: str) -> None:
        timestamps = PARACOL_TIMESTAMPS if boss_key == "parasol" else DOOM_TIMESTAMPS
        boss_label = (
            "Interluminary Parasol"
            if boss_key == "parasol"
            else "Doom of Caeranthil (World Serpent)"
        )

        while True:
            now = time.time()
            upcoming = [ts for ts in timestamps if (ts - ALERT_OFFSET) > now]
            if not upcoming:
                log.info("%s: all scheduled spawns have passed, loop exiting.", boss_key)
                return

            next_spawn_ts = min(upcoming)
            sleep_duration = (next_spawn_ts - ALERT_OFFSET) - now

            if sleep_duration > 0:
                await asyncio.sleep(sleep_duration)

            await self._broadcast_alert(boss_key, boss_label, next_spawn_ts)
            await asyncio.sleep(1)  # prevent double-fire on same ts

    async def _broadcast_alert(
        self, boss_key: str, boss_label: str, spawn_ts: int
    ) -> None:
        channel_key = f"{boss_key}_channel"
        role_key = f"{boss_key}_role"

        all_guilds = await self.config.all_guilds()
        for guild_id, guild_data in all_guilds.items():
            channel_id: Optional[int] = guild_data.get(channel_key)
            role_id: Optional[int] = guild_data.get(role_key)
            if not channel_id:
                continue

            guild: Optional[discord.Guild] = self.bot.get_guild(guild_id)
            if not guild:
                continue

            channel = guild.get_channel(channel_id)
            if not isinstance(channel, discord.TextChannel):
                continue

            role_mention = f"<@&{role_id}> " if role_id else ""
            try:
                await channel.send(
                    f"{role_mention}⚠️ **{boss_label}** spawns <t:{spawn_ts}:R> "
                    f"(<t:{spawn_ts}:t>)!"
                )
            except discord.Forbidden:
                log.warning("Missing send permissions in channel %s guild %s", channel_id, guild_id)
            except discord.HTTPException as exc:
                log.error("Failed to send boss alert: %s", exc)

    # ------------------------------------------------------------------
    # Parasol commands
    # ------------------------------------------------------------------

    @commands.group(name="parasol", invoke_without_command=False)
    @commands.guild_only()
    async def parasol_group(self, ctx: commands.Context) -> None:
        """Interluminary Parasol boss alert management."""

    @parasol_group.group(name="ping", invoke_without_command=False)
    @commands.guild_only()
    async def parasol_ping(self, ctx: commands.Context) -> None:
        """Manage Parasol ping settings."""

    @parasol_ping.command(name="add")
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    async def parasol_ping_add(
        self,
        ctx: commands.Context,
        channel: Optional[discord.TextChannel] = None,
        role: Optional[discord.Role] = None,
    ) -> None:
        """
        Register a channel for Interluminary Parasol spawn alerts.
        Optionally mention a role to ping. E.g.: `[p]parasol ping add #alerts @BossRole`
        """
        target_channel = channel or ctx.channel
        await self.config.guild(ctx.guild).parasol_channel.set(target_channel.id)
        role_note = ""
        if role:
            await self.config.guild(ctx.guild).parasol_role.set(role.id)
            role_note = f" and will ping {role.mention}"

        spawns_display = " | ".join(f"<t:{ts}:t>" for ts in PARACOL_TIMESTAMPS)
        await ctx.send(
            f"✅ **Interluminary Parasol** alerts set in {target_channel.mention}{role_note}.\n"
            f"Pings fire **5 min before** each spawn.\n"
            f"Spawns: {spawns_display}"
        )

    @parasol_ping.command(name="remove")
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    async def parasol_ping_remove(self, ctx: commands.Context) -> None:
        """Stop Parasol alerts for this server."""
        await self.config.guild(ctx.guild).parasol_channel.set(None)
        await self.config.guild(ctx.guild).parasol_role.set(None)
        await ctx.send("🗑️ Interluminary Parasol alerts disabled.")

    @parasol_ping.command(name="status")
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    async def parasol_ping_status(self, ctx: commands.Context) -> None:
        """Show current Parasol alert config."""
        channel_id = await self.config.guild(ctx.guild).parasol_channel()
        role_id = await self.config.guild(ctx.guild).parasol_role()
        await ctx.send(
            f"**Parasol alerts** → channel: {'<#' + str(channel_id) + '>' if channel_id else '*not set*'} "
            f"| role: {'<@&' + str(role_id) + '>' if role_id else '*not set*'}"
        )

    # ------------------------------------------------------------------
    # Doom commands
    # ------------------------------------------------------------------

    @commands.group(name="doom", invoke_without_command=False)
    @commands.guild_only()
    async def doom_group(self, ctx: commands.Context) -> None:
        """Doom of Caeranthil boss alert management."""

    @doom_group.group(name="ping", invoke_without_command=False)
    @commands.guild_only()
    async def doom_ping(self, ctx: commands.Context) -> None:
        """Manage Doom ping settings."""

    @doom_ping.command(name="add")
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    async def doom_ping_add(
        self,
        ctx: commands.Context,
        channel: Optional[discord.TextChannel] = None,
        role: Optional[discord.Role] = None,
    ) -> None:
        """
        Register a channel for Doom of Caeranthil spawn alerts.
        Optionally mention a role to ping. E.g.: `[p]doom ping add #alerts @BossRole`
        """
        target_channel = channel or ctx.channel
        await self.config.guild(ctx.guild).doom_channel.set(target_channel.id)
        role_note = ""
        if role:
            await self.config.guild(ctx.guild).doom_role.set(role.id)
            role_note = f" and will ping {role.mention}"

        spawns_display = " | ".join(f"<t:{ts}:t>" for ts in DOOM_TIMESTAMPS)
        await ctx.send(
            f"✅ **Doom of Caeranthil** alerts set in {target_channel.mention}{role_note}.\n"
            f"Pings fire **5 min before** each spawn.\n"
            f"Spawns: {spawns_display}"
        )

    @doom_ping.command(name="remove")
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    async def doom_ping_remove(self, ctx: commands.Context) -> None:
        """Stop Doom alerts for this server."""
        await self.config.guild(ctx.guild).doom_channel.set(None)
        await self.config.guild(ctx.guild).doom_role.set(None)
        await ctx.send("🗑️ Doom of Caeranthil alerts disabled.")

    @doom_ping.command(name="status")
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    async def doom_ping_status(self, ctx: commands.Context) -> None:
        """Show current Doom alert config."""
        channel_id = await self.config.guild(ctx.guild).doom_channel()
        role_id = await self.config.guild(ctx.guild).doom_role()
        await ctx.send(
            f"**Doom alerts** → channel: {'<#' + str(channel_id) + '>' if channel_id else '*not set*'} "
            f"| role: {'<@&' + str(role_id) + '>' if role_id else '*not set*'}"
        )
