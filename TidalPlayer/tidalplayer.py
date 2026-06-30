"""
TidalPlayer - Tidal music integration for Red Discord Bot
Features: Hi-Res Audio, Album Art, Spotify/YT Importing, MixV2, Video URLs,
          Hybrid Slash Commands, Similar Albums, UserPlaylist Mgmt, Rich UI
"""

import asyncio
import logging
import re
from collections import OrderedDict, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from itertools import islice
from typing import Any, Callable, Dict, List, NamedTuple, Optional, Set, Tuple, TypedDict

import discord
from redbot.core import Config, app_commands, commands
from redbot.core.bot import Red
from redbot.core.utils.menus import SimpleMenu

try:
    import lavalink
    LAVALINK_AVAILABLE = True
except ImportError:
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

COG_IDENTIFIER = 160819386
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

EMOJI_OK = "\u2705"
EMOJI_NO = "\u274c"

COLOR_BLUE = discord.Color.blue()
COLOR_GREEN = discord.Color.green()
COLOR_RED = discord.Color.red()
COLOR_BLURPLE = discord.Color.blurple()
COLOR_TEAL = discord.Color.teal()
COLOR_PURPLE = discord.Color.purple()

QUALITY_LABELS = {
    "HI_RES_LOSSLESS": "HI-RES LOSSLESS (FLAC)",
    "LOSSLESS": "LOSSLESS (FLAC)",
    "HIGH": "HIGH (320kbps)",
    "LOW": "LOW (96kbps)",
}

FILTER_KEYWORDS = frozenset(
    {"sped up", "slowed", "tiktok", "reverb", "8d audio", "bass boosted",
     "reverbed", "slowed down", "nightcore", "daycore"}
)
FILTER_REGEX = re.compile(
    "|".join(re.escape(kw) for kw in FILTER_KEYWORDS),
    re.IGNORECASE,
)

YOUTUBE_SKIP_TITLES = frozenset(
    {"[deleted video]", "private video", "[private video]"}
)

TIDAL_URL_PATTERNS = {
    "track": re.compile(r"tidal\.com/(?:browse/)?track/(\d+)"),
    "video": re.compile(r"tidal\.com/(?:browse/)?video/(\d+)"),
    "album": re.compile(r"tidal\.com/(?:browse/)?album/(\d+)"),
    "playlist": re.compile(r"tidal\.com/(?:browse/)?playlist/([a-f0-9-]+)"),
    "mix": re.compile(r"tidal\.com/(?:browse/)?mix/([a-f0-9A-Z_-]+)"),
}

SPOTIFY_PLAYLIST_PATTERN = re.compile(r"open\.spotify\.com/playlist/([a-zA-Z0-9]+)")
SPOTIFY_TRACK_PATTERN = re.compile(r"open\.spotify\.com/track/([a-zA-Z0-9]+)")
SPOTIFY_ALBUM_PATTERN = re.compile(r"open\.spotify\.com/album/([a-zA-Z0-9]+)")
YOUTUBE_PLAYLIST_PATTERN = re.compile(r"youtube\.com/.*[?&]list=([a-zA-Z0-9_-]+)")
ISRC_PATTERN = re.compile(r"^isrc:([A-Z]{2}[A-Z0-9]{3}\d{7})$", re.IGNORECASE)

_TIDAL_URL_RE = re.compile(r"tidal\.com/")
_SPOTIFY_PLAYLIST_RE = re.compile(r"open\.spotify\.com/playlist/")
_SPOTIFY_ALBUM_RE = re.compile(r"open\.spotify\.com/album/")
_SPOTIFY_TRACK_RE = re.compile(r"open\.spotify\.com/track/")
_YOUTUBE_PLAYLIST_RE = re.compile(r"youtube\.com/.*[?&]list=")

_CACHE_CAPS: Dict[str, int] = {
    "search": 200,
    "track": 500,
    "isrc": 500,
    "album": 100,
    "playlist": 100,
    "mix": 50,
    "video": 100,
}


class TrackMeta(TypedDict):
    title: str
    artist: str
    album: Optional[str]
    duration: int
    quality: str
    image: Optional[str]
    share_url: Optional[str]
    audio_resolution: Optional[str]
    track_id: Optional[int]


class _PageResult(NamedTuple):
    items: List[Any]
    sparse_supported: Optional[bool]


class Messages:
    ERROR_NO_TIDALAPI = "tidalapi not installed. Run: `[p]pipinstall tidalapi`"
    ERROR_NOT_AUTHENTICATED = (
        "Not authenticated with Tidal. The bot owner must complete the OAuth flow "
        "(device code auth) before playback is available."
    )
    ERROR_NO_AUDIO_COG = "Audio cog not loaded. Run: `[p]load audio`"
    ERROR_NO_PLAYER = "No active player. Join a voice channel first."
    ERROR_NO_TRACKS_FOUND = "No tracks found."
    ERROR_INVALID_URL = "Invalid {platform} {content_type} URL"
    ERROR_CONTENT_UNAVAILABLE = "Content unavailable (private/region-locked)"
    ERROR_LAVALINK_FAILED = "Playback failed: Could not retrieve Tidal stream."
    ERROR_STILL_LOADING = "\u23f3 TidalPlayer is still initializing, please wait a moment."
    ERROR_NOT_PLAYING = "Nothing is currently playing."
    STATUS_PLAYING = "Playing from Tidal"
    PROGRESS_QUEUEING = "Queueing {name} ({count} tracks)..."
    STATUS_STOPPING = "Stopping playlist queueing..."
    SUCCESS_SPOTIFY_CONFIGURED = "Spotify configured."
    SUCCESS_YOUTUBE_CONFIGURED = "YouTube configured."
    SUCCESS_FILTER_ENABLED = "Remix/TikTok filter enabled."
    SUCCESS_FILTER_DISABLED = "Remix/TikTok filter disabled."
    SUCCESS_INTERACTIVE_ENABLED = "Interactive search enabled."
    SUCCESS_INTERACTIVE_DISABLED = "Interactive search disabled."
    SUCCESS_TOKENS_CLEARED = "Tokens cleared."
    SUCCESS_PARTIAL_QUEUE = "Queued {queued}/{total} ({skipped} skipped)"
    ERROR_TIMEOUT = "Selection timed out."
    ERROR_FETCH_FAILED = "Could not fetch playlist."
    ERROR_NO_SPOTIFY = (
        "Spotify not configured. Set credentials with: "
        "`[p]set api spotify client_id,<id> client_secret,<secret>`"
    )
    ERROR_NO_YOUTUBE = (
        "YouTube not configured. Set credentials with: "
        "`[p]set api youtube api_key,<key>`"
    )
    ERROR_NOT_USER_PLAYLIST = "That playlist is not a user-owned playlist. Use `[p]tpl list` to see your playlists."
    ERROR_PLAYLIST_WRITE_FAILED = "Playlist operation failed."
    ERROR_NO_QUEUE = "The queue is empty."


def truncate(text: str, limit: int) -> str:
    if len(text) > limit:
        return text[:limit - 3] + "..."
    return text


def make_tidal_url(content_type: str, content_id: Any) -> str:
    return f"https://listen.tidal.com/{content_type}/{content_id}"


def _is_tidal_track(obj: Any) -> bool:
    if TIDAL_MODELS_AVAILABLE and TidalTrack is not None:
        return isinstance(obj, TidalTrack)
    return (
        hasattr(obj, "id")
        and hasattr(obj, "duration")
        and (hasattr(obj, "get_stream") or hasattr(obj, "get_url"))
    )


