"""Immutable, backend-neutral values used by the experimental player."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import cast
from urllib.parse import urlsplit

from ..domain.models import TrackMeta
from ..domain.public_audio_urls import canonical_public_audio_url


class SourceKind(StrEnum):
    """Stable source kinds understood by the playback boundary."""

    TIDAL = "tidal"
    TIDAL_VIDEO = "tidal_video"
    YOUTUBE = "youtube"
    SOUNDCLOUD = "soundcloud"
    BANDCAMP = "bandcamp"


_YOUTUBE_IDENTIFIER = re.compile(r"[A-Za-z0-9_-]{11}\Z")
_META_REQUIRED_KEYS = frozenset(
    {
        "title",
        "artist",
        "album",
        "duration",
        "quality",
        "image",
        "share_url",
        "audio_resolution",
        "track_id",
    }
)
_META_ALLOWED_KEYS = _META_REQUIRED_KEYS | {"source"}


def _safe_mapping_copy(value: object) -> dict[object, object] | None:
    """Copy a real mapping without retaining an exception from its iteration."""

    if not isinstance(value, Mapping):
        return None
    try:
        copied = dict(value)
    except Exception:  # noqa: BLE001 - hostile mappings may raise arbitrary exceptions
        copied = None
    return copied


def _copy_track_meta(value: object) -> dict[str, object] | None:
    """Validate scalar TrackMeta fields and return a detached mutable copy."""

    copied = _safe_mapping_copy(value)
    if copied is None:
        return None
    try:
        if any(not isinstance(key, str) for key in copied):
            return None
        if not _META_REQUIRED_KEYS.issubset(copied) or not set(copied).issubset(_META_ALLOWED_KEYS):
            return None
        if not isinstance(copied["title"], str) or not isinstance(copied["artist"], str):
            return None
        if copied["album"] is not None and not isinstance(copied["album"], str):
            return None
        if isinstance(copied["duration"], bool) or not isinstance(copied["duration"], int):
            return None
        if not isinstance(copied["quality"], str):
            return None
        for key in ("image", "share_url", "audio_resolution"):
            if copied[key] is not None and not isinstance(copied[key], str):
                return None
        if copied["track_id"] is not None and (
            isinstance(copied["track_id"], bool) or not isinstance(copied["track_id"], int)
        ):
            return None
        if "source" in copied and not isinstance(copied["source"], str):
            return None
    except Exception:  # noqa: BLE001 - malformed metadata mappings are untrusted
        return None
    return cast(dict[str, object], copied)


@dataclass(frozen=True, slots=True)
class SourceReference:
    """A validated provider kind and its stable identifier."""

    kind: SourceKind
    identifier: str

    def __post_init__(self) -> None:
        if not isinstance(self.kind, SourceKind) or not isinstance(self.identifier, str):
            raise ValueError("Source reference is invalid")  # noqa: TRY004 - stable invariant error

        if self.kind in (SourceKind.TIDAL, SourceKind.TIDAL_VIDEO):
            valid = (
                self.identifier.isascii()
                and self.identifier.isdecimal()
                and any(character != "0" for character in self.identifier)
            )
        elif self.kind in (SourceKind.SOUNDCLOUD, SourceKind.BANDCAMP):
            try:
                provider, content_type, canonical = canonical_public_audio_url(self.identifier)
                valid = provider == self.kind.value and content_type == "track" and canonical == self.identifier
            except ValueError:
                valid = False
        else:
            valid = _YOUTUBE_IDENTIFIER.fullmatch(self.identifier) is not None
        if not valid:
            raise ValueError("Source identifier is invalid")


def _validate_positive(value: int | None, field_name: str) -> None:
    if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value <= 0):
        raise ValueError(f"{field_name} must be positive")


@dataclass(frozen=True, slots=True, repr=False)
class ResolvedSource:
    """A resolved HTTPS media source whose sensitive fields stay in memory."""

    url: str
    headers: Mapping[str, str]
    codec: str | None = None
    sample_rate: int | None = None
    channels: int | None = None
    duration: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.url, str):
            raise ValueError("Resolved source URL must be HTTPS")  # noqa: TRY004 - stable invariant error
        try:
            parts = urlsplit(self.url)
            _ = parts.port
            has_credentials = parts.username is not None or parts.password is not None
            valid_url = parts.scheme.lower() == "https" and bool(parts.hostname) and not has_credentials
        except (TypeError, ValueError):
            valid_url = False
        if not valid_url:
            raise ValueError("Resolved source URL must be HTTPS")

        copied_headers = _safe_mapping_copy(self.headers)
        if copied_headers is None:
            raise ValueError("Resolved source headers must be a mapping")
        if any(not isinstance(key, str) or not isinstance(value, str) for key, value in copied_headers.items()):
            raise ValueError("Resolved source headers must contain strings")
        object.__setattr__(self, "headers", MappingProxyType(cast(dict[str, str], copied_headers)))

        _validate_positive(self.sample_rate, "Sample rate")
        _validate_positive(self.channels, "Channel count")
        _validate_positive(self.duration, "Duration")

    def __repr__(self) -> str:
        """Return a constant representation that cannot expose media details."""

        return "ResolvedSource(<redacted>)"


@dataclass(frozen=True, slots=True)
class PlaybackEntry:
    """A queued track and its optional source fallback."""

    entry_id: str
    primary: SourceReference
    fallback: SourceReference | None
    meta: TrackMeta
    requester_id: int | None
    fallback_meta: TrackMeta | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.entry_id, str) or not self.entry_id or len(self.entry_id) > 64:
            raise ValueError("Playback entry identifier is invalid")
        if self.fallback == self.primary:
            raise ValueError("Playback fallback must differ from primary")
        copied_meta = _copy_track_meta(self.meta)
        if copied_meta is None:
            raise ValueError("Playback metadata is invalid")
        # TrackMeta is a TypedDict, while MappingProxyType supplies the
        # runtime read-only boundary; this cast preserves the public shape.
        object.__setattr__(self, "meta", cast(TrackMeta, MappingProxyType(copied_meta)))
        if self.fallback_meta is not None:
            copied_fallback = _copy_track_meta(self.fallback_meta)
            if copied_fallback is None:
                raise ValueError("Playback fallback metadata is invalid")
            object.__setattr__(self, "fallback_meta", cast(TrackMeta, MappingProxyType(copied_fallback)))


@dataclass(frozen=True, slots=True)
class PlaybackSnapshot:
    """An immutable view of current playback and queued entries."""

    current: PlaybackEntry | None
    queued: tuple[PlaybackEntry, ...]
    paused: bool
    channel_id: int | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "queued", tuple(self.queued))
