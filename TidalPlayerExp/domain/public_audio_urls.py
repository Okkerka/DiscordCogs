"""Canonical public SoundCloud and Bandcamp references, without provider I/O."""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlsplit

_SOUNDCLOUD_HOSTS = frozenset({"soundcloud.com", "www.soundcloud.com", "m.soundcloud.com"})
_BANDCAMP_HOST = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.bandcamp\.com\Z")
_SLUG = re.compile(r"[A-Za-z0-9_-]+\Z")
_PROFILE_PAGES = frozenset({"albums", "sets", "likes", "reposts", "tracks", "popular-tracks", "followers", "following"})
_RESERVED_ARTISTS = frozenset({"discover", "search", "charts", "you", "stream", "upload", "settings", "signin", "terms", "pages"})
_TRACKING_KEYS = frozenset({"from", "si", "ref", "ref_id", "fbclid", "gclid", "in"})
_SOUNDCLOUD_SECRET = re.compile(r"s-[A-Za-z0-9]{1,128}\Z")


def valid_soundcloud_secret(value: object) -> bool:
    """Validate a supplied share token, not a login or arbitrary query string."""
    return isinstance(value, str) and _SOUNDCLOUD_SECRET.fullmatch(value) is not None


def parse_public_audio_url(value: str) -> tuple[str, str, str, str | None]:
    """Separate an explicit SoundCloud track share token from its public identity.

    Tokens are supported only in /artist/track/s-token links. Collection tokens,
    credentials, arbitrary query parameters and redirect hosts remain rejected.
    """
    if not isinstance(value, str) or len(value) > 2048 or any(
        character.isspace() or ord(character) < 32 for character in value
    ):
        raise ValueError("Unsupported public audio URL")
    try:
        parts = urlsplit(value)
        segments = parts.path.removesuffix("/").split("/")
        if parts.hostname in _SOUNDCLOUD_HOSTS and len(segments) == 4 and segments[2] != "sets":
            token = segments[-1]
            if not valid_soundcloud_secret(token):
                raise ValueError("Invalid SoundCloud share link")
            base = parts._replace(path="/".join(segments[:-1])).geturl()
            provider, content_type, canonical = canonical_public_audio_url(base)
            if content_type != "track":
                raise ValueError("Invalid SoundCloud share link")
            return provider, content_type, canonical, token
    except ValueError:
        raise ValueError("Unsupported public audio URL") from None
    return (*canonical_public_audio_url(value), None)


def canonical_public_audio_url(value: str) -> tuple[str, str, str]:
    """Return provider, content type, and canonical HTTPS URL for public audio.

    Only known tracking parameters are discarded. Private links, path escapes,
    credentials, custom domains, and unrecognized query parameters are rejected.
    """
    if not isinstance(value, str) or not value or len(value) > 2048:
        raise ValueError("Unsupported public audio URL")
    if any(character.isspace() or ord(character) < 32 for character in value) or "\\" in value:
        raise ValueError("Unsupported public audio URL")
    try:
        parts = urlsplit(value)
        if (
            parts.scheme != "https"
            or not parts.hostname
            or parts.username is not None
            or parts.password is not None
            or parts.port not in (None, 443)
            or parts.fragment
        ):
            raise ValueError
        query = parse_qsl(parts.query, keep_blank_values=True, max_num_fields=32)
        if any(not (key.casefold().startswith("utm_") or key.casefold() in _TRACKING_KEYS) for key, _ in query):
            raise ValueError
        host = parts.hostname.lower()
        path = parts.path.removesuffix("/")
        segments = path.split("/")[1:]
        if not path.startswith("/") or any(_SLUG.fullmatch(segment) is None for segment in segments):
            raise ValueError
        if host in _SOUNDCLOUD_HOSTS:
            if not segments or segments[0].casefold() in _RESERVED_ARTISTS:
                raise ValueError
            if len(segments) == 2 and segments[1].casefold() not in _PROFILE_PAGES:
                content_type = "track"
            elif len(segments) == 3 and segments[1] == "sets":
                content_type = "playlist"
            else:
                raise ValueError
            return "soundcloud", content_type, f"https://soundcloud.com{path}"
        if (_BANDCAMP_HOST.fullmatch(host)
                and host.split(".", 1)[0] not in {"www", "daily", "fan", "help", "auth"}
                and len(segments) == 2 and segments[0] in {"track", "album"}):
            return "bandcamp", segments[0], f"https://{host}{path}"
        raise ValueError
    except (TypeError, ValueError):
        raise ValueError("Unsupported public audio URL") from None