def _error_embed(message: str) -> discord.Embed:
    return discord.Embed(description=f"{EMOJI_NO} {message}", color=COLOR_RED)


def _success_embed(message: str) -> discord.Embed:
    return discord.Embed(description=f"{EMOJI_OK} {message}", color=COLOR_GREEN)


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


def _utc_now() -> datetime:
    return datetime.now(tz=timezone.utc)


def _ensure_aware(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


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
                res = await self._run_with_backoff(lambda: self.session.playlist(playlist_id), timeout=10.0)
                if res:
                    self._set_cached("playlist", playlist_id, res, 300.0)
                return res
            except Exception:
                return None

    async def get_mix(self, mix_id: str) -> Optional[Any]:
        if not self.session:
            return None
        cached = self._get_cached("mix", mix_id)
        if cached is not None:
            return cached
        res = None
        async with self.api_semaphore:
            if hasattr(self.session, "mix_v2"):
                try:
                    res = await self._run_with_backoff(lambda: self.session.mix_v2(mix_id), timeout=10.0)
                except Exception:
                    pass
            if not res and hasattr(self.session, "mix"):
                try:
                    res = await self._run_with_backoff(lambda: self.session.mix(mix_id), timeout=10.0)
                except Exception:
                    pass
        if res:
            self._set_cached("mix", mix_id, res, 300.0)
        return res

    async def get_similar_albums(self, album: Any) -> List[Any]:
        if not album or not hasattr(album, "similar"):
            return []
        async with self.api_semaphore:
            try:
                result = await self._run_with_backoff(album.similar, timeout=10.0)
                return list(result) if result else []
            except Exception as e:
                log.debug(f"get_similar_albums failed: {e}")
                return []

    async def get_album_review(self, album: Any) -> Optional[str]:
        if not album or not hasattr(album, "review"):
            return None
        async with self.api_semaphore:
            try:
                result = await self._run_with_backoff(album.review, timeout=10.0)
                if isinstance(result, str):
                    return result
                if hasattr(result, "text"):
                    return result.text
                return str(result) if result else None
            except Exception:
                return None

    async def get_user_playlists(self) -> List[Any]:
        if not self.session or not hasattr(self.session, "user"):
            return []
        async with self.api_semaphore:
            try:
                def _fetch():
                    user = self.session.user
                    if hasattr(user, "playlists"):
                        val = user.playlists
                        return list(val() if callable(val) else val)
                    return []
                return await self._run_with_backoff(_fetch, timeout=15.0)
            except Exception as e:
                log.debug(f"get_user_playlists failed: {e}")
                return []

    async def get_user_playlist_by_id(self, playlist_id: str) -> Optional[Any]:
        if not self.session:
            return None
        try:
            pl = await self.get_playlist(playlist_id)
            if pl is None:
                return None
            creator = getattr(pl, "creator", None)
            session_user = getattr(self.session, "user", None)
            if creator is None or session_user is None:
                return pl
            creator_id = getattr(creator, "id", None)
            user_id = getattr(session_user, "id", None)
            if creator_id is not None and user_id is not None and str(creator_id) != str(user_id):
                return None
            return pl
        except Exception as e:
            log.debug(f"get_user_playlist_by_id failed for {playlist_id}: {e}")
            return None

    async def create_user_playlist(self, name: str, description: str = "") -> Optional[Any]:
        if not self.session or not hasattr(self.session, "user"):
            return None
        async with self.api_semaphore:
            try:
                def _create():
                    user = self.session.user
                    if hasattr(user, "create_playlist"):
                        return user.create_playlist(name, description)
                    return None
                return await self._run_with_backoff(_create, timeout=15.0)
            except Exception as e:
                log.error(f"create_user_playlist failed: {e}")
                return None

    async def add_track_to_playlist(self, playlist: Any, track_id: int) -> bool:
        if not playlist or not hasattr(playlist, "add"):
            return False
        async with self.api_semaphore:
            try:
                await self._run_with_backoff(lambda: playlist.add([track_id]), timeout=10.0)
                return True
            except Exception as e:
                log.error(f"add_track_to_playlist failed: {e}")
                return False

    async def remove_track_from_playlist(self, playlist: Any, track_id: int) -> bool:
        if not playlist or not hasattr(playlist, "remove_by_id"):
            return False
        async with self.api_semaphore:
            try:
                await self._run_with_backoff(lambda: playlist.remove_by_id(track_id), timeout=10.0)
                return True
            except Exception as e:
                log.error(f"remove_track_from_playlist failed: {e}")
                return False

    async def get_items(self, container: Any) -> List[Any]:
        if hasattr(container, "items") and callable(container.items):
            try:
                return await self._paginate_items(container)
            except Exception as e:
                log.warning(f"Paginated fetch failed, falling back to legacy: {e}")
        def _fetch():
            if hasattr(container, "tracks"):
                val = container.tracks
                return list(val() if callable(val) else val)
            if hasattr(container, "items"):
                val = container.items
                return list(val() if callable(val) else val)
            return []
        async with self.api_semaphore:
            try:
                items = await self._run_with_backoff(_fetch, timeout=30.0)
            except asyncio.TimeoutError:
                log.error("Timed out extracting items from Tidal container")
                return []
            except Exception as e:
                log.error(f"Failed to extract items: {e}")
                return []
        if len(items) > MAX_ITEMS:
            log.warning(f"Truncating Tidal container from {len(items)} to {MAX_ITEMS} items")
        return items[:MAX_ITEMS]

    async def _paginate_items(self, container: Any) -> List[Any]:
        all_items: List[Any] = []
        offset = 0
        _sparse_supported: Optional[bool] = None
        while len(all_items) < MAX_ITEMS:
            await asyncio.sleep(0)
            async with self.api_semaphore:
                try:
                    def _fetch(o: int = offset, sparse: Optional[bool] = _sparse_supported) -> _PageResult:
                        if sparse is False:
                            return _PageResult(
                                items=list(container.items(limit=PAGINATION_LIMIT, offset=o)),
                                sparse_supported=None,
                            )
                        try:
                            result = list(container.items(limit=PAGINATION_LIMIT, offset=o, sparse_album=True))
                            return _PageResult(items=result, sparse_supported=True)
                        except TypeError:
                            return _PageResult(
                                items=list(container.items(limit=PAGINATION_LIMIT, offset=o)),
                                sparse_supported=False,
                            )
                    page: _PageResult = await self._run_with_backoff(_fetch, timeout=25.0)
                except asyncio.TimeoutError:
                    log.error(f"Pagination timeout at offset {offset}")
                    break
                except Exception as e:
                    log.error(f"Pagination error at offset {offset}: {e}")
                    break
            if _sparse_supported is None and page.sparse_supported is not None:
                _sparse_supported = page.sparse_supported
            if not page.items:
                break
            all_items.extend(page.items)
            if len(page.items) < PAGINATION_LIMIT:
                break
            offset += PAGINATION_LIMIT
        return all_items[:MAX_ITEMS]

    async def get_audio_resolution(self, album_obj: Any) -> Optional[Tuple[int, int]]:
        if not album_obj or not hasattr(album_obj, "get_audio_resolution"):
            return None
        try:
            res = await self._run_blocking(album_obj.get_audio_resolution, timeout=5.0)
            if res:
                entry = res[0] if isinstance(res, (list, tuple)) and len(res) > 0 else res
                if hasattr(entry, "__iter__") and not isinstance(entry, str):
                    parts = list(entry)
                    if len(parts) >= 2:
                        return int(parts[0]), int(parts[1])
        except Exception:
            pass
        return None

    async def get_stream_url(self, track: Any) -> Optional[str]:
        track_id = getattr(track, "id", None)
        async with self.api_semaphore:
            try:
                def _get_urls() -> List[str]:
                    stream = track.get_stream()
                    return stream.get_urls()
                urls = await self._run_with_backoff(_get_urls, timeout=15.0)
                if urls:
                    return urls[0]
            except asyncio.TimeoutError:
                log.debug(f"get_stream().get_urls() timed out for track {track_id}")
            except AttributeError:
                pass
            except Exception as e:
                log.debug(f"get_stream().get_urls() failed for track {track_id}: {e}")
        async with self.api_semaphore:
            try:
                url = await self._run_with_backoff(track.get_url, timeout=10.0)
                if url:
                    return url
            except Exception as e:
                log.debug(f"get_url() failed for track {track_id}: {e}")
        if track_id:
            return make_tidal_url("track", track_id)
        return None

    def _extract_tracks(self, result: Any) -> List[Any]:
        if (t := getattr(result, "tracks", None)) is not None:
            return t if isinstance(t, list) else getattr(t, "items", [])
        if isinstance(result, dict):
            t = result.get("tracks", [])
            return t if isinstance(t, list) else getattr(t, "items", [])
        return result if isinstance(result, list) else []

    def _filter_tracks(self, tracks: List[Any]) -> List[Any]:
        if not tracks:
            return []
        return [t for t in tracks if not FILTER_REGEX.search(getattr(t, "name", "") or "")]


class TidalPlayer(commands.Cog):
    """Play music from Tidal with full metadata support."""

    __slots__ = (
        "bot", "config", "tidal", "sp", "yt", "_tasks", "_guild_locks",
        "_cancel_events", "_last_progress_edit", "_initialized", "_current_meta",
    )

    def __init__(self, bot: Red):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=COG_IDENTIFIER, force_registration=True)
        self.config.register_global(
            token_type=None, access_token=None, refresh_token=None,
            expiry_time=None,
            _schema_version=3,
        )
        self.config.register_guild(filter_remixes=True, interactive_search=False)
        self.tidal = TidalHandler(bot, self.config)
        self.sp: Optional[Any] = None
        self.yt: Optional[Any] = None
        self._tasks: Set[asyncio.Task] = set()
        self._guild_locks: Dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._cancel_events: Dict[int, asyncio.Event] = defaultdict(asyncio.Event)
        self._last_progress_edit: Dict[int, float] = {}
        self._current_meta: Dict[int, TrackMeta] = {}
        self._initialized: bool = False

    async def cog_load(self) -> None:
        await self._migrate_config()
        await self._initialize_apis()

    async def _migrate_config(self) -> None:
        try:
            version = await self.config._schema_version()
            if version is None or version < 3:
                await self.config.clear_raw("spotify_client_id")
                await self.config.clear_raw("spotify_client_secret")
                await self.config.clear_raw("youtube_api_key")
                await self.config._schema_version.set(3)
                log.info("TidalPlayer: config migrated to schema v3 (cleared legacy API keys)")
        except Exception as e:
            log.warning(f"Config migration check failed (non-fatal): {e}")

    def cog_unload(self) -> None:
        for ev in self._cancel_events.values():
            ev.set()
        for t in list(self._tasks):
            t.cancel()
        self.tidal.unload()
        self.sp = None
        self.yt = None
        self._guild_locks.clear()
        self._cancel_events.clear()
        self._current_meta.clear()
        self._last_progress_edit.clear()
        log.info("TidalPlayer cog unloaded")

    async def cog_command_error(self, ctx: commands.Context, error: Exception) -> None:
        if isinstance(error, commands.CommandInvokeError):
            log.error(
                f"Unhandled error in command {ctx.command}: {error.original}",
                exc_info=error.original,
            )
            await ctx.send(embed=_error_embed("An unexpected error occurred. Please try again later."))

    async def cog_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        if isinstance(error, app_commands.CommandInvokeError):
            log.error(
                f"Unhandled error in app command {interaction.command}: {error.original}",
                exc_info=error.original,
            )
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    embed=_error_embed("An unexpected error occurred. Please try again later."),
                    ephemeral=True,
                )
            else:
                await interaction.followup.send(
                    embed=_error_embed("An unexpected error occurred. Please try again later."),
                    ephemeral=True,
                )

    @commands.Cog.listener()
    async def on_guild_remove(self, guild: discord.Guild) -> None:
        self._guild_locks.pop(guild.id, None)
        self._cancel_events.pop(guild.id, None)
        self._current_meta.pop(guild.id, None)
        self._last_progress_edit.pop(guild.id, None)

    @commands.Cog.listener()
    async def on_wavelink_track_end(self, *args: Any, **kwargs: Any) -> None:
        if args:
            raw = args[0]
            player = raw if hasattr(raw, "queue") else getattr(raw, "player", None)
        else:
            player = kwargs.get("player")
        if player is None:
            return
        guild_id = (
            getattr(player, "guild_id", None)
            or getattr(getattr(player, "guild", None), "id", None)
        )
        if guild_id and guild_id in self._current_meta:
            queue = getattr(player, "queue", None)
            queue_empty = not queue or not len(queue)
            if queue_empty and not getattr(player, "current", None):
                self._current_meta.pop(guild_id, None)

    async def _initialize_apis(self) -> None:
        t0 = asyncio.get_running_loop().time()
        creds = await self.config.all()
        results = await asyncio.gather(
            self.tidal.initialize(creds),
            self._initialize_spotify(),
            self._initialize_youtube(),
            return_exceptions=True,
        )
        for name, r in zip(["Tidal", "Spotify", "YouTube"], results):
            if isinstance(r, Exception):
                log.error(f"{name} init error: {r}")
        elapsed = asyncio.get_running_loop().time() - t0
        self._initialized = True
        self.tidal.start_refresh_loop()
        log.info(f"TidalPlayer fully initialized in {elapsed:.2f}s")

    async def _initialize_spotify(self) -> None:
        if not SPOTIFY_AVAILABLE:
            return
        tokens = await self.bot.get_shared_api_tokens("spotify")
        cid = tokens.get("client_id")
        csec = tokens.get("client_secret")
        if cid and csec:
            try:
                self.sp = await self.tidal._run_blocking(
                    lambda: spotipy.Spotify(
                        client_credentials_manager=SpotifyClientCredentials(cid, csec)
                    ),
                    timeout=15.0,
                )
            except Exception as e:
                log.error(f"Spotify init failed: {e}")

    async def _initialize_youtube(self) -> None:
        if not YOUTUBE_API_AVAILABLE:
            return
        tokens = await self.bot.get_shared_api_tokens("youtube")
        key = tokens.get("api_key")
        if key:
            try:
                self.yt = await self.tidal._run_blocking(
                    lambda: build("youtube", "v3", developerKey=key),
                    timeout=15.0,
                )
            except Exception as e:
                log.error(f"YouTube init failed: {e}")

    @commands.Cog.listener()
    async def on_red_api_tokens_update(self, service_name: str, api_tokens: Dict[str, str]) -> None:
        if service_name == "spotify":
            await self._initialize_spotify()
        elif service_name == "youtube":
            await self._initialize_youtube()

    def _build_meta_sync(self, track: Any) -> TrackMeta:
        full_name = getattr(track, "full_name", None)
        name = full_name or getattr(track, "name", "Unknown") or "Unknown"
        artist_obj = getattr(track, "artist", None)
        artist = getattr(artist_obj, "name", "Unknown") if artist_obj else "Unknown"
        album_obj = getattr(track, "album", None)
        album = getattr(album_obj, "name", None) if album_obj else None
        duration = int(getattr(track, "duration", 0) or 0)
        quality = getattr(track, "audio_quality", "LOSSLESS") or "LOSSLESS"
        track_id = getattr(track, "id", None)
        is_video = getattr(track, "video_quality", None) is not None
        content_type = "video" if is_video else "track"
        share_url = make_tidal_url(content_type, track_id) if track_id else None
        meta: TrackMeta = {
            "title": name, "artist": artist, "album": album, "duration": duration,
            "quality": quality, "image": None, "share_url": share_url,
            "audio_resolution": None, "track_id": track_id,
        }
        try:
            if album_obj and hasattr(album_obj, "image"):
                meta["image"] = album_obj.image(dimensions=640)
            elif album_obj and hasattr(album_obj, "cover") and album_obj.cover:
                uuid = album_obj.cover.replace("-", "/")
                meta["image"] = f"https://resources.tidal.com/images/{uuid}/640x640.jpg"
        except Exception:
            pass
        return meta

    async def _extract_meta(self, track: Any, skip_audio_res: bool = False) -> TrackMeta:
        meta = self._build_meta_sync(track)
        if meta["quality"] == "HI_RES_LOSSLESS" and not skip_audio_res:
            album_obj = getattr(track, "album", None)
            if album_obj:
                res = await self.tidal.get_audio_resolution(album_obj)
                if res:
                    bit_depth, sample_rate = res
                    khz = sample_rate // 1000 if sample_rate >= 1000 else sample_rate
                    meta["audio_resolution"] = f"HI-RES LOSSLESS ({bit_depth}-bit / {khz}kHz)"
        return meta

    def _format_duration(self, seconds: int) -> str:
        m, s = divmod(seconds, 60)
        h, m = divmod(m, 60)
        if h:
            return f"{h}:{m:02d}:{s:02d}"
        return f"{m:02d}:{s:02d}"

    async def _get_player(self, ctx: commands.Context, connect: bool = False) -> Optional[Any]:
        if not LAVALINK_AVAILABLE:
            return None
        try:
            return lavalink.get_player(ctx.guild.id)
        except Exception:
            pass
        if connect and ctx.author.voice and ctx.author.voice.channel:
            try:
                await lavalink.connect(ctx.author.voice.channel)
                return lavalink.get_player(ctx.guild.id)
            except Exception as e:
                log.debug(f"Failed to connect to VC: {e}")
        return None

    async def _ensure_player(self, ctx: commands.Context) -> Optional[Any]:
        player = await self._get_player(ctx, connect=True)
        if not player:
            await ctx.send(embed=_error_embed(Messages.ERROR_NO_PLAYER))
        return player

    async def _ensure_vc_connected(self, ctx: commands.Context, player: Any) -> Optional[Any]:
        if getattr(player, "is_connected", True):
            return player
        for attempt in range(VC_RECONNECT_RETRIES):
            await asyncio.sleep(VC_RECONNECT_DELAY)
            new_player = await self._get_player(ctx, connect=True)
            if new_player and getattr(new_player, "is_connected", False):
                log.info(f"Reconnected to VC (attempt {attempt + 1})")
                return new_player
        log.warning("Could not reconnect to VC, stopping queue")
        return None

    async def _queue_resolved_chunk(
        self,
        ctx: commands.Context,
        player: Any,
        resolved_chunk: List[Optional[Tuple[Any, str, TrackMeta]]],
        cancel_event: asyncio.Event,
    ) -> Tuple[int, int]:
        queued = skipped = 0
        for res in resolved_chunk:
            if cancel_event.is_set():
                break
            if res is None:
                skipped += 1
                continue
            track, stream_url, meta = res
            loaded_track = None
            try:
                results = await player.load_tracks(stream_url)
                if results and results.tracks:
                    loaded_track = results.tracks[0]
            except Exception as e:
                log.error(f"Lavalink load failed: {e}")
            if loaded_track:
                loaded_track.title = truncate(meta["title"], 100)
                loaded_track.author = (
                    f"{meta['artist']} - {meta['album']}" if meta.get("album") else meta["artist"]
                )
                player.add(ctx.author, loaded_track)
                if not player.current:
                    await player.play()
                self._current_meta[ctx.guild.id] = meta
                queued += 1
            else:
                skipped += 1
        return queued, skipped

    async def _load_and_queue_track(
        self,
        ctx: commands.Context,
        tidal_track: Any,
        show_embed: bool = True,
        skip_audio_res: bool = False,
    ) -> bool:
        if not ctx.guild:
            return False
        meta = await self._extract_meta(tidal_track, skip_audio_res=skip_audio_res)
        player = await self._get_player(ctx, connect=True)
        if not player:
            await ctx.send(embed=_error_embed(Messages.ERROR_NO_PLAYER))
            return False
        stream_url = await self.tidal.get_stream_url(tidal_track)
        loaded_track = None
        if stream_url:
            try:
                results = await player.load_tracks(stream_url)
                if results and results.tracks:
                    loaded_track = results.tracks[0]
            except Exception as e:
                log.error(f"Lavalink load failed: {e}")
        if not loaded_track:
            await ctx.send(embed=_error_embed(Messages.ERROR_LAVALINK_FAILED))
            return False
        loaded_track.title = truncate(meta["title"], 100)
        loaded_track.author = f"{meta['artist']} - {meta['album']}" if meta.get("album") else meta["artist"]
        player.add(ctx.author, loaded_track)
        if not player.current:
            await player.play()
        self._current_meta[ctx.guild.id] = meta
        if show_embed:
            await self._send_now_playing(ctx, meta)
        return True

    def _build_now_playing_embed(self, meta: TrackMeta) -> discord.Embed:
        desc_parts = [f"**{meta['title']}**", meta["artist"]]
        if meta.get("album"):
            desc_parts.append(f"_{meta['album']}_")
        embed = discord.Embed(
            title=Messages.STATUS_PLAYING, description="\n".join(desc_parts), color=COLOR_BLUE
        )
        quality_display = meta.get("audio_resolution") or QUALITY_LABELS.get(meta["quality"], meta["quality"])
        embed.add_field(name="Quality", value=quality_display, inline=True)
        if meta.get("share_url"):
            embed.add_field(name="Open in TIDAL", value=f"[Listen]({meta['share_url']})", inline=True)
        embed.set_footer(text=f"Duration: {self._format_duration(meta['duration'])}")
        if meta.get("image"):
            embed.set_thumbnail(url=meta["image"])
        return embed

    async def _send_now_playing(self, ctx: commands.Context, meta: TrackMeta) -> None:
        await ctx.send(embed=self._build_now_playing_embed(meta))

    async def _interactive_select(self, ctx: commands.Context, tracks: List[Any]) -> Optional[Any]:
        if not tracks:
            return None
        top = tracks[:5]
        desc = []
        for i, t in enumerate(top):
            name = getattr(t, "full_name", None) or getattr(t, "name", "Unknown")
            artist_obj = getattr(t, "artist", None)
            artist = getattr(artist_obj, "name", "Unknown") if artist_obj else "Unknown"
            album_obj = getattr(t, "album", None)
            album = getattr(album_obj, "name", None) if album_obj else None
            dur = self._format_duration(int(getattr(t, "duration", 0) or 0))
            line = f"**{i + 1}.** {name} \u2014 {artist}"
            if album:
                line += f" *({album})*"
            line += f" `[{dur}]`"
            desc.append(line)
        embed = discord.Embed(title="Select a Track", description="\n".join(desc), color=COLOR_BLUE)
        view = TrackSelectView(top, ctx.author, timeout=float(INTERACTIVE_TIMEOUT))
        msg = await ctx.send(embed=embed, view=view)
        selected = await view.wait_for_selection()
        try:
            await msg.delete()
        except Exception:
            pass
        if view._timed_out:
            await ctx.send(embed=_error_embed(Messages.ERROR_TIMEOUT))
        return selected

    async def _edit_progress_message(self, msg: discord.Message, embed: discord.Embed) -> None:
        guild_id = msg.guild.id if msg.guild else msg.id
        now = asyncio.get_running_loop().time()
        if now - self._last_progress_edit.get(guild_id, 0.0) < PROGRESS_EDIT_RATELIMIT:
            return
        try:
            await msg.edit(embed=embed)
            self._last_progress_edit[guild_id] = now
        except Exception:
            pass

    async def _fetch_all_spotify_tracks(self, playlist_id: str) -> List[Any]:
        all_items: List[Any] = []
        offset = 0
        while len(all_items) < MAX_ITEMS:
            resp = await self.tidal._run_blocking(
                lambda o=offset: self.sp.playlist_tracks(
                    playlist_id, limit=100, offset=o,
                    fields="items(track(name,artists(name),external_ids)),next",
                ),
                timeout=20.0,
            )
            all_items.extend(i for i in resp.get("items", []) if i.get("track"))
            if not resp.get("next"):
                break
            offset += 100
            await asyncio.sleep(0)
        return all_items[:MAX_ITEMS]

    async def _fetch_all_spotify_album_tracks(self, album_id: str) -> Tuple[List[Any], str]:
        all_items: List[Any] = []
        album_name = album_id
        try:
            alb = await self.tidal._run_blocking(lambda: self.sp.album(album_id), timeout=15.0)
            album_name = alb.get("name", album_id)
            tracks = alb.get("tracks", {})
            all_items.extend(tracks.get("items", []))
            next_url = tracks.get("next")
            while next_url and len(all_items) < MAX_ITEMS:
                resp = await self.tidal._run_blocking(
                    lambda u=next_url: self.sp._get(u), timeout=20.0
                )
                all_items.extend(resp.get("items", []))
                next_url = resp.get("next")
                await asyncio.sleep(0)
        except Exception as e:
            log.error(f"Spotify album fetch error: {e}")
        return all_items[:MAX_ITEMS], album_name

    async def _fetch_all_youtube_tracks(self, playlist_id: str) -> List[Any]:
        all_items: List[Any] = []
        page_token: Optional[str] = None
        while True:
            kwargs: Dict[str, Any] = {"part": "snippet", "playlistId": playlist_id, "maxResults": 50}
            if page_token:
                kwargs["pageToken"] = page_token
            resp = await self.tidal._run_blocking(
                self.yt.playlistItems().list(**kwargs).execute, timeout=20.0
            )
            for item in resp.get("items", []):
                if item.get("snippet", {}).get("title", "").lower() not in YOUTUBE_SKIP_TITLES:
                    all_items.append(item)
            page_token = resp.get("nextPageToken")
            if not page_token or len(all_items) >= MAX_ITEMS:
                break
            await asyncio.sleep(0)
        return all_items[:MAX_ITEMS]

    async def _resolve_and_extract(
        self,
        item: Any,
        item_processor: Callable[[Any], Any],
        filter_remixes: bool,
    ) -> Optional[Tuple[Any, str, TrackMeta]]:
        try:
            query = item_processor(item)
            if not query:
                return None
            track = None
            if _is_tidal_track(query):
                track = query
            else:
                if isinstance(query, str) and ISRC_PATTERN.match(query):
                    isrc = ISRC_PATTERN.match(query).group(1).upper()
                    track = await self.tidal.get_track_by_isrc(isrc)
                if not track:
                    results = await self.tidal.search(query, filter_remixes=filter_remixes)
                    if results:
                        track = results[0]
            if not track:
                return None
            meta_result, stream_url_result = await asyncio.gather(
                self._extract_meta(track, skip_audio_res=True),
                self.tidal.get_stream_url(track),
                return_exceptions=True,
            )
            if isinstance(meta_result, Exception):
                log.error(f"Error extracting metadata: {meta_result}")
                return None
            if isinstance(stream_url_result, Exception):
                log.error(f"Error getting stream URL: {stream_url_result}")
                return None
            if not stream_url_result:
                return None
            return track, stream_url_result, meta_result
        except Exception as e:
            log.error(f"Failed to resolve track concurrently: {e}")
            return None

    async def _process_track_list(
        self,
        ctx: commands.Context,
        items: List[Any],
        name: str,
        item_processor: Callable[[Any], Any],
        color: discord.Color = discord.Color.blue(),
        thumbnail_url: Optional[str] = None,
    ) -> None:
        if not items:
            await ctx.send(embed=_error_embed(Messages.ERROR_NO_TRACKS_FOUND))
            return
        lock = self._guild_locks[ctx.guild.id]
        async with lock:
            if not await self._check_ready(ctx):
                return
            filter_remixes = await self.config.guild(ctx.guild).filter_remixes()
            player = await self._ensure_player(ctx)
            if not player:
                return
            cancel_event = self._cancel_events[ctx.guild.id]
            trunc_name = truncate(name, 50)
            total = len(items)
            initial_embed = discord.Embed(
                title=Messages.PROGRESS_QUEUEING.format(name=trunc_name, count=total), color=color
            )
            if thumbnail_url:
                initial_embed.set_thumbnail(url=thumbnail_url)
            pmsg = await ctx.send(embed=initial_embed)
            queued, skipped, last_up = 0, 0, 0
            try:
                for chunk_start in range(0, total, SEARCH_BATCH_SIZE):
                    if cancel_event.is_set():
                        break
                    player = await self._ensure_vc_connected(ctx, player)
                    if player is None:
                        break
                    chunk_items = items[chunk_start:chunk_start + SEARCH_BATCH_SIZE]
                    tasks = [
                        self._resolve_and_extract(item, item_processor, filter_remixes)
                        for item in chunk_items
                    ]
                    resolved_chunk = await asyncio.gather(*tasks)
                    chunk_queued, chunk_skipped = await self._queue_resolved_chunk(
                        ctx, player, list(resolved_chunk), cancel_event
                    )
                    queued += chunk_queued
                    skipped += chunk_skipped
                    current_count = min(chunk_start + SEARCH_BATCH_SIZE, total)
                    if current_count - last_up >= BATCH_UPDATE_INTERVAL or current_count == total:
                        upd = discord.Embed(
                            title=Messages.PROGRESS_QUEUEING.format(name=trunc_name, count=total),
                            description=Messages.SUCCESS_PARTIAL_QUEUE.format(
                                queued=queued, total=total, skipped=skipped
                            ),
                            color=color,
                        )
                        if thumbnail_url:
                            upd.set_thumbnail(url=thumbnail_url)
                        await self._edit_progress_message(pmsg, upd)
                        last_up = current_count
                    await asyncio.sleep(0.1)
                final = discord.Embed(
                    title=Messages.SUCCESS_PARTIAL_QUEUE.format(queued=queued, total=total, skipped=skipped),
                    description=f"Source: {truncate(name, 100)}",
                    color=color,
                )
                if thumbnail_url:
                    final.set_thumbnail(url=thumbnail_url)
                try:
                    await pmsg.edit(embed=final)
                except Exception:
                    pass
            except Exception as e:
                log.error(f"Queue processing error: {e}")
                try:
                    await pmsg.edit(embed=_error_embed(Messages.ERROR_FETCH_FAILED))
                except Exception:
                    pass
            finally:
                cancel_event.clear()

    async def _check_ready(self, ctx: commands.Context) -> bool:
        if not self._initialized:
            await ctx.send(embed=_error_embed(Messages.ERROR_STILL_LOADING))
            return False
        if not TIDALAPI_AVAILABLE:
            await ctx.send(embed=_error_embed(Messages.ERROR_NO_TIDALAPI))
            return False
        if not await self.tidal.is_logged_in():
            await ctx.send(embed=_error_embed(Messages.ERROR_NOT_AUTHENTICATED))
            return False
        if not LAVALINK_AVAILABLE:
            await ctx.send(embed=_error_embed(Messages.ERROR_NO_AUDIO_COG))
            return False
        return True

    async def _handle_tidal_url(self, ctx: commands.Context, url: str) -> None:
        for kind, pattern in TIDAL_URL_PATTERNS.items():
            m = pattern.search(url)
            if not m:
                continue
            match kind:
                case "track":
                    await self._handle_track(ctx, m.group(1))
                case "video":
                    await self._handle_video(ctx, m.group(1))
                case "album":
                    await self._handle_album(ctx, m.group(1))
                case "playlist":
                    await self._handle_playlist(ctx, m.group(1))
                case "mix":
                    await self._handle_mix(ctx, m.group(1))
                case _:
                    await ctx.send(embed=_error_embed(
                        Messages.ERROR_INVALID_URL.format(platform="Tidal", content_type="link")
                    ))
            return
        await ctx.send(embed=_error_embed(
            Messages.ERROR_INVALID_URL.format(platform="Tidal", content_type="link")
        ))

    async def _handle_track(self, ctx: commands.Context, tid: str) -> None:
        t = await self.tidal.get_track(tid)
        if t:
            await self._load_and_queue_track(ctx, t)
        else:
            await ctx.send(embed=_error_embed(Messages.ERROR_NO_TRACKS_FOUND))

    async def _handle_video(self, ctx: commands.Context, vid: str) -> None:
        v = await self.tidal.get_video(vid)
        if v:
            await self._load_and_queue_track(ctx, v)
        else:
            await ctx.send(embed=_error_embed(Messages.ERROR_NO_TRACKS_FOUND))

    async def _handle_album(self, ctx: commands.Context, aid: str) -> None:
        alb = await self.tidal.get_album(aid)
        if not alb:
            await ctx.send(embed=_error_embed(Messages.ERROR_CONTENT_UNAVAILABLE))
            return
        async def _get_thumb() -> Optional[str]:
            try:
                if hasattr(alb, "image"):
                    return alb.image(dimensions=320)
            except Exception:
                pass
            return None
        tracks, thumb = await asyncio.gather(self.tidal.get_items(alb), _get_thumb())
        await self._process_track_list(ctx, tracks, getattr(alb, "name", aid), lambda t: t, thumbnail_url=thumb)

    async def _handle_playlist(self, ctx: commands.Context, pid: str) -> None:
        pl = await self.tidal.get_playlist(pid)
        if not pl:
            await ctx.send(embed=_error_embed(Messages.ERROR_CONTENT_UNAVAILABLE))
            return
        tracks = await self.tidal.get_items(pl)
        await self._process_track_list(ctx, tracks, getattr(pl, "name", pid), lambda t: t)

    async def _handle_mix(self, ctx: commands.Context, mid: str) -> None:
        mix = await self.tidal.get_mix(mid)
        if not mix:
            await ctx.send(embed=_error_embed(Messages.ERROR_CONTENT_UNAVAILABLE))
            return
        items = await self.tidal.get_items(mix)
        name = getattr(mix, "title", None) or getattr(mix, "name", None) or "Tidal Mix"
        await self._process_track_list(ctx, items, name, lambda t: t, COLOR_PURPLE)

    @commands.hybrid_command(name="tplay")
    async def tplay(self, ctx: commands.Context, *, query: str):
        """Play a Tidal track, album, playlist, mix, Spotify link, YouTube playlist, or search query."""
        if not await self._check_ready(ctx):
            return
        if ISRC_PATTERN.match(query):
            isrc = ISRC_PATTERN.match(query).group(1).upper()
            track = await self.tidal.get_track_by_isrc(isrc)
            if track:
                await self._load_and_queue_track(ctx, track)
            else:
                await ctx.send(embed=_error_embed(Messages.ERROR_NO_TRACKS_FOUND))
            return
        if _TIDAL_URL_RE.search(query):
            await self._handle_tidal_url(ctx, query)
            return
        if _SPOTIFY_PLAYLIST_RE.search(query):
            await self._handle_spotify_playlist(ctx, query)
            return
        if _SPOTIFY_ALBUM_RE.search(query):
            await self._handle_spotify_album(ctx, query)
            return
        if _SPOTIFY_TRACK_RE.search(query):
            await self._handle_spotify_track(ctx, query)
            return
        if _YOUTUBE_PLAYLIST_RE.search(query):
            await self._handle_youtube_playlist(ctx, query)
            return
        filter_remixes, interactive = await asyncio.gather(
            self.config.guild(ctx.guild).filter_remixes(),
            self.config.guild(ctx.guild).interactive_search(),
        )
        results = await self.tidal.search(query, filter_remixes=filter_remixes)
        if not results:
            await ctx.send(embed=_error_embed(Messages.ERROR_NO_TRACKS_FOUND))
            return
        if interactive:
            selected = await self._interactive_select(ctx, results)
            if selected:
                await self._load_and_queue_track(ctx, selected)
        else:
            await self._load_and_queue_track(ctx, results[0])

    async def _handle_spotify_playlist(self, ctx: commands.Context, url: str) -> None:
        if not self.sp:
            await ctx.send(embed=_error_embed(Messages.ERROR_NO_SPOTIFY))
            return
        match = SPOTIFY_PLAYLIST_PATTERN.search(url)
        if not match:
            await ctx.send(embed=_error_embed(Messages.ERROR_INVALID_URL.format(platform="Spotify", content_type="playlist")))
            return
        playlist_id = match.group(1)
        try:
            meta = await self.tidal._run_blocking(
                lambda: self.sp.playlist(playlist_id, fields="name,images"), timeout=15.0
            )
            items = await self._fetch_all_spotify_tracks(playlist_id)
            thumb = meta.get("images", [{}])[0].get("url") if meta.get("images") else None
            await self._process_track_list(
                ctx, items, meta.get("name", "Spotify Playlist"),
                _spotify_item_to_query, color=COLOR_GREEN, thumbnail_url=thumb,
            )
        except Exception as e:
            log.error(f"Spotify playlist handling failed: {e}")
            await ctx.send(embed=_error_embed(Messages.ERROR_FETCH_FAILED))

    async def _handle_spotify_track(self, ctx: commands.Context, url: str) -> None:
        if not self.sp:
            await ctx.send(embed=_error_embed(Messages.ERROR_NO_SPOTIFY))
            return
        match = SPOTIFY_TRACK_PATTERN.search(url)
        if not match:
            await ctx.send(embed=_error_embed(Messages.ERROR_INVALID_URL.format(platform="Spotify", content_type="track")))
            return
        track_id = match.group(1)
        try:
            item = await self.tidal._run_blocking(lambda: self.sp.track(track_id), timeout=15.0)
            isrc = (item.get("external_ids", {}) or {}).get("isrc")
            if isrc:
                track = await self.tidal.get_track_by_isrc(isrc)
                if track:
                    await self._load_and_queue_track(ctx, track)
                    return
            filter_remixes = await self.config.guild(ctx.guild).filter_remixes()
            query = f"{item['name']} {' '.join(a['name'] for a in item.get('artists', []))}"
            results = await self.tidal.search(query, filter_remixes=filter_remixes)
            if results:
                await self._load_and_queue_track(ctx, results[0])
            else:
                await ctx.send(embed=_error_embed(Messages.ERROR_NO_TRACKS_FOUND))
        except Exception as e:
            log.error(f"Spotify track handling failed: {e}")
            await ctx.send(embed=_error_embed(Messages.ERROR_FETCH_FAILED))

    async def _handle_spotify_album(self, ctx: commands.Context, url: str) -> None:
        if not self.sp:
            await ctx.send(embed=_error_embed(Messages.ERROR_NO_SPOTIFY))
            return
        match = SPOTIFY_ALBUM_PATTERN.search(url)
        if not match:
            await ctx.send(embed=_error_embed(Messages.ERROR_INVALID_URL.format(platform="Spotify", content_type="album")))
            return
        album_id = match.group(1)
        try:
            album_meta = await self.tidal._run_blocking(lambda: self.sp.album(album_id), timeout=15.0)
            items, album_name = await self._fetch_all_spotify_album_tracks(album_id)
            thumb = album_meta.get("images", [{}])[0].get("url") if album_meta.get("images") else None
            await self._process_track_list(
                ctx, items, album_name,
                _spotify_album_item_to_query, color=COLOR_GREEN, thumbnail_url=thumb,
            )
        except Exception as e:
            log.error(f"Spotify album handling failed: {e}")
            await ctx.send(embed=_error_embed(Messages.ERROR_FETCH_FAILED))

    async def _handle_youtube_playlist(self, ctx: commands.Context, url: str) -> None:
        if not self.yt:
            await ctx.send(embed=_error_embed(Messages.ERROR_NO_YOUTUBE))
            return
        match = YOUTUBE_PLAYLIST_PATTERN.search(url)
        if not match:
            await ctx.send(embed=_error_embed(Messages.ERROR_INVALID_URL.format(platform="YouTube", content_type="playlist")))
            return
        playlist_id = match.group(1)
        try:
            pl_resp = await self.tidal._run_blocking(
                self.yt.playlists().list(part="snippet", id=playlist_id, maxResults=1).execute, timeout=15.0
            )
            title = pl_resp.get("items", [{}])[0].get("snippet", {}).get("title", "YouTube Playlist")
            thumb = pl_resp.get("items", [{}])[0].get("snippet", {}).get("thumbnails", {}).get("high", {}).get("url")
            items = await self._fetch_all_youtube_tracks(playlist_id)
            await self._process_track_list(
                ctx, items, title,
                lambda item: item.get("snippet", {}).get("title"),
                color=COLOR_RED, thumbnail_url=thumb,
            )
        except Exception as e:
            log.error(f"YouTube playlist handling failed: {e}")
            await ctx.send(embed=_error_embed(Messages.ERROR_FETCH_FAILED))

    @commands.hybrid_command(name="tsearch")
    async def tsearch(self, ctx: commands.Context, *, query: str):
        """Search Tidal and choose from top results."""
        if not await self._check_ready(ctx):
            return
        filter_remixes = await self.config.guild(ctx.guild).filter_remixes()
        results = await self.tidal.search(query, filter_remixes=filter_remixes)
        if not results:
            await ctx.send(embed=_error_embed(Messages.ERROR_NO_TRACKS_FOUND))
            return
        selected = await self._interactive_select(ctx, results)
        if selected:
            await self._load_and_queue_track(ctx, selected)

    @commands.hybrid_command(name="tnowplaying")
    async def tnowplaying(self, ctx: commands.Context):
        """Show what's currently playing."""
        meta = self._current_meta.get(ctx.guild.id) if ctx.guild else None
        if not meta:
            await ctx.send(embed=_error_embed(Messages.ERROR_NOT_PLAYING))
            return
        await ctx.send(embed=self._build_now_playing_embed(meta))

    @commands.hybrid_command(name="tqueue")
    async def tqueue(self, ctx: commands.Context):
        """Show the current queue."""
        if not await self._check_ready(ctx):
            return
        player = await self._get_player(ctx)
        if not player:
            await ctx.send(embed=_error_embed(Messages.ERROR_NO_PLAYER))
            return
        queue = getattr(player, "queue", None)
        if not queue or not len(queue):
            await ctx.send(embed=_error_embed(Messages.ERROR_NO_QUEUE))
            return
        queue_list = list(islice(queue, MAX_ITEMS))
        pages = []
        for start in range(0, len(queue_list), QUEUE_PAGE_SIZE):
            chunk = queue_list[start:start + QUEUE_PAGE_SIZE]
            desc = "\n".join(
                f"`{start + i + 1}.` {truncate(getattr(t, 'title', 'Unknown'), 60)} "
                f"\u2014 {truncate(getattr(t, 'author', 'Unknown'), 40)}"
                for i, t in enumerate(chunk)
            )
            embed = discord.Embed(
                title=f"Queue ({len(queue_list)} tracks)",
                description=desc,
                color=COLOR_BLUE,
            )
            pages.append(embed)
        if len(pages) == 1:
            await ctx.send(embed=pages[0])
        else:
            await SimpleMenu(pages).start(ctx)

    @commands.hybrid_command(name="tstop")
    async def tstop(self, ctx: commands.Context):
        """Stop queueing the current playlist."""
        if ctx.guild:
            self._cancel_events[ctx.guild.id].set()
            await ctx.send(embed=_success_embed(Messages.STATUS_STOPPING))

    @commands.hybrid_command(name="tfilter")
    @commands.guild_only()
    async def tfilter(self, ctx: commands.Context):
        """Toggle the remix/TikTok track filter."""
        current = await self.config.guild(ctx.guild).filter_remixes()
        await self.config.guild(ctx.guild).filter_remixes.set(not current)
        msg = Messages.SUCCESS_FILTER_DISABLED if current else Messages.SUCCESS_FILTER_ENABLED
        await ctx.send(embed=_success_embed(msg))

    @commands.hybrid_command(name="tinteractive")
    @commands.guild_only()
    async def tinteractive(self, ctx: commands.Context):
        """Toggle interactive search mode."""
        current = await self.config.guild(ctx.guild).interactive_search()
        await self.config.guild(ctx.guild).interactive_search.set(not current)
        msg = Messages.SUCCESS_INTERACTIVE_DISABLED if current else Messages.SUCCESS_INTERACTIVE_ENABLED
        await ctx.send(embed=_success_embed(msg))

    @commands.group(name="tpl")
    async def tpl(self, ctx: commands.Context):
        """Manage your Tidal playlists."""

    @tpl.command(name="list")
    async def tpl_list(self, ctx: commands.Context):
        """List your Tidal playlists."""
        if not await self._check_ready(ctx):
            return
        playlists = await self.tidal.get_user_playlists()
        if not playlists:
            await ctx.send(embed=_error_embed("No playlists found."))
            return
        pages = []
        for start in range(0, len(playlists), TPL_LIST_PAGE_SIZE):
            chunk = playlists[start:start + TPL_LIST_PAGE_SIZE]
            desc = "\n".join(
                f"`{start + i + 1}.` {truncate(getattr(p, 'name', 'Unnamed'), 60)}"
                for i, p in enumerate(chunk)
            )
            embed = discord.Embed(
                title=f"Your Tidal Playlists ({len(playlists)} total)",
                description=desc,
                color=COLOR_TEAL,
            )
            pages.append(embed)
        if len(pages) == 1:
            await ctx.send(embed=pages[0])
        else:
            await SimpleMenu(pages).start(ctx)

    @tpl.command(name="create")
    async def tpl_create(self, ctx: commands.Context, *, name: str):
        """Create a new Tidal playlist."""
        if not await self._check_ready(ctx):
            return
        pl = await self.tidal.create_user_playlist(name)
        if pl:
            await ctx.send(embed=_success_embed(f"Created playlist: **{truncate(name, 60)}**"))
        else:
            await ctx.send(embed=_error_embed(Messages.ERROR_PLAYLIST_WRITE_FAILED))

    @tpl.command(name="add")
    async def tpl_add(self, ctx: commands.Context, playlist_id: str, *, query: str):
        """Add a track (by search or ISRC) to one of your playlists."""
        if not await self._check_ready(ctx):
            return
        pl = await self.tidal.get_user_playlist_by_id(playlist_id)
        if not pl:
            await ctx.send(embed=_error_embed(Messages.ERROR_NOT_USER_PLAYLIST))
            return
        track = None
        if ISRC_PATTERN.match(query):
            isrc = ISRC_PATTERN.match(query).group(1).upper()
            track = await self.tidal.get_track_by_isrc(isrc)
        if not track:
            results = await self.tidal.search(query)
            if results:
                track = results[0]
        if not track:
            await ctx.send(embed=_error_embed(Messages.ERROR_NO_TRACKS_FOUND))
            return
        track_id = getattr(track, "id", None)
        if not track_id:
            await ctx.send(embed=_error_embed(Messages.ERROR_NO_TRACKS_FOUND))
            return
        ok = await self.tidal.add_track_to_playlist(pl, track_id)
        if ok:
            name = getattr(track, "name", str(track_id))
            await ctx.send(embed=_success_embed(f"Added **{truncate(name, 60)}** to playlist."))
        else:
            await ctx.send(embed=_error_embed(Messages.ERROR_PLAYLIST_WRITE_FAILED))

    @tpl.command(name="remove")
    async def tpl_remove(self, ctx: commands.Context, playlist_id: str, track_id: int):
        """Remove a track by ID from one of your playlists."""
        if not await self._check_ready(ctx):
            return
        pl = await self.tidal.get_user_playlist_by_id(playlist_id)
        if not pl:
            await ctx.send(embed=_error_embed(Messages.ERROR_NOT_USER_PLAYLIST))
            return
        ok = await self.tidal.remove_track_from_playlist(pl, track_id)
        if ok:
            await ctx.send(embed=_success_embed(f"Removed track `{track_id}` from playlist."))
        else:
            await ctx.send(embed=_error_embed(Messages.ERROR_PLAYLIST_WRITE_FAILED))

    @tpl.command(name="play")
    async def tpl_play(self, ctx: commands.Context, playlist_id: str):
        """Queue one of your Tidal playlists."""
        if not await self._check_ready(ctx):
            return
        pl = await self.tidal.get_user_playlist_by_id(playlist_id)
        if not pl:
            await ctx.send(embed=_error_embed(Messages.ERROR_NOT_USER_PLAYLIST))
            return
        tracks = await self.tidal.get_items(pl)
        await self._process_track_list(ctx, tracks, getattr(pl, "name", playlist_id), lambda t: t, COLOR_TEAL)

    @commands.group(name="tidalsetup")
    @commands.is_owner()
    async def tidalsetup(self, ctx: commands.Context):
        """Tidal OAuth setup commands (bot owner only)."""

    @tidalsetup.command(name="login")
    @commands.is_owner()
    async def tidalsetup_login(self, ctx: commands.Context):
        """Start the Tidal device-code OAuth flow."""
        if not TIDALAPI_AVAILABLE:
            await ctx.send(embed=_error_embed(Messages.ERROR_NO_TIDALAPI))
            return
        try:
            login_url, future = self.tidal.session.login_oauth()
            await ctx.author.send(
                f"Open this URL to authenticate with Tidal:\n<{login_url.verification_uri_complete}>\n"
                f"You have {login_url.expires_in} seconds."
            )
            await ctx.send(embed=_success_embed("Check your DMs for the Tidal login link."))
            await self.tidal._run_blocking(lambda: future.result(), timeout=120.0)
            def _get_state():
                return (
                    self.tidal.session.expiry_time,
                    self.tidal.session.token_type,
                    self.tidal.session.access_token,
                    self.tidal.session.refresh_token,
                )
            expiry_time, token_type, access, refresh = await self.tidal._run_blocking(_get_state, timeout=5.0)
            await asyncio.gather(
                self.config.token_type.set(token_type),
                self.config.access_token.set(access),
                self.config.refresh_token.set(refresh),
                self.config.expiry_time.set(int(expiry_time.timestamp()) if expiry_time else None),
            )
            self.tidal.invalidate_login_cache()
            await ctx.send(embed=_success_embed("Tidal authentication successful!"))
        except asyncio.TimeoutError:
            await ctx.send(embed=_error_embed("Authentication timed out. Please try again."))
        except Exception as e:
            log.error(f"Tidal OAuth login failed: {e}")
            await ctx.send(embed=_error_embed("Authentication failed. Check logs for details."))

    @tidalsetup.command(name="logout")
    @commands.is_owner()
    async def tidalsetup_logout(self, ctx: commands.Context):
        """Clear stored Tidal tokens."""
        await asyncio.gather(
            self.config.token_type.set(None),
            self.config.access_token.set(None),
            self.config.refresh_token.set(None),
            self.config.expiry_time.set(None),
        )
        self.tidal.invalidate_login_cache()
        await ctx.send(embed=_success_embed(Messages.SUCCESS_TOKENS_CLEARED))

    @tidalsetup.command(name="status")
    @commands.is_owner()
    async def tidalsetup_status(self, ctx: commands.Context):
        """Check Tidal authentication status."""
        logged_in = await self.tidal.is_logged_in()
        if logged_in:
            await ctx.send(embed=_success_embed("Tidal session is active."))
        else:
            await ctx.send(embed=_error_embed("Not authenticated. Use `[p]tidalsetup login`."))


async def setup(bot: Red) -> None:
    await bot.add_cog(TidalPlayer(bot))
