"""Configurable chat alerts with bounded dispatch and Components V2 controls."""

from __future__ import annotations

import asyncio
import copy
import logging
import time
from collections import defaultdict
from typing import Literal
from urllib.parse import urlsplit

import aiohttp
import discord
from redbot.core import Config, commands
from redbot.core.bot import Red

try:
    import lavalink

    LAVALINK_AVAILABLE = True
except ImportError:
    lavalink = None
    LAVALINK_AVAILABLE = False

log = logging.getLogger("red.chattriggers")
AudioMode = Literal["skip", "interrupt"]


def validate_trigger(phrase: str, data: dict) -> dict:
    """Validate input before storing it or constructing a Discord payload."""
    phrase = phrase.strip()
    if not 1 <= len(phrase) <= 50:
        raise ValueError("The phrase must contain 1–50 characters.")
    result = dict(data, phrase_case=phrase)
    for key, limit in (("sound", 2048), ("gif", 2048), ("title", 256), ("desc", 4000)):
        value = result.get(key, "")
        if not isinstance(value, str) or len(value.strip()) > limit:
            raise ValueError(f"{key} must be text of at most {limit} characters.")
        result[key] = value.strip()
    if not any(result[key] for key in ("sound", "gif", "title", "desc")):
        raise ValueError("Add a sound URL, image URL, title, or message.")
    for key in ("sound", "gif"):
        if not result[key]:
            continue
        try:
            url = urlsplit(result[key])
            valid = (
                url.scheme in ("http", "https")
                and url.hostname
                and not url.username
                and not url.password
            )
            if any(char.isspace() for char in result[key]):
                valid = False
            _ = url.port
        except ValueError:
            valid = False
        if not valid:
            raise ValueError(f"{key} must be an HTTP(S) URL without credentials.")
    if result.get("audio_mode", "skip") not in ("skip", "interrupt"):
        raise ValueError("Choose skip or interrupt for music behavior.")
    if (
        type(result.get("cooldown", 30)) is not int
        or not 0 <= result.get("cooldown", 30) <= 3600
    ):
        raise ValueError("Cooldown must be 0–3,600 seconds.")
    channels = result.get("channels", [])
    if (
        not isinstance(channels, list)
        or len(channels) > 25
        or any(type(cid) is not int or cid <= 0 for cid in channels)
    ):
        raise ValueError("Choose up to 25 valid channels.")
    return result


