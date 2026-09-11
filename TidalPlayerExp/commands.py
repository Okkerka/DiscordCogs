"""Shared prefix/slash playback controls; sessions own all queue mutations."""
from __future__ import annotations

from typing import Literal

import discord
from redbot.core import commands

from .domain.normalization import format_duration
from .playback.interfaces import PlaybackSession
from .playback.requests import current_request, playback_request
from .ui.display import escape_display


def parse_position(value: str) -> int | None:
    """Parse a bounded absolute timestamp without accepting signs or fractions."""
    if len(value) > 12:
        return None
    parts = value.strip().split(":")
    if not 1 <= len(parts) <= 3 or any(not item.isascii() or not item.isdecimal() for item in parts):
        return None
    values = [int(item) for item in parts]
    if any(item >= 60 for item in values[1:]):
        return None
    total = 0
    for item in values:
        total = total * 60 + item
    return total


def playback_cooldown(ctx: commands.Context) -> commands.Cooldown:
    """Per-user cooldown: 3.5s for normal searches/tracks, 10s for playlist/album imports."""
    query = ""
    if ctx.kwargs and "query" in ctx.kwargs:
        query = str(ctx.kwargs["query"] or "").lower()
    elif ctx.args and len(ctx.args) > 2:
        query = " ".join(str(a) for a in ctx.args[2:]).lower()
    elif getattr(ctx, "message", None) and ctx.message.content:
        query = ctx.message.content.lower()
    elif getattr(ctx, "interaction", None) and getattr(ctx.interaction, "data", None):
        options = ctx.interaction.data.get("options", [])
        query = " ".join(str(opt.get("value", "")) for opt in options).lower()

    if any(k in query for k in ("playlist", "album", "mix", "/sets/")):
        return commands.Cooldown(1, 10.0)
    return commands.Cooldown(1, 3.5)


