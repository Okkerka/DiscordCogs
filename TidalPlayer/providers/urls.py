"""Strict, non-fallback URL recognition for external music providers."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import parse_qs, urlsplit


class ProviderKind(StrEnum):
    TIDAL = "tidal"
    SPOTIFY = "spotify"
    YOUTUBE = "youtube"


@dataclass(frozen=True)
class ProviderURL:
    provider: ProviderKind
    content_type: str
    identifier: str


class MalformedProviderURL(ValueError):
    pass


_TIDAL_TYPES = {"track", "video", "album", "playlist", "mix"}
_SPOTIFY_TYPES = {"track", "album", "playlist"}
_YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"}
_YOUTUBE_VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}")
_YOUTUBE_PLAYLIST_ID = re.compile(r"[A-Za-z0-9_-]+")


def _normalize_url(value: str) -> str:
    normalized = value.strip()
    if normalized.startswith("<") or normalized.endswith(">"):
        if (
            normalized.startswith("<")
            and normalized.endswith(">")
            and normalized.count("<") == 1
            and normalized.count(">") == 1
        ):
            normalized = normalized[1:-1].strip()
        else:
            raise MalformedProviderURL("Malformed provider URL")
    return normalized


def _youtube_video(identifier: str | None) -> ProviderURL:
    if identifier is None or _YOUTUBE_VIDEO_ID.fullmatch(identifier) is None:
        raise MalformedProviderURL("Unsupported YouTube URL")
    return ProviderURL(ProviderKind.YOUTUBE, "video", identifier)


def parse_provider_url(value: str) -> ProviderURL | None:
    """Parse only exact supported HTTPS URLs; a provider lookalike raises."""
    value = _normalize_url(value)
    try:
        parts = urlsplit(value)
    except ValueError as error:
        raise MalformedProviderURL("Malformed provider URL") from error
    if not parts.scheme and not parts.netloc:
        return None
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
        raise MalformedProviderURL("Provider URLs must be HTTPS without credentials")
    host = parts.hostname.lower()
    path = [segment for segment in parts.path.split("/") if segment]
    if host in {"tidal.com", "www.tidal.com", "listen.tidal.com"}:
        if len(path) == 3 and path[0] == "browse":
            path = path[1:]
        if len(path) != 2 or path[0] not in _TIDAL_TYPES or not path[1]:
            raise MalformedProviderURL("Unsupported Tidal URL")
        if path[0] in {"track", "album", "video"} and not path[1].isdigit():
            raise MalformedProviderURL("Tidal media identifiers must be numeric")
        return ProviderURL(ProviderKind.TIDAL, path[0], path[1])
    if host == "open.spotify.com":
        if len(path) != 2 or path[0] not in _SPOTIFY_TYPES or not path[1].isalnum():
            raise MalformedProviderURL("Unsupported Spotify URL")
        return ProviderURL(ProviderKind.SPOTIFY, path[0], path[1])
    if host in _YOUTUBE_HOSTS or host == "youtu.be":
        query = parse_qs(parts.query)
        playlist_id = query.get("list", [None])[0]
        if playlist_id is not None:
            if _YOUTUBE_PLAYLIST_ID.fullmatch(playlist_id) is None:
                raise MalformedProviderURL("Unsupported YouTube URL")
            if path == ["playlist"] or path == ["watch"] or (
                host == "youtu.be" and len(path) == 1
            ):
                return ProviderURL(ProviderKind.YOUTUBE, "playlist", playlist_id)
        if host == "youtu.be":
            if len(path) != 1:
                raise MalformedProviderURL("Unsupported YouTube URL")
            return _youtube_video(path[0])
        if path == ["watch"]:
            return _youtube_video(query.get("v", [None])[0])
        if len(path) == 2 and path[0] in {"shorts", "live", "embed"}:
            return _youtube_video(path[1])
        raise MalformedProviderURL("Unsupported YouTube URL")
    if "." in host:
        raise MalformedProviderURL("Unsupported provider URL")
    return None
