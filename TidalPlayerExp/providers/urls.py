"""Strict, non-fallback URL recognition for external music providers."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum
from urllib.parse import parse_qs, urlsplit

from ..domain.public_audio_urls import parse_public_audio_url


class ProviderKind(StrEnum):
    TIDAL = "tidal"
    SPOTIFY = "spotify"
    YOUTUBE = "youtube"
    SOUNDCLOUD = "soundcloud"
    BANDCAMP = "bandcamp"


@dataclass(frozen=True)
class ProviderURL:
    provider: ProviderKind
    content_type: str
    identifier: str
    secret_token: str | None = field(default=None, repr=False)


class MalformedProviderURL(ValueError):
    pass


_TIDAL_TYPES = {"track", "video", "album", "playlist", "mix"}
_SPOTIFY_TYPES = {"track", "album", "playlist"}
_YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"}
_YOUTUBE_EMBED_HOSTS = {"youtube-nocookie.com", "www.youtube-nocookie.com"}
_YOUTUBE_VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}\Z")
_YOUTUBE_PLAYLIST_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_TIDAL_COLLECTION_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")


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
        elif normalized.strip("<>").lstrip().startswith(("https://", "http://")):
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
    if any(character.isspace() or ord(character) < 32 for character in value) or "\\" in value:
        raise MalformedProviderURL("Malformed provider URL")
    if (
        parts.scheme != "https" or not parts.hostname
        or parts.username is not None or parts.password is not None
    ):
        raise MalformedProviderURL("Provider URLs must be HTTPS without credentials")
    try:
        if parts.port not in (None, 443):
            raise ValueError("Unsupported provider port")
    except ValueError as error:
        raise MalformedProviderURL("Malformed provider URL") from error
    host = parts.hostname.lower()
    if host in {"soundcloud.com", "www.soundcloud.com", "m.soundcloud.com"} or host.endswith(".bandcamp.com"):
        try:
            provider, content_type, canonical, secret_token = parse_public_audio_url(value)
        except ValueError:
            raise MalformedProviderURL("Unsupported public audio URL") from None
        return ProviderURL(ProviderKind(provider), content_type, canonical, secret_token)
    # Accept one ordinary trailing slash, never collapse empty path segments.
    path = parts.path.removesuffix("/").split("/")[1:]
    if host in {"tidal.com", "www.tidal.com", "listen.tidal.com"}:
        if path and path[0] == "browse":
            path = path[1:]
        if len(path) == 3 and path[-1] == "u":
            path = path[:-1]
        if len(path) != 2 or path[0] not in _TIDAL_TYPES or not path[1]:
            raise MalformedProviderURL("Unsupported Tidal URL")
        if path[0] in {"track", "album", "video"}:
            if not (path[1].isascii() and path[1].isdecimal() and any(c != "0" for c in path[1])):
                raise MalformedProviderURL("Tidal media identifiers must be positive ASCII numbers")
        elif _TIDAL_COLLECTION_ID.fullmatch(path[1]) is None:
            raise MalformedProviderURL("Unsupported Tidal collection identifier")
        return ProviderURL(ProviderKind.TIDAL, path[0], path[1])
    if host == "open.spotify.com":
        if len(path) != 2 or path[0] not in _SPOTIFY_TYPES or not path[1].isalnum():
            raise MalformedProviderURL("Unsupported Spotify URL")
        return ProviderURL(ProviderKind.SPOTIFY, path[0], path[1])
    if host in _YOUTUBE_HOSTS | _YOUTUBE_EMBED_HOSTS or host == "youtu.be":
        if "//" in parts.path:
            raise MalformedProviderURL("Unsupported YouTube URL")
        try:
            query = parse_qs(parts.query, keep_blank_values=True, max_num_fields=100)
        except ValueError as error:
            raise MalformedProviderURL("Malformed provider URL") from error

        def _single(name: str) -> str | None:
            values = query.get(name)
            if values is None:
                return None
            if len(values) != 1 or not values[0]:
                raise MalformedProviderURL("Unsupported YouTube URL")
            return values[0]

        video_id = _single("v")
        playlist_id = _single("list")
        if playlist_id is not None and _YOUTUBE_PLAYLIST_ID.fullmatch(playlist_id) is None:
            raise MalformedProviderURL("Unsupported YouTube URL")
        if host in _YOUTUBE_EMBED_HOSTS:
            if len(path) != 2 or path[0] != "embed" or video_id is not None:
                raise MalformedProviderURL("Unsupported YouTube embed URL")
            return _youtube_video(path[1])
        if host == "youtu.be":
            if len(path) != 1 or video_id is not None:
                raise MalformedProviderURL("Unsupported YouTube URL")
            return _youtube_video(path[0])
        if path == ["watch"]:
            # A list attached to a video is metadata, not routing.
            return _youtube_video(video_id)
        if path == ["playlist"]:
            if video_id is not None or playlist_id is None:
                raise MalformedProviderURL("Unsupported YouTube URL")
            return ProviderURL(ProviderKind.YOUTUBE, "playlist", playlist_id)
        if len(path) == 2 and path[0] in {"shorts", "live", "embed"}:
            if video_id is not None:
                raise MalformedProviderURL("Unsupported YouTube URL")
            return _youtube_video(path[1])
        raise MalformedProviderURL("Unsupported YouTube URL")
    if "." in host:
        raise MalformedProviderURL("Unsupported provider URL")
    return None