class PlaybackCommands:
    """Command mixin using the cog's native backend and existing controller lifecycle."""

    async def _defer(self, ctx: commands.Context) -> None:
        """Defer only interactions so prefix commands don't trigger unnecessary typing."""
        if getattr(ctx, "interaction", None) is not None:
            response = getattr(ctx.interaction, "response", None)
            if response is not None and response.is_done():
                return
            if hasattr(ctx, "defer") and callable(ctx.defer):
                await ctx.defer()
            else:
                interaction = ctx.interaction
                response = getattr(interaction, "response", None)
                if response is not None and not response.is_done():
                    await interaction.response.defer()

    async def cog_before_invoke(self, ctx: commands.Context) -> None:
        if getattr(ctx, "interaction", None) is not None:
            await self._defer(ctx)

    async def _reply(self, ctx: commands.Context, text: str) -> None:
        await ctx.send(text, allowed_mentions=discord.AllowedMentions.none())

    async def _control_session(self, ctx: commands.Context) -> PlaybackSession | None:
        await self._defer(ctx)
        if ctx.guild is None or self._closing:
            await self._reply(ctx, "Playback is unavailable here.")
            return None
        session = await self.backend.get(ctx.guild.id)
        channel = getattr(getattr(ctx.author, "voice", None), "channel", None)
        if session is None:
            await self._reply(ctx, "There is no active player.")
            return None
        if channel is None or session.snapshot().channel_id != channel.id:
            await self._reply(ctx, "Join the bot's voice channel to control playback.")
            return None
        return session

    def _cancel_imports(self, guild_id: int) -> None:
        self._stop_generations[guild_id] += 1
        self._guild_generations[guild_id] += 1
        event = self._cancel_events.get(guild_id)
        if event is not None:
            event.set()

    async def _stop_playback(self, guild_id: int) -> None:
        """One Stop implementation for command, slash command, and player button."""
        self._cancel_imports(guild_id)
        self._cancel_guild_background_tasks(guild_id)
        session = await self.backend.get(guild_id)
        if session is not None:
            if hasattr(self, "attachment_resolver"):
                for entry in session.snapshot().queued:
                    self.attachment_resolver.discard(entry.primary)
                current = session.snapshot().current
                if current is not None:
                    self.attachment_resolver.discard(current.primary)
            await session.stop(clear_queue=True)
        self._current_entries.pop(guild_id, None)
        self._current_meta.pop(guild_id, None)
        self._controller_meta.pop(guild_id, None)
        message = self._controller_messages.pop(guild_id, None)
        self._stop_controller_view(guild_id)
        if message is not None:
            try:
                await message.delete()
            except discord.HTTPException:
                pass

    def _can_cancel_request(self, ctx: commands.Context, session: PlaybackSession | None) -> bool:
        """Check voice authority even while Discord's handshake is unpublished."""
        if ctx.guild is None or self._closing:
            return False
        channel = getattr(getattr(ctx.author, "voice", None), "channel", None)
        connecting = getattr(getattr(ctx.guild, "voice_client", None), "channel", None)
        target_id = session.snapshot().channel_id if session is not None else getattr(connecting, "id", None)
        return channel is not None and (target_id is None or target_id == channel.id)

    @commands.hybrid_command(name="stop")
    @commands.guild_only()
    async def stop_command(self, ctx: commands.Context) -> None:
        """Stop playback, clear waiting songs, and cancel pending imports/lookups."""
        await self._defer(ctx)
        session = await self.backend.get(ctx.guild.id)
        if not self._can_cancel_request(ctx, session):
            await self._reply(ctx, "Join the bot's voice channel to stop playback.")
            return
        await self._stop_playback(ctx.guild.id)
        await self._reply(ctx, "⏹ Playback stopped. Queue cleared and imports cancelled.")

    @commands.hybrid_group(name="remove", fallback="track", invoke_without_command=True)
    @commands.guild_only()
    async def remove_command(self, ctx: commands.Context, index: int) -> None:
        """Remove a waiting track by position. 1 is the next song, not the current one."""
        session = await self._control_session(ctx)
        if session is None:
            return
        removed = await session.remove(index)
        if removed is None:
            await self._reply(ctx, "Invalid queue position. Use queue; 1 is the next song.")
            return
        if hasattr(self, "attachment_resolver"):
            self.attachment_resolver.discard(removed.primary)
        await self._reply(ctx, f"Removed #{index}: {escape_display(removed.meta['title'])}.")
        await self._refresh_controller(ctx.guild.id, force=True)

    @remove_command.command(name="all")
    async def remove_all(self, ctx: commands.Context) -> None:
        """Clear waiting tracks and imports without stopping the current song."""
        await self._clear_waiting(ctx)

    async def _clear_waiting(self, ctx: commands.Context) -> None:
        """Shared validated implementation; never invoke another command callback."""
        await self._defer(ctx)
        if ctx.guild is None:
            await self._reply(ctx, "Playback is unavailable here.")
            return
        session = await self.backend.get(ctx.guild.id)
        if not self._can_cancel_request(ctx, session):
            await self._reply(ctx, "Join the bot's voice channel to clear the queue.")
            return
        self._cancel_imports(ctx.guild.id)
        if session is not None and hasattr(self, "attachment_resolver"):
            for track in session.snapshot().queued:
                self.attachment_resolver.discard(track.primary)
        count = await session.clear_queue() if session is not None else 0
        await self._reply(ctx, f"Cleared {count} waiting track(s). Current playback continues.")
        await self._refresh_controller(ctx.guild.id, force=True)

    @commands.hybrid_command(name="clear")
    @commands.guild_only()
    async def clear_command(self, ctx: commands.Context) -> None:
        """Clear waiting tracks and imports without stopping the current song."""
        await self._clear_waiting(ctx)

    @commands.hybrid_command(name="volume")
    @commands.guild_only()
    async def volume_command(self, ctx: commands.Context, percent: int | None = None) -> None:
        """Show or set server volume, from 0 (muted) to 150 percent."""
        session = await self._control_session(ctx)
        if session is None:
            return
        if percent is None:
            await self._reply(ctx, f"Volume: {session.snapshot().volume}%.")
            return
        if not 0 <= percent <= 150:
            await self._reply(ctx, "Volume must be between 0 and 150.")
            return
        async with self._guild_locks[ctx.guild.id]:
            if self._closing or await self.backend.get(ctx.guild.id) is not session:
                await self._reply(ctx, "The voice session changed. Try volume again.")
                return
            await session.set_volume(percent)
            await self.config.guild(ctx.guild).volume.set(percent)
        await self._reply(ctx, f"Volume: {percent}%." + (" Amplification is peak-limited." if percent > 100 else ""))

    @commands.hybrid_command(name="pause")
    @commands.guild_only()
    async def pause_command(self, ctx: commands.Context) -> None:
        """Pause current audio."""
        await self._pause_command(ctx, True)

    @commands.hybrid_command(name="resume")
    @commands.guild_only()
    async def resume_command(self, ctx: commands.Context) -> None:
        """Resume paused audio."""
        await self._pause_command(ctx, False)

    async def _pause_command(self, ctx: commands.Context, paused: bool) -> None:
        session = await self._control_session(ctx)
        if session is not None:
            changed = await session.set_paused(paused)
            await self._reply(ctx, ("Paused." if paused else "Resumed.") if changed else "Nothing is playing.")
            if changed:
                await self._refresh_controller(ctx.guild.id, force=True)

    @commands.hybrid_command(name="skip")
    @commands.guild_only()
    async def skip_command(self, ctx: commands.Context) -> None:
        """Skip the current song and continue with the next waiting track."""
        session = await self._control_session(ctx)
        if session is not None:
            skipped = await session.skip()
            await self._reply(ctx, "Skipped." if skipped else "Nothing is playing.")

    @commands.hybrid_command(name="move")
    @commands.guild_only()
    async def move_command(self, ctx: commands.Context, index: int, destination: int) -> None:
        """Move a queued song between one-based positions; e.g. move 5 1."""
        session = await self._control_session(ctx)
        if session is not None:
            moved = await session.move(index, destination)
            await self._reply(ctx, f"Moved #{index} to #{destination}." if moved else "Invalid queue positions. Use queue to see current numbers.")
            if moved:
                await self._refresh_controller(ctx.guild.id, force=True)

    @commands.hybrid_command(name="shuffle")
    @commands.guild_only()
    async def shuffle_command(self, ctx: commands.Context) -> None:
        """Shuffle waiting tracks without interrupting the current song."""
        session = await self._control_session(ctx)
        if session is not None:
            shuffled = await session.shuffle_queue()
            await self._reply(ctx, "Waiting tracks shuffled." if shuffled else "Queue at least two waiting tracks first.")
            if shuffled:
                await self._refresh_controller(ctx.guild.id, force=True)

    @commands.hybrid_command(name="repeat")
    @commands.guild_only()
    async def repeat_command(self, ctx: commands.Context, mode: Literal["off", "track", "queue"]) -> None:
        """Repeat the current track, the queue, or turn repeat off."""
        session = await self._control_session(ctx)
        if session is not None:
            await session.set_repeat(mode)
            await self._reply(ctx, f"Repeat: {mode}. Skipped and failed tracks do not repeat.")

    @commands.hybrid_command(name="seek")
    @commands.guild_only()
    async def seek_command(self, ctx: commands.Context, position: str) -> None:
        """Seek to seconds, MM:SS, or HH:MM:SS in a finite track."""
        session = await self._control_session(ctx)
        if session is None:
            return
        seconds = parse_position(position)
        if seconds is None or not await session.seek(seconds):
            await self._reply(ctx, "Use a position inside the current track, such as 90 or 1:30. Live/unknown-length sources cannot be sought.")
            return
        await self._reply(ctx, f"Seeking to {format_duration(seconds)}.")

    @commands.hybrid_command(name="replay")
    @commands.guild_only()
    async def replay_command(self, ctx: commands.Context) -> None:
        """Restart the current finite track from the beginning."""
        session = await self._control_session(ctx)
        if session is not None:
            restarted = await session.seek(0)
            await self._reply(ctx, "Restarting the current track." if restarted else "No seekable track is playing.")

    @commands.hybrid_command(name="retry")
    @commands.guild_only()
    async def retry_command(self, ctx: commands.Context) -> None:
        """Resume waiting tracks after repeated playback failures halted the queue."""
        session = await self._control_session(ctx)
        if session is not None:
            resumed = await session.resume_queue()
            await self._reply(ctx, "Retrying the waiting queue." if resumed else "There is no stopped queue to retry.")

    @commands.hybrid_command(name="autoplay")
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    async def autoplay_command(self, ctx: commands.Context, mode: Literal["on", "off"] | None = None) -> None:
        """Show or set automatic recommendations (Manage Server permission required)."""
        session = await self._control_session(ctx)
        if session is None:
            return
        setting = self.config.guild(ctx.guild).autoplay_enabled
        if mode is not None:
            await setting.set(mode == "on")
            if mode == "off":
                task = self._autoplay_tasks.pop(ctx.guild.id, None)
                if task is not None:
                    task.cancel()
        enabled = await setting()
        await self._reply(ctx, f"Autoplay: {'on' if enabled else 'off'}.")
        await self._refresh_controller(ctx.guild.id, force=True)

    @commands.hybrid_command(name="playnext")
    @commands.guild_only()
    @playback_request()
    @commands.dynamic_cooldown(playback_cooldown, commands.BucketType.user)
    async def playnext_command(self, ctx: commands.Context, *, query: str) -> None:
        """Queue one song/search result next without interrupting playback."""
        request = current_request(self, ctx)
        if request is not None:
            request.next_up = True
        await self._play_request(ctx, query=query)

    @commands.hybrid_command(name="musichelp")
    async def musichelp_command(self, ctx: commands.Context) -> None:
        """Explain native music commands, queue positions, and file playback."""
        await self._defer(ctx)
        await self._reply(ctx,
            "**Native music commands**\n"
            "`play <link/search>` — TIDAL, YouTube, SoundCloud, Bandcamp, or Spotify imports.\n"
            "`/play` optionally selects `platform`: tidal, youtube, or soundcloud; unset keeps TIDAL search.\n"
            "`playfile` or `/playfile file:<upload>` — Play audio/video uploads (including MP4), up to 50 MiB.\n"
            "`queue` · `now` · `tidalsearch <query>`\n"
            "`pause` · `resume` · `skip` · `stop` (stops audio, clears queue, cancels imports)\n"
            "`remove 1` removes the next waiting song; any valid queue number works. "
            "`remove all` or `clear` clears waiting songs/imports but keeps current audio. Slash: `/remove track index:1`, `/remove all`, or `/clear`.\n"
            "`playnext <link/search>` · `move 5 1` · `shuffle` · `repeat off/track/queue`\n"
            "`volume 0–150` · `seek 1:30` · `replay` · `retry` · `autoplay on/off`\n"
            "`tplaylist` manages TIDAL playlists; `setup` handles provider logins and diagnostics (bot owner). "
            "`tfilter` and `tinteractive` require Manage Server.\n"
            "Playback controls require the bot's voice channel. Voice disconnects automatically after two idle minutes. "
            "Volume changes may briefly rebuffer. Files are streamed, not permanently downloaded; expired uploads need re-uploading.")
