"""Conservative external-metadata to Tidal-catalog matching."""
from __future__ import annotations

import re
from typing import Any, Iterable

from rapidfuzz import fuzz

from .normalization import normalize_identity_text

_BRACKETED = re.compile(r"\[[^\]]*\]|\([^)]*(?:official|video|audio|lyrics|visualizer|remaster|live|hd|4k)[^)]*\)", re.IGNORECASE)
def _normalize(value: str) -> str:
    return normalize_identity_text(_BRACKETED.sub(" ", value))


def _title(track: Any) -> str:
    return str(getattr(track, "full_name", None) or getattr(track, "name", "") or "")


def _artist(track: Any) -> str:
    return str(getattr(getattr(track, "artist", None), "name", "") or "")


def select_best_tidal_track(query: str, tracks: Iterable[Any], *, minimum_score: float = 88.0) -> Any | None:
    normalized_query = _normalize(query)
    if not normalized_query:
        return None
    best_track: Any | None = None
    best_score = 0.0
    for track in tracks:
        title_score = fuzz.token_set_ratio(normalized_query, _normalize(_title(track)))
        combined_score = fuzz.token_set_ratio(normalized_query, _normalize(f"{_title(track)} {_artist(track)}"))
        score = (title_score * 0.55) + (combined_score * 0.45)
        if score > best_score:
            best_track, best_score = track, score
    return best_track if best_track is not None and best_score >= minimum_score else None
