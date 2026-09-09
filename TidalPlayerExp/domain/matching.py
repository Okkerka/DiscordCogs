"""Conservative external-metadata to Tidal-catalog matching."""
from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

from rapidfuzz import fuzz

from .identity import normalize_identity_text
from .candidates import NormalizedCandidate

_OFFICIAL_MARKERS = frozenset({"official", "audio", "video", "lyrics", "visualizer", "hd", "4k"})
_OFFICIAL_MUSIC_VIDEO_RE = re.compile(r"\bofficial music video\b")
_RECORDING_VARIANT_PHRASES = (
    "sped up", "slowed down", "cover", "remix", "live", "karaoke",
    "instrumental", "nightcore", "slowed", "reverb", "remastered",
    "remaster", "acoustic",
)
_VARIANT_RE = re.compile(
    r"(?<!\w)(?:" + "|".join(re.escape(value) for value in _RECORDING_VARIANT_PHRASES) + r")(?!\w)"
)
_VARIANT_ALIASES = {
    "remaster": "remaster",
    "remastered": "remaster",
    "slowed": "slowed",
    "slowed down": "slowed",
    "sped up": "sped up",
}


def _normalize(value: str) -> str:
    return normalize_identity_text(value)


def _title(track: Any) -> str:
    return str(getattr(track, "full_name", None) or getattr(track, "name", "") or "")


def _artist(track: Any) -> str:
    return str(getattr(getattr(track, "artist", None), "name", "") or "")


def _recording_variants(value: str) -> frozenset[str]:
    return frozenset(_VARIANT_ALIASES.get(match.group(0), match.group(0)) for match in _VARIANT_RE.finditer(_normalize(value)))


def _identity_title(value: str) -> str:
    normalized = _VARIANT_RE.sub(" ", _normalize(value))
    normalized = _OFFICIAL_MUSIC_VIDEO_RE.sub(" ", normalized)
    return " ".join(word for word in normalized.split() if word not in _OFFICIAL_MARKERS)


def _title_forms(identity: str, artist: str) -> set[str]:
    """Allow an explicit artist prefix/suffix without dropping song words."""
    forms = {identity}
    prefix, suffix = f"{artist} ", f" {artist}"
    if identity.startswith(prefix):
        forms.add(identity[len(prefix):])
    if identity.endswith(suffix):
        forms.add(identity[:-len(suffix)])
    return forms


def _tokens(value: str) -> frozenset[str]:
    return frozenset(value.split())


def _artist_is_explicit(artist: str, video_title: str, channel: str) -> bool:
    artist_tokens = _tokens(_normalize(artist))
    return bool(artist_tokens) and (
        artist_tokens <= _tokens(_normalize(video_title))
        or artist_tokens <= _tokens(_normalize(channel))
    )


def select_confident_youtube_tidal_track(
    video_title: str,
    channel: str,
    tracks: Iterable[Any],
) -> Any | None:
    """Return a Tidal candidate only when title and artist identity are explicit."""
    normalized_video_title = _normalize(video_title)
    if not normalized_video_title:
        return None
    video_identity = _identity_title(video_title)
    video_variants = _recording_variants(video_title)
    normalized_channel = _normalize(channel)
    best_track: Any | None = None
    best_score = -1.0
    for track in tracks:
        title = _title(track)
        artist = _artist(track)
        normalized_artist = _normalize(artist)
        title_identity = _identity_title(title)
        if not title_identity or not normalized_artist:
            continue
        if _recording_variants(title) != video_variants:
            continue
        if not (_title_forms(title_identity, normalized_artist) & _title_forms(video_identity, normalized_artist)):
            continue
        if not _artist_is_explicit(artist, video_title, channel):
            continue
        title_score = fuzz.token_set_ratio(video_identity, title_identity)
        artist_score = max(
            fuzz.token_set_ratio(normalized_artist, normalized_channel),
            fuzz.token_set_ratio(normalized_artist, normalized_video_title),
        )
        score = title_score * 0.65 + artist_score * 0.35
        if score > best_score:
            best_track, best_score = track, score
    return best_track


def select_best_tidal_track(query: str | NormalizedCandidate, tracks: Iterable[Any], *, minimum_score: float = 88.0) -> Any | None:
    """Match external recordings without letting a title subset hide a wrong artist."""
    structured = query if isinstance(query, NormalizedCandidate) else None
    normalized_query = _normalize(structured.query if structured else query)
    if not normalized_query:
        return None
    best_track: Any | None = None
    best_score = 0.0
    for track in tracks:
        title, artist = _normalize(_title(track)), _normalize(_artist(track))
        if not title:
            continue
        if structured:
            if not artist or not any(_normalize(value) == artist for value in structured.artists):
                continue
            if _recording_variants(structured.title) != _recording_variants(_title(track)):
                continue
            if _identity_title(structured.title) != _identity_title(_title(track)):
                continue
            duration = getattr(track, "duration", None)
            if (structured.duration and isinstance(duration, (int, float)) and duration > 0
                    and abs(duration - structured.duration) > max(10, structured.duration * .05)):
                continue
        elif normalized_query != title and not _artist_is_explicit(artist, normalized_query, ""):
            continue
        title_score = fuzz.token_set_ratio(normalized_query, _normalize(_title(track)))
        combined_score = fuzz.token_set_ratio(normalized_query, _normalize(f"{_title(track)} {_artist(track)}"))
        score = (title_score * 0.55) + (combined_score * 0.45)
        if score > best_score:
            best_track, best_score = track, score
    return best_track if best_track is not None and best_score >= minimum_score else None
