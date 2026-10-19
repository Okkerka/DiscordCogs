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

YOUTUBE_SKIP_TITLES: Final = frozenset(
    {"[deleted video]", "deleted video", "private video", "[private video]"}
)

ISRC_PATTERN: Final = re.compile(r"^isrc:([A-Z]{2}[A-Z0-9]{3}\d{7})$", re.IGNORECASE)


def clamp_text(value: str, limit: int) -> str:
    """Clamp text conservatively in UTF-16 units, including the ellipsis.

    Discord counts string length in UTF-16 code units, so a limit measured in
    code points would let emoji-heavy text exceed the limit it was given.
    """
    if limit <= 0:
        return ""
    encoded = value.encode("utf-16-le", errors="replace")
    if len(encoded) <= limit * 2:
        return value
    return encoded[: (limit - 1) * 2].decode("utf-16-le", errors="ignore").rstrip("\\") + "…"


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
