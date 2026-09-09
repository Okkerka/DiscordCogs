"""Bound untrusted provider text before composing Discord Markdown."""
from __future__ import annotations

import re
from urllib.parse import quote, urlsplit

_MARKDOWN = re.compile(r"([\\`*_{}\[\]|~#>])")


def clamp_text(value: str, limit: int) -> str:
    """Clamp text conservatively in UTF-16 units, including the ellipsis."""
    if limit <= 0:
        return ""
    encoded = value.encode("utf-16-le", errors="replace")
    if len(encoded) <= limit * 2:
        return value
    return encoded[: (limit - 1) * 2].decode("utf-16-le", errors="ignore").rstrip("\\") + "…"


def escape_display(value: object, limit: int = 256) -> str:
    """Render one metadata value as bounded text, without mentions or Markdown."""
    text = " ".join(str(value or "").split())
    text = _MARKDOWN.sub(r"\\\1", text)
    text = text.replace("@", "@\u200b").replace("<", "<\u200b")
    return clamp_text(text, limit)


def safe_display_url(value: object, limit: int = 768) -> str | None:
    """Return an HTTP(S) display URL safe inside Markdown, or omit it.

    This does not authorize fetching the URL; provider network validation remains
    separate. Long links are omitted rather than truncated into a different URL.
    """
    if not isinstance(value, str) or not value or len(value) > limit:
        return None
    if any(character.isspace() or ord(character) < 32 for character in value) or "\\" in value:
        return None
    try:
        parts = urlsplit(value)
        if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username is not None or parts.password is not None:
            return None
        parts.port  # Reject malformed ports before passing a URL to Discord.
        escaped = quote(value, safe=":/?#@!$&'*+,;=%")
    except (ValueError, UnicodeError):
        return None
    return escaped if len(escaped) <= limit else None
