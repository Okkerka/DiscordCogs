class ProviderFailure(RuntimeError):
    """Base class for sanitized provider and playback boundary failures."""


class PlaybackUnavailable(ProviderFailure):
    """Raised when Red Audio cannot provide or connect a player."""
