"""Stable Discord embed factories for TidalPlayerExp."""

import discord

from ..domain.models import TrackMeta
from ..domain.normalization import QUALITY_LABELS, format_duration
from .display import clamp_text, escape_display, safe_display_url

COLOR_BLUE = discord.Color.blue()
COLOR_GREEN = discord.Color.green()
COLOR_RED = discord.Color.red()
COLOR_TEAL = discord.Color.teal()
COLOR_PURPLE = discord.Color.purple()


class Messages:
    ERROR_NO_TIDALAPI = "tidalapi not installed. Run: `[p]pipinstall tidalapi`"
    ERROR_NOT_AUTHENTICATED = (
        "Not authenticated with Tidal. The bot owner must complete the OAuth flow "
        "(device code auth) before playback is available."
    )
    ERROR_NO_TRACKS_FOUND = "No tracks found."
    ERROR_INVALID_URL = "Invalid {platform} {content_type} URL"
    ERROR_CONTENT_UNAVAILABLE = "Content unavailable (private/region-locked)"
    ERROR_YOUTUBE_FAILED = "Playback failed: Could not retrieve YouTube audio."
    ERROR_STILL_LOADING = "⏳ TidalPlayerExp is still initializing, please wait a moment."
    ERROR_NOT_PLAYING = "Nothing is currently playing."
    PROGRESS_QUEUEING = "Queueing {name} ({count} tracks)..."
    SUCCESS_FILTER_ENABLED = "Remix/TikTok filter enabled."
    SUCCESS_FILTER_DISABLED = "Remix/TikTok filter disabled."
    SUCCESS_INTERACTIVE_ENABLED = "Interactive search enabled."
    SUCCESS_INTERACTIVE_DISABLED = "Interactive search disabled."
    SUCCESS_TOKENS_CLEARED = "Tokens cleared."
    SUCCESS_PARTIAL_QUEUE = "Queued {queued}/{total} ({skipped} skipped)"
    ERROR_TIMEOUT = "Selection timed out."
    ERROR_FETCH_FAILED = "Could not fetch playlist."
    ERROR_NO_SPOTIFY = (
        "Spotify not configured. Use `[p]setup spotify` to set app credentials."
    )
    ERROR_NOT_USER_PLAYLIST = "That playlist is not a user-owned playlist. Use `[p]tplaylist list` to see your playlists."
    ERROR_PLAYLIST_WRITE_FAILED = "Playlist operation failed."
    ERROR_BATCH_IN_PROGRESS = (
        "A playlist import is already running. Use `[p]remove all` before starting another."
    )


def display_source(meta: TrackMeta) -> str:
    source = str(meta.get("source") or "Tidal")
    return {"soundcloud": "SoundCloud", "bandcamp": "Bandcamp"}.get(source.casefold(), source)


def source_link_label(meta: TrackMeta) -> str:
    source = display_source(meta)
    return "TIDAL" if source.casefold() == "tidal" else source


def source_quality_field(meta: TrackMeta) -> tuple[str, str]:
    """Describe catalog availability without claiming measured stream quality."""
    source = display_source(meta)
    if source.casefold() != "tidal":
        return "Source", f"{source} audio"
    quality = str(meta.get("quality") or "Unknown")
    return "Catalog quality", str(meta.get("audio_resolution") or QUALITY_LABELS.get(quality, quality))


def display_duration(meta: TrackMeta) -> str:
    """Do not describe missing or live-stream duration as a zero-length song."""
    duration = meta.get("duration")
    if isinstance(duration, bool) or not isinstance(duration, int) or duration <= 0:
        return "Unknown"
    return format_duration(duration)


def error_embed(message: str) -> discord.Embed:
    return discord.Embed(description=clamp_text(message, 4096), color=COLOR_RED)


def success_embed(message: str) -> discord.Embed:
    return discord.Embed(description=clamp_text(message, 4096), color=COLOR_GREEN)


def make_queue_embed(meta: TrackMeta, *, title: str = "Song added to the queue") -> discord.Embed:
    """Compact embed shown when a track is added to the queue."""
    track_title = escape_display(meta.get("title") or "Unknown track", 512)
    artist = escape_display(meta.get("artist") or "Unknown artist")
    album = escape_display(meta.get("album") or "")
    duration = display_duration(meta)
    share_url = safe_display_url(meta.get("share_url"))

    lines = [f"**{track_title}**", artist]
    if album:
        lines.append(f"_{album}_")

    embed = discord.Embed(
        title=clamp_text(title, 256),
        description="\n".join(lines),
        color=COLOR_PURPLE,
    )
    embed.set_footer(text=f"Duration: {duration}")
    if share_url:
        embed.add_field(
            name=f"Open in {escape_display(source_link_label(meta), 64)}",
            value=f"[Listen]({share_url})",
            inline=True,
        )
    if image := safe_display_url(meta.get("image"), 2048):
        embed.set_thumbnail(url=image)
    return embed
