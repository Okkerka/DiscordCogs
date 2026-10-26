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
# Feature credits are billing metadata, not part of the recording identity. The
# text is already normalized here, so "feat." has become "feat" and the clause
# always runs to the end of the string.
_FEAT_CLAUSE_RE = re.compile(r"\b(?:feat|ft|featuring)\b.*$")
_RECORDING_VARIANT_PHRASES = (
    "sped up", "slowed down", "cover", "remix", "live", "karaoke",
    "instrumental", "nightcore", "slowed", "reverb", "remastered",
    "remaster", "acoustic",
)
_VARIANT_RE = re.compile(
    r"(?<!\w)(" + "|".join(re.escape(value) for value in _RECORDING_VARIANT_PHRASES) + r")(?!\w)"
    # Tidal encodes versions as "Remastered 2011" in ``full_name``; the year
    # residue would otherwise defeat identity comparison against providers
    # that omit the suffix.
    r"(?:\s+(?:19|20)\d{2})?"
)
_VARIANT_ALIASES = {
    "remaster": "remaster",
    "remastered": "remaster",
    "slowed": "slowed",
    "slowed down": "slowed",
    "sped up": "sped up",
}
# Words that only join two artist names never decide a match.
_ARTIST_JOINERS = frozenset({"feat", "ft", "featuring", "and", "with"})

# Remaster markers describe a mastering generation, not a different recording.
# External providers usually omit them while Tidal keeps them in the version
# suffix, so a one-sided remaster marker must not reject the catalog copy.
_VARIANT_NOISE = frozenset({"remaster"})


def _normalize(value: str) -> str:
    return normalize_identity_text(value)


def _title(track: Any) -> str:
    return str(getattr(track, "full_name", None) or getattr(track, "name", "") or "")


def _artist(track: Any) -> str:
    return str(getattr(getattr(track, "artist", None), "name", "") or "")


def _variants_from_normalized(normalized: str) -> frozenset[str]:
    return frozenset(
        _VARIANT_ALIASES.get(match.group(1), match.group(1))
        for match in _VARIANT_RE.finditer(normalized)
    )


def _recording_variants(value: str) -> frozenset[str]:
    return _variants_from_normalized(_normalize(value))


def _variants_match(left: frozenset[str], right: frozenset[str]) -> bool:
    """Allow a one-sided remaster marker; require every other variant to agree."""
    return left == right or (left ^ right) <= _VARIANT_NOISE


def _identity_from_normalized(normalized: str) -> str:
    """Strip version, feature, and official markers from normalized text."""
    normalized = _VARIANT_RE.sub(" ", normalized)
    normalized = _OFFICIAL_MUSIC_VIDEO_RE.sub(" ", normalized)
    normalized = _FEAT_CLAUSE_RE.sub(" ", normalized)
    return " ".join(word for word in normalized.split() if word not in _OFFICIAL_MARKERS)


def _identity_title(value: str) -> str:
    return _identity_from_normalized(_normalize(value))


def _title_forms_from_candidates(identity: str, candidates: set[str]) -> set[str]:
    """Allow an explicit artist prefix/suffix without dropping song words.

    Identities may have had official-marker words stripped while the artist
    name still carries them (a band named "Audio Bullys"), and separators vary
    between "Artist Title" and "Artist - Title"; try every spelling.
    """
    forms = {identity}
    for candidate in candidates:
        if not candidate:
            continue
        for separator in (" ", " - "):
            prefix, suffix = f"{candidate}{separator}", f"{separator}{candidate}"
            if identity.startswith(prefix):
                forms.add(identity[len(prefix):])
            if identity.endswith(suffix):
                forms.add(identity[:-len(suffix)])
    return forms


def _tokens(value: str) -> frozenset[str]:
    return frozenset(value.split())


def _artist_tokens_from_normalized(normalized: str) -> set[str]:
    return {token for token in normalized.split() if token not in _ARTIST_JOINERS}


def _artist_tokens(value: str) -> set[str]:
    """Return an artist credit's names without the words that join them."""
    return _artist_tokens_from_normalized(_normalize(value))


def _primary_artist_tokens_from_normalized(normalized: str) -> set[str]:
    return _artist_tokens_from_normalized(_FEAT_CLAUSE_RE.sub(" ", normalized))


