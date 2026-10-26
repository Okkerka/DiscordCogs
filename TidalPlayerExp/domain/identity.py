"""Unicode-aware text identity helpers."""

import unicodedata
from typing import Any


def normalize_identity_text(value: Any) -> str:
    """Normalize user-visible text for cache keys and recording identity."""
    normalized = unicodedata.normalize("NFKC", str(value or "")).casefold()
    # Casefolding can introduce combining marks (e.g. "İ" becomes "i" plus
    # U+0307). Treating those as separators would split the word ("i stanbul"),
    # so drop them instead; every other non-alphanumeric character separates.
    alphanumeric = "".join(
        char if char.isalnum() else ("" if unicodedata.combining(char) else " ")
        for char in normalized
    )
    return " ".join(alphanumeric.split())


def recording_signature(title: Any, artist: Any) -> str:
    """Return a Unicode-aware song identity across catalog IDs."""
    normalized_title = normalize_identity_text(title)
    normalized_artist = normalize_identity_text(artist)
    return f"{normalized_artist}\x00{normalized_title}" if normalized_title else ""
