"""Runtime provider and infrastructure boundaries for TidalPlayer."""

from .audio import RedAudioGateway
from .circuit_breaker import CircuitBreaker, CircuitState
from .config_repository import ConfigRepository
from .errors import ProviderFailure
from .spotify_adapter import SpotifyAdapter
from .tidal_client import TidalClient
from .tokens import TokenRepository, TokenService, TokenSnapshot
from .youtube_adapter import YouTubeAdapter

__all__ = (
    "CircuitBreaker",
    "CircuitState",
    "ConfigRepository",
    "ProviderFailure",
    "RedAudioGateway",
    "SpotifyAdapter",
    "TidalClient",
    "TokenRepository",
    "TokenService",
    "TokenSnapshot",
    "YouTubeAdapter",
)
