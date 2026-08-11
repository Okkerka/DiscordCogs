"""Unicode-aware text identity helpers."""

import unicodedata
from typing import Any


def normalize_identity_text(value: Any) -> str:
    """Normalize user-visible text for cache keys and recording identity."""
    normalized = unicodedata.normalize("NFKC", str(value or "")).casefold()
    alphanumeric = "".join(char if char.isalnum() else " " for char in normalized)
    return " ".join(alphanumeric.split())


def recording_signature(title: Any, artist: Any) -> str:
    """Return a Unicode-aware song identity across catalog IDs."""
    normalized_title = normalize_identity_text(title)
    normalized_artist = normalize_identity_text(artist)
    return f"{normalized_artist}\x00{normalized_title}" if normalized_title else ""