class ChatTriggers(commands.Cog):
    """Trigger text, image and optional Lavalink audio alerts from chat."""

    def __init__(self, bot: Red) -> None:
        self.bot = bot
        self.config = Config.get_conf(
            self, identifier=999888777, force_registration=True
        )
        self.config.register_guild(triggers={}, allowed_users=[], admin_users=[])
        self._settings: dict[int, dict] = {}
        self._locks = defaultdict(asyncio.Lock)
        self._load_locks = defaultdict(asyncio.Lock)
        self._audio_locks = defaultdict(asyncio.Lock)
        self._last_fired: dict[tuple[int, str], float] = {}
        self._busy: set[int] = set()
        self._views: set = set()
        self._closed = False

    def cog_unload(self) -> None:
        self._closed = True
        for view in list(self._views):
            view.stop()
        self._views.clear()

    async def cog_check(self, ctx: commands.Context) -> bool:
        """Every trigger command requires a server, including hybrid children."""
        if ctx.guild is None:
            raise commands.NoPrivateMessage()
        return True

    async def get_settings(self, guild_id: int) -> dict:
        """Read a detached snapshot; legacy triggers keep interrupt behavior."""
        if guild_id not in self._settings:
            async with self._load_locks[guild_id]:
                if guild_id not in self._settings:
                    settings = await self.config.guild_from_id(guild_id).all()
                    for data in settings["triggers"].values():
                        data.setdefault("audio_mode", "interrupt")
                        data.setdefault("cooldown", 0)
                        data.setdefault("channels", [])
                        data.setdefault("active", True)
                    self._settings[guild_id] = settings
        return copy.deepcopy(self._settings[guild_id])

    async def _persist(self, guild_id: int, settings: dict) -> None:
        await self.config.guild_from_id(guild_id).set(settings)
        self._settings[guild_id] = settings

    async def save_trigger(
        self,
        guild_id: int,
        phrase: str,
        data: dict,
        *,
        old_key: str | None = None,
        expected: dict | None = None,
    ) -> str:
        """Create or edit without clobbering another trigger or a stale edit."""
        key = phrase.strip().lower()
        async with self._locks[guild_id]:
            settings = await self.get_settings(guild_id)
            triggers = settings["triggers"]
            if old_key is not None and old_key not in triggers:
                raise ValueError("This trigger was deleted. Refresh the panel.")
            if key in triggers and key != old_key:
                raise ValueError("A trigger with that phrase already exists.")
            if expected is not None and triggers.get(old_key) != expected:
                raise ValueError(
                    "This trigger changed while you were editing. Reopen the editor."
                )
            base = (
                triggers[old_key]
                if old_key is not None
                else {
                    "active": True,
                    "cooldown": 30,
                    "channels": [],
                    "audio_mode": "skip",
                }
            )
            updated = validate_trigger(phrase, {**base, **data})
            if old_key is not None and old_key != key:
                del triggers[old_key]
            triggers[key] = updated
            await self._persist(guild_id, settings)
            if old_key is not None and old_key != key:
                fired = self._last_fired.pop((guild_id, old_key), None)
                if fired is not None:
                    self._last_fired[guild_id, key] = fired
        return key

    async def update_trigger(self, guild_id: int, key: str, **changes) -> None:
        """Update options atomically against the latest trigger data."""
        async with self._locks[guild_id]:
            settings = await self.get_settings(guild_id)
            if key not in settings["triggers"]:
                raise ValueError("Trigger not found. Refresh the panel.")
            previous = settings["triggers"][key]
            settings["triggers"][key] = validate_trigger(
                previous.get("phrase_case", key), {**previous, **changes}
            )
            await self._persist(guild_id, settings)

    async def delete_trigger(self, guild_id: int, key: str, expected: dict) -> None:
        """Only delete the version shown in the confirmation."""
        async with self._locks[guild_id]:
            settings = await self.get_settings(guild_id)
            if settings["triggers"].get(key) != expected:
                raise ValueError(
                    "The trigger changed or was deleted. Refresh before deleting."
                )
            del settings["triggers"][key]
            await self._persist(guild_id, settings)
            self._last_fired.pop((guild_id, key), None)

    async def change_permission(
        self, guild_id: int, field: str, user_id: int, enabled: bool
    ) -> None:
        """Update an existing permission list without losing concurrent changes."""
        if field not in ("allowed_users", "admin_users"):
            raise ValueError("Unknown permission list.")
        async with self._locks[guild_id]:
            settings = await self.get_settings(guild_id)
            users = settings[field]
            if enabled and user_id not in users:
                users.append(user_id)
            elif not enabled and user_id in users:
                users.remove(user_id)
            await self._persist(guild_id, settings)

    async def can_manage(self, user: discord.Member, guild_id: int) -> bool:
        if user.guild_permissions.manage_guild or await self.bot.is_owner(user):
            return True
        return user.id in (await self.get_settings(guild_id))["admin_users"]

    async def is_admin_or_manager(self, ctx: commands.Context) -> bool:
        return bool(ctx.guild) and await self.can_manage(ctx.author, ctx.guild.id)

    async def _play_sound(self, channel, user, data: dict) -> str:
        if not data.get("sound"):
            return "No sound configured."
        if not LAVALINK_AVAILABLE or self.bot.get_cog("Audio") is None:
            return "Sound skipped: Red Audio/Lavalink is not available."
        # Native music owns its voice connection; do not connect a second backend.
        if self.bot.get_cog("TidalPlayerExp") is not None:
            return "Sound skipped: the native music player owns voice."
        target = getattr(getattr(user, "voice", None), "channel", None)
        if target is None:
            return "Sound skipped: join a voice channel first."
        permissions = target.permissions_for(channel.guild.me)
        if not permissions.connect or not permissions.speak:
            return "Sound skipped: missing Connect or Speak permission."
        async with self._audio_locks[channel.guild.id]:
            try:
                async with asyncio.timeout(20):
                    try:
                        player = lavalink.get_player(channel.guild.id)
                    except lavalink.PlayerNotFound:
                        player = None
                    if (
                        player is not None
                        and (player.current is not None or player.queue)
                        and data.get("audio_mode", "interrupt") == "skip"
                    ):
                        return "Sound skipped: music is playing or queued."
                    if player is None:
                        player = await lavalink.connect(target)
                    # Resolve before stopping; invalid links must not destroy a queue.
                    results = await player.load_tracks(data["sound"])
                    if not results.tracks:
                        return "Sound unavailable: no playable track found."
                    if self._closed:
                        return "Sound skipped: cog unloaded."
                    if getattr(getattr(user, "voice", None), "channel", None) != target:
                        return "Sound skipped: your voice channel changed."
                    if data.get("audio_mode", "interrupt") == "skip" and (
                        player.current is not None or player.queue
                    ):
                        return "Sound skipped: music started while the sound loaded."
                    if player.channel is None or player.channel.id != target.id:
                        await player.move_to(target)
                    if data.get("audio_mode", "interrupt") == "skip" and (
                        player.current is not None or player.queue
                    ):
                        return "Sound skipped: music started while connecting."
                    if player.current is not None or player.is_playing:
                        await player.stop()
                    player.queue.clear()
                    player.add(user, results.tracks[0])
                    await player.play()
                    return "Sound played."
            except (
                lavalink.RedLavalinkException,
                discord.DiscordException,
                aiohttp.ClientError,
                TimeoutError,
                OSError,
                ValueError,
            ) as error:
                # Provider failures must not expose URLs or tokens in logs.
                log.warning(
                    "Trigger sound failed in guild %s (%s)",
                    channel.guild.id,
                    type(error).__name__,
                )
                return "Sound could not be played. Check Red Audio and the source link."

    async def play_trigger(self, channel, user, data: dict) -> str:
        """Deliver the visual alert even when its optional sound is skipped."""
        sound_status = await self._play_sound(channel, user, data)
        if self._closed:
            return "Cog unloaded."
        if any(data.get(key) for key in ("title", "desc", "gif")):
            permissions = channel.permissions_for(channel.guild.me)
            if permissions.embed_links:
                embed = discord.Embed(
                    title=(data.get("title") or "")[:256] or None,
                    description=(data.get("desc") or "")[:4000] or None,
                    color=discord.Color.red(),
                )
                embed.set_footer(
                    text=f"Triggered by {user.display_name}"[:2048],
                    icon_url=user.display_avatar.url,
                )
                if data.get("gif"):
                    embed.set_image(url=data["gif"])
                await channel.send(
                    embed=embed, allowed_mentions=discord.AllowedMentions.none()
                )
            else:
                text = "\n".join(
                    data.get(key, "")
                    for key in ("title", "desc", "gif")
                    if data.get(key)
                )
                await channel.send(
                    text[:1900], allowed_mentions=discord.AllowedMentions.none()
                )
        return sound_status

    async def fire(self, channel, user, key: str, *, testing: bool = False) -> str:
        """Reserve cooldown before I/O; at most one alert per guild is in flight."""
        guild_id = channel.guild.id
        if self._closed or await self.bot.cog_disabled_in_guild(self, channel.guild):
            return "ChatTriggers is disabled."
        if not await self.bot.allowed_by_whitelist_blacklist(user):
            return "You cannot use ChatTriggers."
        if testing and not await self.can_manage(user, guild_id):
            return "You no longer have trigger management permission."
        is_owner = await self.bot.is_owner(user)
        async with self._locks[guild_id]:
            settings = await self.get_settings(guild_id)
            data = settings["triggers"].get(key)
            if data is None:
                return "Trigger not found."
            if not testing and not (
                is_owner
                or user.id in settings["allowed_users"]
                or user.id in settings["admin_users"]
            ):
                return "You are not permitted to fire triggers."
            if not testing and not data["active"]:
                return "Trigger is disabled."
            channels = (channel.id, getattr(channel, "parent_id", None))
            if data["channels"] and not set(channels).intersection(data["channels"]):
                return "This channel is not allowed for that trigger."
            now = time.monotonic()
            cooldown = max(data["cooldown"], 3 if testing else 0)
            last = self._last_fired.get((guild_id, key))
            if guild_id in self._busy or (last is not None and now - last < cooldown):
                return "Trigger is cooling down or another alert is still sending."
            self._last_fired[guild_id, key] = now
            self._busy.add(guild_id)
        try:
            return await self.play_trigger(channel, user, data)
        except discord.HTTPException:
            log.warning("Could not deliver trigger in guild %s", guild_id)
            return "The alert could not be sent. Check channel permissions."
        finally:
            self._busy.discard(guild_id)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if (
            self._closed
            or message.author.bot
            or not message.guild
            or not message.content
        ):
            return
        permissions = message.channel.permissions_for(message.guild.me)
        can_send = (
            permissions.send_messages_in_threads
            if isinstance(message.channel, discord.Thread)
            else permissions.send_messages
        )
        if not can_send:
            return
        settings = await self.get_settings(message.guild.id)
        content = message.content.lower()
        # Preserve first-match ordering without the old long-message cutoff.
        for key, data in settings["triggers"].items():
            if data["active"] and key and key in content:
                await self.fire(message.channel, message.author, key)
                return

    @commands.hybrid_group(
        name="chattrigger",
        aliases=["alert"],
        invoke_without_command=True,
        fallback="settings",
    )
    @commands.guild_only()
    async def chattrigger(self, ctx: commands.Context) -> None:
        """Open trigger settings and the creation wizard."""
        if not await self.is_admin_or_manager(ctx):
            return await ctx.send(
                "You need Manage Server or trigger manager permission.", ephemeral=True
            )
        from .ui import TriggerPanel

        view = TriggerPanel(self, ctx.author.id, ctx.guild.id)
        await view.build()
        view.message = await ctx.send(
            view=view, ephemeral=True, allowed_mentions=discord.AllowedMentions.none()
        )

    async def _permission_command(self, ctx, user, field: str, enabled: bool) -> None:
        if not await self.is_admin_or_manager(ctx):
            return await ctx.send(
                "You need Manage Server or trigger manager permission.", ephemeral=True
            )
        await self.change_permission(ctx.guild.id, field, user.id, enabled)
        await ctx.send(
            f"{'Granted' if enabled else 'Removed'} {'trigger manager' if field == 'admin_users' else 'trigger firing'} permission for {user.id}.",
            ephemeral=True,
        )

    @chattrigger.command(name="add_perm")
    async def add_perm(self, ctx: commands.Context, user: discord.User) -> None:
        """Allow a user to fire alerts without editing them."""
        await self._permission_command(ctx, user, "allowed_users", True)

    @chattrigger.command(name="remove_perm")
    async def remove_perm(self, ctx: commands.Context, user: discord.User) -> None:
        """Revoke an explicitly granted firing permission."""
        await self._permission_command(ctx, user, "allowed_users", False)

    @chattrigger.command(name="add_manager")
    async def add_manager(self, ctx: commands.Context, user: discord.User) -> None:
        """Allow a user to manage and fire triggers."""
        await self._permission_command(ctx, user, "admin_users", True)

    @chattrigger.command(name="remove_manager")
    async def remove_manager(self, ctx: commands.Context, user: discord.User) -> None:
        """Revoke an explicitly granted manager permission."""
        await self._permission_command(ctx, user, "admin_users", False)

    @chattrigger.command(name="list")
    async def ct_list(self, ctx: commands.Context) -> None:
        """Browse all triggers with pagination."""
        from .ui import TriggerPanel

        view = TriggerPanel(self, ctx.author.id, ctx.guild.id, read_only=True)
        await view.build()
        view.message = await ctx.send(
            view=view, allowed_mentions=discord.AllowedMentions.none()
        )

    @chattrigger.command(name="test")
    async def test_trigger(self, ctx: commands.Context, *, phrase: str) -> None:
        """Test a trigger, including its configured sound behavior."""
        if not await self.is_admin_or_manager(ctx):
            return await ctx.send(
                "You need trigger management permission.", ephemeral=True
            )
        await ctx.defer(ephemeral=True)
        result = await self.fire(
            ctx.channel, ctx.author, phrase.strip().lower(), testing=True
        )
        await ctx.send(result, ephemeral=True)

    @chattrigger.command(name="cooldown")
    async def cooldown(
        self, ctx: commands.Context, seconds: int, *, phrase: str
    ) -> None:
        """Set a trigger's server-wide cooldown in seconds."""
        await self._option_command(ctx, phrase, cooldown=seconds)

    @chattrigger.command(name="audio")
    async def audio(
        self, ctx: commands.Context, mode: AudioMode, *, phrase: str
    ) -> None:
        """Choose interrupt music or skip sound while music is busy."""
        await self._option_command(ctx, phrase, audio_mode=mode)

    async def _option_command(self, ctx, phrase: str, **changes) -> None:
        if not await self.is_admin_or_manager(ctx):
            return await ctx.send(
                "You need trigger management permission.", ephemeral=True
            )
        try:
            await self.update_trigger(ctx.guild.id, phrase.strip().lower(), **changes)
        except ValueError as error:
            return await ctx.send(str(error), ephemeral=True)
        await ctx.send("Trigger updated.", ephemeral=True)

    @commands.Cog.listener()
    async def on_guild_remove(self, guild: discord.Guild) -> None:
        self._settings.pop(guild.id, None)
        self._last_fired = {
            key: value for key, value in self._last_fired.items() if key[0] != guild.id
        }

    async def red_delete_data_for_user(self, *, requester: str, user_id: int) -> None:
        """Remove explicit permissions and invalidate the runtime permission cache."""
        for guild_id in await self.config.all_guilds():
            async with self._locks[guild_id]:
                settings = await self.get_settings(guild_id)
                for field in ("allowed_users", "admin_users"):
                    settings[field] = [uid for uid in settings[field] if uid != user_id]
                await self._persist(guild_id, settings)
