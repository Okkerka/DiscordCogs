"""
TidalPlayer - Tidal music integration for Red Discord Bot
Features: Hi-Res Audio, Album Art, Spotify/YT Importing, MixV2, Video URLs,
          Hybrid Slash Commands, Similar Albums, UserPlaylist Mgmt, Rich UI
"""
from __future__ import annotations

import asyncio
import logging
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
SEARCH_BATCH_SIZE = 5


_CACHE_CAPS: Dict[str, int] = {
    "search": 200,
    "track": 500,
    "isrc": 500,
    "album": 100,
    "playlist": 100,
    "mix": 50,
    "video": 100,
}


def _is_tidal_track(obj: Any) -> bool:
    if TIDAL_MODELS_AVAILABLE and TidalTrack is not None:
        return isinstance(obj, TidalTrack)
    return (
        hasattr(obj, "id")
        and hasattr(obj, "duration")
        and (hasattr(obj, "get_stream") or hasattr(obj, "get_url"))
    )


def _spotify_item_to_query(item: dict) -> str:
    track = item.get("track") or {}
    isrc = (track.get("external_ids") or {}).get("isrc")
    if isrc:
        return f"isrc:{isrc}"
    artists = " ".join(a["name"] for a in track.get("artists", []))
    return f"{track.get('name', '')} {artists}".strip()


def _spotify_album_item_to_query(item: dict) -> str:
    isrc = (item.get("external_ids") or {}).get("isrc")
    if isrc:
        return f"isrc:{isrc}"
    artists = " ".join(a["name"] for a in item.get("artists", []))
    return f"{item.get('name', '')} {artists}".strip()


class TrackSelectView(discord.ui.View):
    def __init__(self, tracks: List[Any], author: discord.User, timeout: float = 30.0):
        super().__init__(timeout=timeout)
        self.tracks = tracks[:5]
        self.author = author
        self.selected: Optional[Any] = None
        self._event = asyncio.Event()
        self._timed_out = False

        for i, track in enumerate(self.tracks):
            name = getattr(track, "full_name", None) or getattr(track, "name", f"Track {i+1}")
            artist = getattr(getattr(track, "artist", None), "name", "")
            raw_label = f"{artist} \u2014 {name}" if artist else name
            btn = discord.ui.Button(
                label=truncate(raw_label, 80),
                style=discord.ButtonStyle.primary,
                custom_id=f"track_{i}",
                row=0,
            )
            btn.callback = self._make_track_callback(i)
            self.add_item(btn)

        cancel_btn = discord.ui.Button(
            label="Cancel", style=discord.ButtonStyle.danger, custom_id="cancel", row=1
        )
        cancel_btn.callback = self._cancel_callback
        self.add_item(cancel_btn)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author.id:
            await interaction.response.send_message("Not your selection.", ephemeral=True)
            return False
        return True

    def _disable_all(self) -> None:
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True

    def _make_track_callback(self, index: int):
        async def callback(interaction: discord.Interaction) -> None:
            self.selected = self.tracks[index]
            self._disable_all()
            self._event.set()
            self.stop()
            await interaction.response.defer()
        return callback

    async def _cancel_callback(self, interaction: discord.Interaction) -> None:
        self.selected = None
        self._disable_all()
        self._event.set()
        self.stop()
        await interaction.response.defer()

    async def wait_for_selection(self) -> Optional[Any]:
        try:
            await asyncio.wait_for(self._event.wait(), timeout=self.timeout + 5.0)
        except asyncio.TimeoutError:
            self._timed_out = True
        return self.selected

    async def on_timeout(self) -> None:
        self._timed_out = True
        self._event.set()


