"""Stable Discord embed factories for TidalPlayer."""

from typing import Final

import discord

from ..domain.models import TrackMeta
from ..domain.normalization import QUALITY_LABELS, format_duration


COLOR_BLUE = discord.Color.blue()
COLOR_GREEN = discord.Color.green()
COLOR_RED = discord.Color.red()
COLOR_BLURPLE = discord.Color.blurple()
COLOR_TEAL = discord.Color.teal()
COLOR_PURPLE = discord.Color.purple()


class Messages:
    ERROR_NO_TIDALAPI = "tidalapi not installed. Run: `[p]pipinstall tidalapi`"
    ERROR_NOT_AUTHENTICATED = (
        "Not authenticated with Tidal. The bot owner must complete the OAuth flow "
        "(device code auth) before playback is available."
    )
    ERROR_NO_AUDIO_COG = "Audio cog not loaded. Run: `[p]load audio`"
    ERROR_NO_PLAYER = "No active player. Join a voice channel first."
    ERROR_NO_TRACKS_FOUND = "No tracks found."
    ERROR_INVALID_URL = "Invalid {platform} {content_type} URL"
    ERROR_CONTENT_UNAVAILABLE = "Content unavailable (private/region-locked)"
    ERROR_LAVALINK_FAILED = "Playback failed: Could not retrieve Tidal stream."
    ERROR_STILL_LOADING = "⏳ TidalPlayer is still initializing, please wait a moment."
    ERROR_NOT_PLAYING = "Nothing is currently playing."
    STATUS_PLAYING = "Playing from Tidal"
    PROGRESS_QUEUEING = "Queueing {name} ({count} tracks)..."
    STATUS_STOPPING = "Stopping playlist queueing..."
    SUCCESS_SPOTIFY_CONFIGURED = "Spotify configured."
    SUCCESS_YOUTUBE_CONFIGURED = "YouTube configured."
    SUCCESS_FILTER_ENABLED = "Remix/TikTok filter enabled."
    SUCCESS_FILTER_DISABLED = "Remix/TikTok filter disabled."
    SUCCESS_INTERACTIVE_ENABLED = "Interactive search enabled."
    SUCCESS_INTERACTIVE_DISABLED = "Interactive search disabled."
    SUCCESS_TOKENS_CLEARED = "Tokens cleared."
    SUCCESS_PARTIAL_QUEUE = "Queued {queued}/{total} ({skipped} skipped)"
    ERROR_TIMEOUT = "Selection timed out."
    ERROR_FETCH_FAILED = "Could not fetch playlist."
    ERROR_NO_SPOTIFY = (
        "Spotify not configured. Set credentials with: "
        "`[p]set api spotify client_id,<id> client_secret,<secret>`"
    )
    ERROR_NO_YOUTUBE = "YouTube not configured. Set credentials with: `[p]set api youtube api_key,<key>`"
    ERROR_NOT_USER_PLAYLIST = "That playlist is not a user-owned playlist. Use `[p]tpl list` to see your playlists."
    ERROR_PLAYLIST_WRITE_FAILED = "Playlist operation failed."
    ERROR_NO_QUEUE = "The queue is empty."


def error_embed(message: str) -> discord.Embed:
    return discord.Embed(description=message, color=COLOR_RED)


def success_embed(message: str) -> discord.Embed:
    return discord.Embed(description=message, color=COLOR_GREEN)


def make_now_playing_embed(meta: TrackMeta) -> discord.Embed:
    """Render the canonical compact now-playing embed without altering its contract."""
    description = [f"**{meta['title']}**", meta["artist"]]
    if meta.get("album"):
        description.append(f"_{meta['album']}_")
    embed = discord.Embed(
        title=Messages.STATUS_PLAYING,
        description="\n".join(description),
        color=COLOR_BLUE,
    )
    quality = meta.get("audio_resolution") or QUALITY_LABELS.get(meta["quality"], meta["quality"])
    audio_value = f"`{quality}`"
    if meta.get("share_url"):
        audio_value += f" â€¢ [Listen on TIDAL]({meta['share_url']})"
    embed.add_field(name="Audio", value=audio_value, inline=False)
    embed.set_footer(text=f"Duration: {format_duration(meta['duration'])}")
    if meta.get("image"):
        embed.set_thumbnail(url=meta["image"])
    return embed