def _artist_is_explicit_from_tokens(
    artist_tokens: set[str],
    video_tokens: frozenset[str],
    channel_tokens: frozenset[str],
) -> bool:
    # A credit like "Artist A feat. Artist B" names two artists while a video
    # title normally carries only the primary one; require just that part.
    return bool(artist_tokens) and (
        artist_tokens <= video_tokens
        or artist_tokens <= channel_tokens
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
    video_identity = _identity_from_normalized(normalized_video_title)
    video_variants = _variants_from_normalized(normalized_video_title)
    video_tokens = _tokens(normalized_video_title)
    normalized_channel = _normalize(channel)
    channel_tokens = _tokens(normalized_channel)
    best_track: Any | None = None
    best_score = -1.0
    best_exact = False
    for track in tracks:
        title = _normalize(_title(track))
        artist = _normalize(_artist(track))
        if not artist:
            continue
        title_identity = _identity_from_normalized(title)
        if not title_identity:
            continue
        if not _variants_match(_variants_from_normalized(title), video_variants):
            continue
        artist_candidates = {artist, _identity_from_normalized(artist)}
        if not (
            _title_forms_from_candidates(title_identity, artist_candidates)
            & _title_forms_from_candidates(video_identity, artist_candidates)
        ):
            continue
        if not _artist_is_explicit_from_tokens(
            _primary_artist_tokens_from_normalized(artist), video_tokens, channel_tokens
        ):
            continue
        # token_sort (not token_set) so a title-subset candidate cannot saturate
        # the score at 100 and outrank the full identity by tie order.
        title_score = fuzz.token_sort_ratio(video_identity, title_identity)
        artist_score = max(
            fuzz.token_set_ratio(artist, normalized_channel),
            fuzz.token_set_ratio(artist, normalized_video_title),
        )
        score = title_score * 0.65 + artist_score * 0.35
        exact = title_identity == video_identity
        if score > best_score or (score == best_score and exact and not best_exact):
            best_track, best_score, best_exact = track, score, exact
    return best_track


def select_best_tidal_track(query: str | NormalizedCandidate, tracks: Iterable[Any], *, minimum_score: float = 88.0) -> Any | None:
    """Match external recordings without letting a title subset hide a wrong artist."""
    structured = query if isinstance(query, NormalizedCandidate) else None
    normalized_query = _normalize(structured.query if structured else query)
    if not normalized_query:
        return None
    # The candidate side of every comparison is the same for each track.
    structured_variants = _recording_variants(structured.title) if structured else frozenset()
    structured_identity = _identity_title(structured.title) if structured else ""
    structured_artist_tokens: set[str] = set()
    if structured is not None:
        for value in structured.artists:
            structured_artist_tokens |= _artist_tokens(value)
    query_tokens = _tokens(normalized_query)
    best_track: Any | None = None
    best_score = 0.0
    best_exact = False
    for track in tracks:
        # Normalize each field exactly once: the derived identity/variant/token
        # forms below all operate on this normalized text.
        title = _normalize(_title(track))
        if not title:
            continue
        artist = _normalize(_artist(track))
        if structured:
            if not artist or structured_artist_tokens != _artist_tokens_from_normalized(artist):
                continue
            if not _variants_match(structured_variants, _variants_from_normalized(title)):
                continue
            identity = _identity_from_normalized(title)
            if structured_identity != identity:
                continue
            duration = getattr(track, "duration", None)
            if (structured.duration and isinstance(duration, (int, float)) and duration > 0
                    and abs(duration - structured.duration) > max(10, structured.duration * .05)):
                continue
            # The gates above already pinned the recording; score the canonical
            # title so a Tidal version suffix ("(Remastered 2011)") cannot dilute
            # the match below the threshold.
            score_title = identity
        else:
            if normalized_query != title:
                # Featured artists are billing metadata; matching on the primary
                # credit keeps "Artist A feat. Artist B" reachable from a query
                # that only names Artist A.
                if not (artist and _primary_artist_tokens_from_normalized(artist) <= query_tokens):
                    continue
            score_title = title
        title_score = fuzz.token_set_ratio(normalized_query, score_title)
        combined_score = fuzz.token_set_ratio(normalized_query, f"{score_title} {artist}")
        score = (title_score * 0.55) + (combined_score * 0.45)
        # Subset candidates saturate both metrics at 100; prefer a title that
        # equals the query exactly before falling back to provider list order.
        exact = normalized_query in {score_title, title}
        if score > best_score or (score == best_score and exact and not best_exact):
            best_track, best_score, best_exact = track, score, exact
    return best_track if best_track is not None and best_score >= minimum_score else None
