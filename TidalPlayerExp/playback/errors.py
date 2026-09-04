"""Sanitized exceptions exposed by the playback boundary."""

from __future__ import annotations


class PlaybackError(Exception):
    """Base playback error with a fixed, safe public message."""

    _DEFAULT_MESSAGE = "Playback error"

    def __init__(
        self,
        message: str | None = None,
        *,
        provider: str | None = None,
        stage: str | None = None,
    ) -> None:
        # The parameters are accepted for call-site compatibility, but caller
        # input is deliberately never retained: it may contain secrets or IDs.
        del message, provider, stage
        super().__init__(self._DEFAULT_MESSAGE)


class PlaybackUnavailable(PlaybackError):
    """Raised when playback capacity or connectivity is unavailable."""

    _DEFAULT_MESSAGE = "Playback unavailable"


class SourceResolutionError(PlaybackError):
    """Raised when a source cannot be resolved."""

    _DEFAULT_MESSAGE = "Source resolution failed"


class PlaybackStartError(PlaybackError):
    """Raised when a resolved source cannot be started."""

    _DEFAULT_MESSAGE = "Playback start failed"
