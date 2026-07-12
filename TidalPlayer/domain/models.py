"""Type-only immutable-at-boundary metadata models."""

from typing import Any, NamedTuple, Optional, TypedDict


class TrackMeta(TypedDict):
    """Normalized track metadata used by the playback and UI layers."""

    title: str
    artist: str
    album: Optional[str]
    duration: int
    quality: str
    image: Optional[str]
    share_url: Optional[str]
    audio_resolution: Optional[str]
    track_id: Optional[int]


class PageResult(NamedTuple):
    """A provider page plus whether sparse paging was accepted."""

    items: list[Any]
    sparse_supported: Optional[bool]
