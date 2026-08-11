"""Pure parsing and formatting helpers with no Discord or provider dependency."""

import re
from datetime import datetime, timezone
from typing import Any, Final

QUALITY_LABELS: Final = {
    "HI_RES_LOSSLESS": "HI-RES LOSSLESS (FLAC)",
    "LOSSLESS": "LOSSLESS (FLAC)",
    "HIGH": "HIGH (320kbps)",
    "LOW": "LOW (96kbps)",
}

FILTER_KEYWORDS: Final = frozenset(
    {
        "sped up", "slowed", "tiktok", "reverb", "8d audio", "bass boosted",
        "reverbed", "slowed down", "nightcore", "daycore",
    }
)
FILTER_REGEX: Final = re.compile("|".join(re.escape(kw) for kw in FILTER_KEYWORDS), re.IGNORECASE)

YOUTUBE_SKIP_TITLES: Final = frozenset({"[deleted video]", "private video", "[private video]"})

TIDAL_URL_PATTERNS: Final = {
    "track": re.compile(r"tidal\.com/(?:browse/)?track/(\d+)"),
    "video": re.compile(r"tidal\.com/(?:browse/)?video/(\d+)"),
    "album": re.compile(r"tidal\.com/(?:browse/)?album/(\d+)"),
    "playlist": re.compile(r"tidal\.com/(?:browse/)?playlist/([a-f0-9-]+)"),
    "mix": re.compile(r"tidal\.com/(?:browse/)?mix/([a-f0-9A-Z_-]+)"),
}
SPOTIFY_PLAYLIST_PATTERN: Final = re.compile(r"open\.spotify\.com/playlist/([a-zA-Z0-9]+)")
SPOTIFY_TRACK_PATTERN: Final = re.compile(r"open\.spotify\.com/track/([a-zA-Z0-9]+)")
SPOTIFY_ALBUM_PATTERN: Final = re.compile(r"open\.spotify\.com/album/([a-zA-Z0-9]+)")
YOUTUBE_PLAYLIST_PATTERN: Final = re.compile(r"youtube\.com/.*[?&]list=([a-zA-Z0-9_-]+)")
ISRC_PATTERN: Final = re.compile(r"^isrc:([A-Z]{2}[A-Z0-9]{3}\d{7})$", re.IGNORECASE)

TIDAL_URL_RE: Final = re.compile(r"tidal\.com/")
SPOTIFY_PLAYLIST_RE: Final = re.compile(r"open\.spotify\.com/playlist/")
SPOTIFY_ALBUM_RE: Final = re.compile(r"open\.spotify\.com/album/")
SPOTIFY_TRACK_RE: Final = re.compile(r"open\.spotify\.com/track/")
YOUTUBE_PLAYLIST_RE: Final = re.compile(r"youtube\.com/.*[?&]list=")


def truncate(text: str, limit: int) -> str:
    """Shorten text to an exact display limit, preserving existing behavior."""
    if len(text) > limit:
        return text[:limit - 3] + "..."
    return text


def make_tidal_url(content_type: str, content_id: Any) -> str:
    return f"https://listen.tidal.com/{content_type}/{content_id}"


def utc_now() -> datetime:
    return datetime.now(tz=timezone.utc)


def ensure_aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def format_duration(seconds: int) -> str:
    minutes, remainder = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{remainder:02d}"
    return f"{minutes:02d}:{remainder:02d}"
