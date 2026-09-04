"""Conservative external-metadata to Tidal-catalog matching."""
from __future__ import annotations

import re
from typing import Any, Iterable

from rapidfuzz import fuzz

from .identity import normalize_identity_text

_BRACKETED = re.compile(r"\[[^\]]*\]|\([^)]*(?:official|video|audio|lyrics|visualizer|remaster|live|hd|4k)[^)]*\)", re.IGNORECASE)
_RECORDING_VARIANTS = (
    "cover",
    "remix",
    "live",
    "karaoke",
    "instrumental",
    "nightcore",
    "sped up",
    "slowed",
    "reverb",
)


def _normalize(value: str) -> str:
    return normalize_identity_text(_BRACKETED.sub(" ", value))


def _title(track: Any) -> str:
    return str(getattr(track, "full_name", None) or getattr(track, "name", "") or "")


def _artist(track: Any) -> str:
    return str(getattr(getattr(track, "artist", None), "name", "") or "")


def _contains_phrase(haystack: str, needle: str) -> bool:
    return bool(needle) and f" {needle} " in f" {haystack} "


def select_confident_youtube_tidal_track(
    video_title: str,
    channel: str,
    tracks: Iterable[Any],
) -> Any | None:
    """Return a Tidal candidate only when title and artist identity are explicit."""
    normalized_video_title = _normalize(video_title)
    normalized_channel = _normalize(channel)
    raw_video_title = normalize_identity_text(video_title)
    if not normalized_video_title:
        return None

    for track in tracks:
        title = _title(track)
        artist = _artist(track)
        normalized_title = _normalize(title)
        normalized_artist = _normalize(artist)
        if not normalized_title or not normalized_artist:
            continue
        raw_title = normalize_identity_text(title)
        if any(
            _contains_phrase(raw_video_title, variant)
            and not _contains_phrase(raw_title, variant)
            for variant in _RECORDING_VARIANTS
        ):
            continue
        if not _contains_phrase(normalized_video_title, normalized_title):
            continue
        if _contains_phrase(normalized_channel, normalized_artist) or _contains_phrase(
            normalized_video_title, normalized_artist
        ):
            return track
    return None


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