class TidalHandler:
    __slots__ = (
        "bot", "config", "session", "_refresh_task", "api_semaphore",
        "_login_cache", "_login_cache_time", "_cache", "_refresh_lock", "_executor",
    )

    def __init__(self, bot: Red, config: Config):
        self.bot = bot
        self.config = config
        self.session: Optional[Any] = tidalapi.Session() if TIDALAPI_AVAILABLE else None
        self._refresh_task: Optional[asyncio.Task] = None
        self.api_semaphore = asyncio.Semaphore(API_SEMAPHORE_LIMIT)
        self._login_cache: Optional[bool] = None
        self._login_cache_time: float = 0.0
        self._cache: Dict[str, OrderedDict] = {}
        self._refresh_lock = asyncio.Lock()
        self._executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="tidal_io")

    def _get_cached(self, category: str, key: str) -> Optional[Any]:
        bucket = self._cache.get(category)
        if bucket is None:
            return None
        entry = bucket.get(key)
        if entry is None:
            return None
        value, expiry = entry
        now = asyncio.get_running_loop().time()
        if now > expiry:
            del bucket[key]
            return None
        bucket.move_to_end(key)
        return value

    def _set_cached(self, category: str, key: str, value: Any, ttl: float) -> None:
        if category not in self._cache:
            self._cache[category] = OrderedDict()
        bucket = self._cache[category]
        cap = _CACHE_CAPS.get(category, 200)
        if key in bucket:
            bucket.move_to_end(key)
        else:
            if len(bucket) >= cap:
                bucket.popitem(last=False)
        now = asyncio.get_running_loop().time()
        bucket[key] = (value, now + ttl)

    async def _run_blocking(self, func: Callable[[], Any], timeout: float = 10.0) -> Any:
        return await asyncio.wait_for(
            asyncio.get_running_loop().run_in_executor(self._executor, func),
            timeout=timeout,
        )

    async def _run_with_backoff(self, func: Callable[[], Any], timeout: float = 10.0) -> Any:
        delay = RATELIMIT_BACKOFF_BASE
        last_exc: Optional[Exception] = None
        for attempt in range(RATELIMIT_MAX_RETRIES):
            try:
                return await self._run_blocking(func, timeout=timeout)
            except Exception as e:
                last_exc = e
                status = getattr(e, "status", None) or getattr(e, "status_code", None)
                if status is None and hasattr(e, "response") and e.response is not None:
                    status = getattr(e.response, "status_code", None)
                is_unauthorized = status == 401 or "401" in str(e).lower() or "unauthorized" in str(e).lower()
                if is_unauthorized:
                    log.warning("Encountered 401 Unauthorized from Tidal API. Attempting token refresh...")
                    refreshed = await self.refresh_tokens()
                    if refreshed:
                        log.info("Token refresh succeeded after 401, retrying...")
                        continue
                    else:
                        log.error("Token refresh failed after 401. Session is invalid.")
                        raise
                exc_type = type(e).__name__.lower()
                err_str = str(e).lower()
                is_ratelimit = (
                    status == 429
                    or "429" in err_str or "too many requests" in err_str
                    or "rate limit" in err_str or "ratelimit" in err_str
                    or "toomanyrequests" in exc_type or "ratelimit" in exc_type
                )
                if is_ratelimit and attempt < RATELIMIT_MAX_RETRIES - 1:
                    wait = min(delay, RATELIMIT_BACKOFF_MAX)
                    log.warning(f"Rate limited by Tidal, retrying in {wait:.1f}s (attempt {attempt + 1})")
                    await asyncio.sleep(wait)
                    delay *= 2
                else:
                    raise
        raise last_exc

    async def initialize(self, creds: Dict[str, Any]) -> None:
        if not self.session or not creds.get("access_token"):
            return
        try:
            expiry = datetime.fromtimestamp(creds["expiry_time"]) if creds.get("expiry_time") else None
            def _load() -> None:
                self.session.load_oauth_session(
                    creds["token_type"], creds["access_token"], creds["refresh_token"], expiry
                )
            await self._run_blocking(_load, timeout=15.0)
            self._login_cache = True
            self._login_cache_time = asyncio.get_running_loop().time()
            log.info("Tidal session loaded successfully")
        except asyncio.TimeoutError:
            log.warning("Timed out loading Tidal session from stored credentials")
        except Exception as e:
            log.warning(f"Failed to load Tidal session: {e}")

    async def refresh_tokens(self) -> bool:
        if not self.session:
            return False
        async with self._refresh_lock:
            try:
                expiry_time = await self._run_blocking(lambda: self.session.expiry_time, timeout=5.0)
                if expiry_time:
                    expiry_aware = _ensure_aware(expiry_time)
                    if _utc_now() + timedelta(hours=2) <= expiry_aware:
                        return True
            except Exception:
                pass
            log.info("Refreshing Tidal tokens...")
            try:
                if hasattr(self.session, "request") and hasattr(self.session.request, "refresh_token"):
                    await self._run_blocking(self.session.request.refresh_token, timeout=15.0)
                    log.info("Token refreshed via request.refresh_token")
                def _get_state():
                    return (
                        self.session.expiry_time, self.session.token_type,
                        self.session.access_token, self.session.refresh_token,
                    )
                expiry_time, token_type, access, refresh = await self._run_blocking(_get_state, timeout=5.0)
                await asyncio.gather(
                    self.config.token_type.set(token_type),
                    self.config.access_token.set(access),
                    self.config.refresh_token.set(refresh),
                    self.config.expiry_time.set(int(expiry_time.timestamp()) if expiry_time else None),
                )
                self._login_cache = True
                self._login_cache_time = asyncio.get_running_loop().time()
                return True
            except Exception as e:
                log.error(f"Token refresh failed: {e}")
                self._login_cache = False
                self._login_cache_time = asyncio.get_running_loop().time()
                return False

    def start_refresh_loop(self) -> None:
        if self._refresh_task:
            self._refresh_task.cancel()
        self._refresh_task = asyncio.create_task(self._auto_refresh_tokens())

    def unload(self) -> None:
        if self._refresh_task:
            self._refresh_task.cancel()
        self._executor.shutdown(wait=False)

    def invalidate_login_cache(self) -> None:
        self._login_cache = None
        self._login_cache_time = 0.0

    async def is_logged_in(self) -> bool:
        if not self.session:
            return False
        now = asyncio.get_running_loop().time()
        if self._login_cache is not None and (now - self._login_cache_time) < LOGIN_CACHE_TTL:
            return self._login_cache
        for attempt in range(LOGIN_CHECK_RETRIES):
            try:
                result = bool(await self._run_blocking(self.session.check_login, timeout=LOGIN_CHECK_TIMEOUT))
                self._login_cache = result
                self._login_cache_time = asyncio.get_running_loop().time()
                return result
            except asyncio.TimeoutError:
                log.warning(f"Timed out checking Tidal login (attempt {attempt + 1}/{LOGIN_CHECK_RETRIES})")
                if attempt < LOGIN_CHECK_RETRIES - 1:
                    await asyncio.sleep(2)
            except Exception:
                self._login_cache = False
                self._login_cache_time = asyncio.get_running_loop().time()
                return False
        return self._login_cache if self._login_cache is not None else False

    async def _auto_refresh_tokens(self) -> None:
        while True:
            sleep_secs = 3600
            try:
                if await self.is_logged_in():
                    expiry_time = await self._run_blocking(lambda: self.session.expiry_time, timeout=5.0)
                    if expiry_time:
                        expiry_aware = _ensure_aware(expiry_time)
                        until_expiry = (expiry_aware - _utc_now()).total_seconds()
                        sleep_secs = max(60, until_expiry - 7200)
            except Exception:
                pass
            await asyncio.sleep(sleep_secs)
            try:
                if not await self.is_logged_in():
                    continue
                expiry_time = await self._run_blocking(lambda: self.session.expiry_time, timeout=5.0)
                if not expiry_time:
                    continue
                expiry_aware = _ensure_aware(expiry_time)
                if _utc_now() + timedelta(hours=2) <= expiry_aware:
                    continue
                await self.refresh_tokens()
            except Exception as e:
                log.error(f"Auto token refresh failed: {e}")

    async def search(self, query: str, filter_remixes: bool = False) -> List[Any]:
        if not self.session:
            return []
        cache_key = f"{query}:{filter_remixes}"
        cached = self._get_cached("search", cache_key)
        if cached is not None:
            return cached
        async with self.api_semaphore:
            try:
                def run_search():
                    if TIDAL_MODELS_AVAILABLE and TidalTrack is not None:
                        return self.session.search(query, models=[TidalTrack])
                    return self.session.search(query)
                result = await self._run_with_backoff(run_search, timeout=10.0)
                tracks = self._extract_tracks(result)
                filtered = self._filter_tracks(tracks) if filter_remixes else tracks
                self._set_cached("search", cache_key, filtered, 600.0)
                return filtered
            except asyncio.TimeoutError:
                log.warning(f"Tidal search timeout for '{query}'")
                return []
            except Exception as e:
                log.error(f"Search failed for '{query}': {e}")
                return []

    async def get_track_by_isrc(self, isrc: str) -> Optional[Any]:
        if not self.session:
            return None
        cached = self._get_cached("isrc", isrc)
        if cached is not None:
            return cached
        async with self.api_semaphore:
            try:
                def _fetch():
                    if hasattr(self.session, "get_tracks_by_isrc"):
                        results = self.session.get_tracks_by_isrc(isrc)
                        return results[0] if results else None
                    return None
                res = await self._run_with_backoff(_fetch, timeout=10.0)
                if res:
                    self._set_cached("isrc", isrc, res, 3600.0)
                return res
            except Exception as e:
                log.debug(f"ISRC lookup failed for {isrc}: {e}")
                return None

    async def get_track(self, track_id: str) -> Optional[Any]:
        if not self.session:
            return None
        cached = self._get_cached("track", track_id)
        if cached is not None:
            return cached
        async with self.api_semaphore:
            try:
                res = await self._run_with_backoff(lambda: self.session.track(track_id), timeout=10.0)
                if res:
                    self._set_cached("track", track_id, res, 3600.0)
                return res
            except asyncio.TimeoutError:
                log.warning(f"Tidal get_track timeout for id {track_id}")
                return None
            except Exception as e:
                log.debug(f"Failed to fetch track {track_id}: {e}")
                return None

    async def get_track_radio(self, track_id: str) -> List[Any]:
        """Return Tidal's track-radio candidates for a specific catalog track."""
        if not self.session or not track_id:
            return []
        cache_key = f"radio:{track_id}"
        cached = self._get_cached("mix", cache_key)
        if cached is not None:
            return cached
        async with self.api_semaphore:
            try:
                def fetch() -> Any:
                    if hasattr(self.session, "get_track_radio"):
                        return self.session.get_track_radio(track_id)
                    if hasattr(self.session, "track_radio"):
                        return self.session.track_radio(track_id)
                    track = self.session.track(track_id)
                    radio = getattr(track, "radio", None)
                    return radio() if callable(radio) else []
                result = await self._run_with_backoff(fetch, timeout=15.0)
                tracks = list(result) if result else []
                self._set_cached("mix", cache_key, tracks, 300.0)
                return tracks
            except Exception as error:
                log.debug("Tidal track radio failed for %s: %s", track_id, error)
                return []

    async def get_video(self, video_id: str) -> Optional[Any]:
        if not self.session or not hasattr(self.session, "video"):
            return None
        cached = self._get_cached("video", video_id)
        if cached is not None:
            return cached
        async with self.api_semaphore:
            try:
                res = await self._run_with_backoff(lambda: self.session.video(video_id), timeout=10.0)
                if res:
                    self._set_cached("video", video_id, res, 3600.0)
                return res
            except Exception as e:
                log.debug(f"Failed to fetch video {video_id}: {e}")
                return None

    async def get_album(self, album_id: str) -> Optional[Any]:
        if not self.session:
            return None
        cached = self._get_cached("album", album_id)
        if cached is not None:
            return cached
        async with self.api_semaphore:
            try:
                res = await self._run_with_backoff(lambda: self.session.album(album_id), timeout=10.0)
                if res:
                    self._set_cached("album", album_id, res, 1800.0)
                return res
            except Exception:
                return None

    async def get_playlist(self, playlist_id: str) -> Optional[Any]:
        if not self.session:
            return None
        cached = self._get_cached("playlist", playlist_id)
        if cached is not None:
            return cached
        async with self.api_semaphore:
            try:
                res =