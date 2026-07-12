"""Strict, non-fallback URL recognition for external music providers."""

from __future__ import annotations

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


def parse_provider_url(value: str) -> ProviderURL | None:
    """Parse only exact supported HTTPS URLs; a provider lookalike raises."""
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
        return ProviderURL(ProviderKind.TIDAL, path[0], path[1])
    if host == "open.spotify.com":
        if len(path) != 2 or path[0] not in _SPOTIFY_TYPES or not path[1].isalnum():
            raise MalformedProviderURL("Unsupported Spotify URL")
        return ProviderURL(ProviderKind.SPOTIFY, path[0], path[1])
    if host in {"www.youtube.com", "youtube.com"}:
        playlist_id = parse_qs(parts.query).get("list", [None])[0]
        if path not in (["playlist"], ["watch"]) or not playlist_id:
            raise MalformedProviderURL("Unsupported YouTube URL")
        return ProviderURL(ProviderKind.YOUTUBE, "playlist", playlist_id)
    if "." in host:
        raise MalformedProviderURL("Unsupported provider URL")
    return None
