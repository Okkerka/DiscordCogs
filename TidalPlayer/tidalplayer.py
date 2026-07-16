"""
TidalPlayer - Tidal music integration for Red Discord Bot
Features: Hi-Res Audio, Album Art, Spotify/YT Importing, MixV2, Video URLs,
          Hybrid Slash Commands, Similar Albums, UserPlaylist Mgmt, Rich UI
"""
from __future__ import annotations

import asyncio
import logging
from urllib.parse import urlencode
from urllib.request import urlopen
from collections import OrderedDict, defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from itertools import islice
from typing import Any, Callable, Deque, Dict, List, Optional, Set, Tuple

import discord
from redbot.core import Config, app_commands, commands
from redbot.core.bot import Red
from redbot.core.utils.menus import SimpleMenu

from .config_schema import COG_IDENTIFIER, GLOBAL_DEFAULTS, GUILD_DEFAULTS, SCHEMA_VERSION
from .domain.models import PageResult as _PageResult
from .domain.models import TrackMeta
from .domain.matching import select_best_tidal_track
from .domain.normalization import (
    FILTER_REGEX, ISRC_PATTERN, SPOTIFY_ALBUM_PATTERN, SPOTIFY_ALBUM_RE as _SPOTIFY_ALBUM_RE,
    SPOTIFY_PLAYLIST_PATTERN, SPOTIFY_PLAYLIST_RE as _SPOTIFY_PLAYLIST_RE,
    SPOTIFY_TRACK_PATTERN, SPOTIFY_TRACK_RE as _SPOTIFY_TRACK_RE, TIDAL_URL_PATTERNS,
    TIDAL_URL_RE as _TIDAL_URL_RE, YOUTUBE_PLAYLIST_PATTERN,
    YOUTUBE_PLAYLIST_RE as _YOUTUBE_PLAYLIST_RE, YOUTUBE_SKIP_TITLES, ensure_aware as _ensure_aware,
    format_duration, make_tidal_url, truncate, utc_now as _utc_now,
)
from .ui.embeds import (
    COLOR_BLUE, COLOR_GREEN, COLOR_PURPLE, COLOR_RED, COLOR_TEAL, Messages,
    error_embed as _error_embed, make_now_playing_embed, success_embed as _success_embed,
)
from .ui.controller import PlayerControllerView
from .providers.audio import RedAudioGateway
from .providers.errors import PlaybackUnavailable
from .providers.tokens import TokenRepository, TokenService, TokenSnapshot
from .providers.urls import MalformedProviderURL, ProviderKind, parse_provider_url

try:
    import lavalink
    LAVALINK_AVAILABLE = True
except ImportError:
    lavalink = None
    LAVALINK_AVAILABLE = False

try:
    import tidalapi
    try:
        from tidalapi.media import Track as TidalTrack
        TIDAL_MODELS_AVAILABLE = True
    except ImportError:
        TidalTrack = None
        TIDAL_MODELS_AVAILABLE = False
    TIDALAPI_AVAILABLE = True
except ImportError:
    TidalTrack = None
    TIDALAPI_AVAILABLE = False
    TIDAL_MODELS_AVAILABLE = False

try:
    from googleapiclient.discovery import build
    YOUTUBE_API_AVAILABLE = True
except ImportError:
    YOUTUBE_API_AVAILABLE = False

try:
    import spotipy
    from spotipy.oauth2 import SpotifyClientCredentials
    SPOTIFY_AVAILABLE = True
except ImportError:
    SPOTIFY_AVAILABLE = False

log = logging.getLogger("red.tidalplayer")

API_SEMAPHORE_LIMIT = 5
INTERACTIVE_TIMEOUT = 30
BATCH_UPDATE_INTERVAL = 10
LOGIN_CACHE_TTL = 300.0
PROGRESS_EDIT_RATELIMIT = 1.5
LOGIN_CHECK_TIMEOUT = 10.0
LOGIN_CHECK_RETRIES = 2
PAGINATION_LIMIT = 100
MAX_ITEMS = 1000
RATELIMIT_BACKOFF_BASE = 2.0
RATELIMIT_BACKOFF_MAX = 30.0
RATELIMIT_MAX_RETRIES = 4
VC_RECONNECT_RETRIES = 2
VC_RECONNECT_DELAY = 3.0
QUEUE_PAGE_SIZE = 10
TPL_LIST_PAGE_SIZE = 15
SEARCH_BATCH_SIZE = 8
CONTROLLER_REFRESH_COOLDOWN = 3.0   # seconds between background-only controller edits
PROGRESS_SLEEP_INTERVAL = 0.0       # seconds to sleep between batch chunks (0 = no sleep)
