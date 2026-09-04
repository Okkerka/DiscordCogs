"""Small public surface for backend-neutral playback contracts."""

from .errors import (
    PlaybackError,
    PlaybackStartError,
    PlaybackUnavailable,
    SourceResolutionError,
)
from .interfaces import (
    PlaybackBackend,
    PlaybackEventSink,
    PlaybackSession,
    SourceResolver,
)
from .models import (
    PlaybackEntry,
    PlaybackSnapshot,
    ResolvedSource,
    SourceKind,
    SourceReference,
)

__all__ = (
    "PlaybackBackend",
    "PlaybackEntry",
    "PlaybackError",
    "PlaybackEventSink",
    "PlaybackSession",
    "PlaybackSnapshot",
    "PlaybackStartError",
    "PlaybackUnavailable",
    "ResolvedSource",
    "SourceKind",
    "SourceReference",
    "SourceResolutionError",
    "SourceResolver",
)
