"""
TidalPlayer - Tidal music integration for Red Discord Bot
Features: Hi-Res Audio, Album Art, Spotify/YT Importing, MixV2, Video URLs,
          Hybrid Slash Commands, Similar Albums, UserPlaylist Mgmt, Rich UI
"""

import asyncio
import importlib.metadata
import logging
import re
from collections import defaultdict
from datetime import datetime, timedelta
from itertools import islice
from typing import Any, Callable, Coroutine, Dict, List, Optional, Set, Tuple, TypedDict

import discord
from redbot.core import Config, commands
from redbot.core.bot import Red
from redbot.core.utils.menus import SimpleMenu

# --- Dependency Checks ---
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
    try:
        from tidalapi.playlist import UserPlaylist as TidalUserPlaylist
        TIDAL_USER_PLAYLIST_AVAILABLE = True
    except ImportError:
        TidalUserPlaylist = None
        TIDAL_USER_PLAYLIST_AVAILABLE = False
    TIDALAPI_AVAILABLE = True
except ImportError:
    TidalTrack = None
    TidalUserPlaylist = None
    TIDALAPI_AVAILABLE = False
    TIDAL_MODELS_AVAILABLE = False
    TIDAL_USER_PLAYLIST_AVAILABLE = False

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

# --- Constants ---
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
SEARCH_BATCH_SIZE = 5  # P25: concurrent Tidal searches per batch

EMOJI_OK = "\u2705"
EMOJI_NO = "\u274c"
EMOJI_WARN = "\u26a0\ufe0f"
EMOJI_LOADING = "\u23f3"

# P16: Cached Color constants
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

# P9/P21: Pre-compiled filter regex — single pass instead of O(k*n) any() loop
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


class Messages:
    ERROR_NO_TIDALAPI = "tidalapi not installed. Run: `[p]pipinstall tidalapi`"
    ERROR_NOT_AUTHENTICATED = "Not authenticated. Run: `[p]tidalsetup`"
    ERROR_NO_AUDIO_COG = "Audio cog not loaded. Run: `[p]load audio`"
    ERROR_NO_PLAYER = "No active player. Join a voice channel first."
    ERROR_NO_TRACKS_FOUND = "No tracks found."
    ERROR_INVALID_URL = "Invalid {platform} {content_type} URL"
    ERROR_CONTENT_UNAVAILABLE = "Content unavailable (private/region-locked)"
    ERROR_LAVALINK_FAILED = "Playback failed: Could not retrieve Tidal stream."
    ERROR_STILL_LOADING = "\u23f3 TidalPlayer is still initializing, please wait a moment."
    ERROR_NOT_PLAYING = "Nothing is currently playing."
    ERROR_OWNER_ONLY = "\u26a0\ufe0f This command modifies the bot account\u2019s Tidal playlists. Owner only."

    STATUS_PLAYING = "Playing from Tidal"
    PROGRESS_QUEUEING = "Queueing {name} ({count} tracks)..."
    STATUS_STOPPING = "Stopping playlist queueing..."

    SUCCESS_TIDAL_SETUP = "Tidal setup complete!"
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
    ERROR_NO_SPOTIFY = "Spotify not configured. Run: `[p]tidalplay spotify <id> <secret>`"
    ERROR_NO_YOUTUBE = "YouTube not configured. Run: `[p]tidalplay youtube <key>`"
    ERROR_NOT_USER_PLAYLIST = "That playlist is not a user-owned playlist. Use `[p]tpl list` to see your playlists."
    ERROR_PLAYLIST_WRITE_FAILED = "Playlist operation failed."
    ERROR_NO_QUEUE = "The queue is empty."


# P38: skip redundant len check when limit is large
def truncate(text: str, limit: int) -> str:
    if len(text) > limit:
        return text[:limit - 3] + "..."
    return text


def make_tidal_url(content_type: str, content_id: Any) -> str:
    """
    Build a listen.tidal.com URL — opens directly in the Tidal web player
    without requiring a login redirect (unlike tidal.com/browse/...).
    """
    return f"https://listen.tidal.com/{content_type}/{content_id}"


def _is_tidal_track(obj: Any) -> bool:
    """Safely check if obj is a Tidal track/video."""
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


# ---------------------------------------------------------------------------
# Interactive track selection
# ---------------------------------------------------------------------------

class TrackSelectView(discord.ui.View):
    """Button-based track picker."""

    def __init__(self, tracks: List[Any], author: discord.User, timeout: float = 30.0):
        super().__init__(timeout=timeout)
        self.tracks = tracks[:5]
        self.author = author
        self.selected: Optional[Any] = None
        self._event = asyncio.Event()
        self._timed_out = False

        # P3: All track buttons row=0 (max 5/row), cancel row=1
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
            btn.callback = self._make_callback(i)
            self.add_item(btn)

        cancel_btn = discord.ui.Button(
            label="Cancel",
            style=discord.ButtonStyle.danger,
            custom_id="cancel",
            row=1,
        )
        cancel_btn.callback = self._make_cancel_callback()
        self.add_item(cancel_btn)

    # P40: disable all buttons immediately on any interaction to prevent double-clicks
    def _disable_all(self) -> None:
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True

    def _make_callback(self, index: int):
        async def callback(interaction: discord.Interaction):
            if interaction.user.id != self.author.id:
                await interaction.response.send_message("Not your selection.", ephemeral=True)
                return
            self.selected = self.tracks[index]
            self._disable_all()
            self._event.set()
            self.stop()
            await interaction.response.defer()
        return callback

    def _make_cancel_callback(self):
        async def callback(interaction: discord.Interaction):
            if interaction.user.id != self.author.id:
                await interaction.response.send_message("Not your selection.", ephemeral=True)
                return
            self.selected = None
            self._disable_all()
            self._event.set()
            self.stop()
            await interaction.response.defer()
        return callback

    async def wait_for_selection(self) -> Optional[Any]:
        await self._event.wait()
        return self.selected

    async def on_timeout(self):
        self._timed_out = True
        self._event.set()


# ---------------------------------------------------------------------------
# TidalHandler
# ---------------------------------------------------------------------------

class TidalHandler:
    """Handles low-level Tidal API interactions safely."""

    __slots__ = (
        "bot", "config", "session", "_refresh_task", "api_semaphore",
        "_login_cache", "_login_cache_time"
    )

    def __init__(self, bot: Red, config: Config):
        self.bot = bot
        self.config = config
        self.session: Optional[Any] = tidalapi.Session() if TIDALAPI_AVAILABLE else None
        self._refresh_task: Optional[asyncio.Task] = None
        self.api_semaphore = asyncio.Semaphore(API_SEMAPHORE_LIMIT)
        self._login_cache: Optional[bool] = None
        self._login_cache_time: float = 0.0

    @staticmethod
    async def _run_blocking(func: Callable[[], Any], timeout: float = 10.0) -> Any:
        return await asyncio.wait_for(
            asyncio.get_running_loop().run_in_executor(None, func),
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
                # P20: check .status attr first before string ops
                status = getattr(e, "status", None) or getattr(e, "status_code", None)
                if status == 429:
                    is_ratelimit = True
                else:
                    exc_type = type(e).__name__.lower()
                    err_str = str(e).lower()
                    is_ratelimit = (
                        "429" in err_str or "too many requests" in err_str
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
            expiry = (
                datetime.fromtimestamp(creds["expiry_time"])
                if creds.get("expiry_time") else None
            )
            def _load() -> None:
                self.session.load_oauth_session(
                    creds["token_type"], creds["access_token"],
                    creds["refresh_token"], expiry,
                )
            await self._run_blocking(_load, timeout=15.0)
            self._login_cache = True
            self._login_cache_time = asyncio.get_running_loop().time()
            log.info("Tidal session loaded successfully")
        except asyncio.TimeoutError:
            log.warning("Timed out loading Tidal session from stored credentials")
        except Exception as e:
            log.warning(f"Failed to load Tidal session: {e}")

    def start_refresh_loop(self) -> None:
        if self._refresh_task:
            self._refresh_task.cancel()
        self._refresh_task = asyncio.create_task(self._auto_refresh_tokens())

    def unload(self) -> None:
        if self._refresh_task:
            self._refresh_task.cancel()

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
                # P8: reuse result, avoid double network call
                if await self.is_logged_in():
                    expiry_time = await self._run_blocking(lambda: self.session.expiry_time, timeout=5.0)
                    if expiry_time:
                        until_expiry = (expiry_time - datetime.now()).total_seconds()
                        sleep_secs = max(60, until_expiry - 7200)
            except Exception:
                pass
            await asyncio.sleep(sleep_secs)
            try:
                if not await self.is_logged_in():
                    continue
                expiry_time = await self._run_blocking(lambda: self.session.expiry_time, timeout=5.0)
                if not expiry_time or datetime.now() + timedelta(hours=2) <= expiry_time:
                    continue
                log.info("Refreshing Tidal tokens...")
                try:
                    if hasattr(self.session, "request") and hasattr(self.session.request, "refresh_token"):
                        await self._run_blocking(self.session.request.refresh_token, timeout=15.0)
                        log.info("Token refreshed via request.refresh_token")
                except Exception as e:
                    log.warning(f"Token refresh call failed: {e}")
                def _get_state():
                    return (self.session.expiry_time, self.session.token_type,
                            self.session.access_token, self.session.refresh_token)
                expiry_time, token_type, access, refresh = await self._run_blocking(_get_state, timeout=5.0)
                await asyncio.gather(
                    self.config.token_type.set(token_type),
                    self.config.access_token.set(access),
                    self.config.refresh_token.set(refresh),
                    self.config.expiry_time.set(int(expiry_time.timestamp()) if expiry_time else None),
                )
                self._login_cache = True
                self._login_cache_time = asyncio.get_running_loop().time()
            except Exception as e:
                log.error(f"Token refresh failed: {e}")

    async def search(self, query: str, filter_remixes: bool = False) -> List[Any]:
        if not self.session:
            return []
        async with self.api_semaphore:
            try:
                def run_search():
                    if TIDAL_MODELS_AVAILABLE and TidalTrack is not None:
                        return self.session.search(query, models=[TidalTrack])
                    return self.session.search(query)
                result = await self._run_with_backoff(run_search, timeout=10.0)
                tracks = self._extract_tracks(result)
                return self._filter_tracks(tracks) if filter_remixes else tracks
            except asyncio.TimeoutError:
                log.warning(f"Tidal search timeout for '{query}'")
                return []
            except Exception as e:
                log.error(f"Search failed for '{query}': {e}")
                return []

    async def get_track_by_isrc(self, isrc: str) -> Optional[Any]:
        if not self.session:
            return None
        async with self.api_semaphore:
            try:
                def _fetch():
                    if hasattr(self.session, "get_tracks_by_isrc"):
                        results = self.session.get_tracks_by_isrc(isrc)
                        return results[0] if results else None
                    return None
                return await self._run_with_backoff(_fetch, timeout=10.0)
            except Exception as e:
                log.debug(f"ISRC lookup failed for {isrc}: {e}")
                return None

    async def get_track(self, track_id: str) -> Optional[Any]:
        if not self.session:
            return None
        async with self.api_semaphore:
            try:
                return await self._run_with_backoff(lambda: self.session.track(track_id), timeout=10.0)
            except asyncio.TimeoutError:
                log.warning(f"Tidal get_track timeout for id {track_id}")
                return None
            except Exception as e:
                log.debug(f"Failed to fetch track {track_id}: {e}")
                return None

    async def get_video(self, video_id: str) -> Optional[Any]:
        if not self.session or not hasattr(self.session, "video"):
            return None
        async with self.api_semaphore:
            try:
                return await self._run_with_backoff(lambda: self.session.video(video_id), timeout=10.0)
            except Exception as e:
                log.debug(f"Failed to fetch video {video_id}: {e}")
                return None

    async def get_album(self, album_id: str) -> Optional[Any]:
        if not self.session:
            return None
        async with self.api_semaphore:
            try:
                return await self._run_with_backoff(lambda: self.session.album(album_id), timeout=10.0)
            except Exception:
                return None

    async def get_playlist(self, playlist_id: str) -> Optional[Any]:
        if not self.session:
            return None
        async with self.api_semaphore:
            try:
                return await self._run_with_backoff(lambda: self.session.playlist(playlist_id), timeout=10.0)
            except Exception:
                return None

    async def get_mix(self, mix_id: str) -> Optional[Any]:
        if not self.session:
            return None
        # P7: separate semaphore blocks so failed mix_v2 doesn't hold slot during fallback
        if hasattr(self.session, "mix_v2"):
            async with self.api_semaphore:
                try:
                    result = await self._run_with_backoff(lambda: self.session.mix_v2(mix_id), timeout=10.0)
                    if result:
                        return result
                except Exception:
                    pass
        if hasattr(self.session, "mix"):
            async with self.api_semaphore:
                try:
                    return await self._run_with_backoff(lambda: self.session.mix(mix_id), timeout=10.0)
                except Exception:
                    pass
        return None

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
        # P30: remember sparse_album support to skip TypeError try/except on every page
        _sparse_supported: Optional[bool] = None
        while len(all_items) < MAX_ITEMS:
            # P6: yield at top of loop so other coroutines run between pages
            await asyncio.sleep(0)
            async with self.api_semaphore:
                try:
                    def _fetch(o=offset, sparse=_sparse_supported):
                        if sparse is False:
                            return list(container.items(limit=PAGINATION_LIMIT, offset=o))
                        try:
                            return list(container.items(limit=PAGINATION_LIMIT, offset=o, sparse_album=True))
                        except TypeError:
                            return list(container.items(limit=PAGINATION_LIMIT, offset=o))
                    chunk = await self._run_with_backoff(_fetch, timeout=25.0)
                    if _sparse_supported is None:
                        _sparse_supported = True
                except asyncio.TimeoutError:
                    log.error(f"Pagination timeout at offset {offset}")
                    break
                except Exception as e:
                    log.error(f"Pagination error at offset {offset}: {e}")
                    break
            if not chunk:
                break
            all_items.extend(chunk)
            if len(chunk) < PAGINATION_LIMIT:
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

    # P22: walrus operator avoids double getattr on result.tracks
    def _extract_tracks(self, result: Any) -> List[Any]:
        if (t := getattr(result, "tracks", None)) is not None:
            return t if isinstance(t, list) else getattr(t, "items", [])
        if isinstance(result, dict):
            t = result.get("tracks", [])
            return t if isinstance(t, list) else getattr(t, "items", [])
        return result if isinstance(result, list) else []

    # P21: FILTER_REGEX.search instead of O(k*n) any() loop
    def _filter_tracks(self, tracks: List[Any]) -> List[Any]:
        if not tracks:
            return []
        return [
            t for t in tracks
            if not FILTER_REGEX.search(getattr(t, "name", "") or "")
        ]


# ---------------------------------------------------------------------------
# TidalPlayer Cog
# ---------------------------------------------------------------------------

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
            token_type=None, access_token=None, refresh_token=None, expiry_time=None,
            spotify_client_id=None, spotify_client_secret=None, youtube_api_key=None,
            _schema_version=2,
        )
        self.config.register_guild(filter_remixes=True, interactive_search=False)

        self.tidal = TidalHandler(bot, self.config)
        self.sp: Optional[Any] = None
        self.yt: Optional[Any] = None
        self._tasks: Set[asyncio.Task] = set()
        # P13: defaultdict for lazy lock/event creation
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
            if version is None:
                await self.config._schema_version.set(2)
                log.info("TidalPlayer: config migrated to schema v2")
        except Exception as e:
            log.warning(f"Config migration check failed (non-fatal): {e}")

    def _create_task(self, coro: Coroutine[Any, Any, Any]) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        # P14: log unhandled exceptions from background tasks
        def _done_cb(t: asyncio.Task) -> None:
            self._tasks.discard(t)
            if not t.cancelled() and (exc := t.exception()):
                log.error(f"Background task raised: {exc}", exc_info=exc)
        task.add_done_callback(_done_cb)
        return task

    def _get_guild_lock(self, guild_id: int) -> asyncio.Lock:
        return self._guild_locks[guild_id]

    def _get_cancel_event(self, guild_id: int) -> asyncio.Event:
        return self._cancel_events[guild_id]

    def cog_unload(self) -> None:
        for ev in self._cancel_events.values():
            ev.set()
        # P32: cancel all background tasks concurrently
        tasks = list(self._tasks)
        for t in tasks:
            t.cancel()
        self.tidal.unload()
        self.sp = None
        self.yt = None
        self._guild_locks.clear()
        self._cancel_events.clear()
        self._current_meta.clear()
        log.info("TidalPlayer cog unloaded")

    @commands.Cog.listener()
    async def on_guild_remove(self, guild: discord.Guild) -> None:
        self._guild_locks.pop(guild.id, None)
        self._cancel_events.pop(guild.id, None)
        self._current_meta.pop(guild.id, None)
        # P34: also clear stale rate-limit timestamps for this guild
        self._last_progress_edit.pop(guild.id, None)

    @commands.Cog.listener()
    async def on_wavelink_track_end(self, player: Any, track: Any, reason: str) -> None:
        """Clear now-playing meta when a track finishes."""
        guild_id = getattr(player, "guild_id", None)
        # P12: use len() to safely check empty queue
        if guild_id and guild_id in self._current_meta:
            if not len(getattr(player, "queue", []) or []) and not getattr(player, "current", None):
                self._current_meta.pop(guild_id, None)

    async def _initialize_apis(self) -> None:
        # P35: log init time per service
        t0 = asyncio.get_running_loop().time()
        creds = await self.config.all()
        results = await asyncio.gather(
            self.tidal.initialize(creds),
            self._initialize_spotify(creds),
            self._initialize_youtube(creds),
            return_exceptions=True,
        )
        service_names = ["Tidal", "Spotify", "YouTube"]
        for name, r in zip(service_names, results):
            if isinstance(r, Exception):
                log.error(f"{name} init error: {r}")
        elapsed = asyncio.get_running_loop().time() - t0
        self._initialized = True
        self.tidal.start_refresh_loop()
        log.info(f"TidalPlayer fully initialized in {elapsed:.2f}s")

    async def _initialize_spotify(self, creds: Dict[str, Any]) -> None:
        if not SPOTIFY_AVAILABLE:
            return
        cid = creds.get("spotify_client_id")
        csec = creds.get("spotify_client_secret")
        if cid and csec:
            try:
                def _build():
                    return spotipy.Spotify(client_credentials_manager=SpotifyClientCredentials(cid, csec))
                self.sp = await TidalHandler._run_blocking(_build, timeout=15.0)
            except asyncio.TimeoutError:
                log.error("Spotify init timed out")
            except Exception as e:
                log.error(f"Spotify init failed: {e}")

    async def _initialize_youtube(self, creds: Dict[str, Any]) -> None:
        if not YOUTUBE_API_AVAILABLE:
            return
        key = creds.get("youtube_api_key")
        if key:
            try:
                self.yt = await TidalHandler._run_blocking(
                    lambda: build("youtube", "v3", developerKey=key), timeout=15.0
                )
            except asyncio.TimeoutError:
                log.error("YouTube API init timed out")
            except Exception as e:
                log.error(f"YouTube init failed: {e}")

    # --- Core Logic ---

    async def _extract_meta(self, track: Any, skip_audio_res: bool = False) -> TrackMeta:
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
            "title": name, "artist": artist, "album": album,
            "duration": duration, "quality": quality,
            "image": None, "share_url": share_url,
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

        if quality == "HI_RES_LOSSLESS" and album_obj and not skip_audio_res:
            res = await self.tidal.get_audio_resolution(album_obj)
            if res:
                bit_depth, sample_rate = res
                khz = sample_rate // 1000 if sample_rate >= 1000 else sample_rate
                meta["audio_resolution"] = f"HI-RES LOSSLESS ({bit_depth}-bit / {khz}kHz)"

        return meta

    def _format_duration(self, seconds: int) -> str:
        # P17: two divmod calls, no repeated division
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

    async def _load_and_queue_track(
        self, ctx: commands.Context, tidal_track: Any,
        show_embed: bool = True, skip_audio_res: bool = False
    ) -> bool:
        # P28: guard against DM context (no guild)
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
        loaded_track.author = (
            f"{meta['artist']} - {meta['album']}" if meta.get("album") else meta["artist"]
        )
        player.add(ctx.author, loaded_track)
        if not player.current:
            await player.play()

        self._current_meta[ctx.guild.id] = meta

        if show_embed:
            await self._send_now_playing(ctx, meta)
        return True

    # P23: skip join step for album field when not present
    def _build_now_playing_embed(self, meta: TrackMeta) -> discord.Embed:
        desc_parts = [f"**{meta['title']}**", meta["artist"]]
        if meta.get("album"):
            desc_parts.append(f"_{meta['album']}_")
        embed = discord.Embed(
            title=Messages.STATUS_PLAYING,
            description="\n".join(desc_parts),
            color=COLOR_BLUE,
        )
        quality_display = (
            meta.get("audio_resolution")
            or QUALITY_LABELS.get(meta["quality"], meta["quality"])
        )
        embed.add_field(name="Quality", value=quality_display, inline=True)
        if meta.get("share_url"):
            embed.add_field(name="Open in TIDAL", value=f"[Listen]({meta['share_url']})", inline=True)
        embed.set_footer(text=f"Duration: {self._format_duration(meta['duration'])}")
        if meta.get("image"):
            embed.set_thumbnail(url=meta["image"])
        return embed

    async def _send_now_playing(self, ctx: commands.Context, meta: TrackMeta) -> None:
        await ctx.send(embed=self._build_now_playing_embed(meta))

    # P26: pre-fetch all attrs in one pass, no repeated getattr per field
    async def _interactive_select(
        self, ctx: commands.Context, tracks: List[Any]
    ) -> Optional[Any]:
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

        embed = discord.Embed(
            title="Select a Track",
            description="\n".join(desc),
            color=COLOR_BLUE,
        )
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
        now = asyncio.get_running_loop().time()
        if now - self._last_progress_edit.get(msg.id, 0.0) < PROGRESS_EDIT_RATELIMIT:
            return
        try:
            await msg.edit(embed=embed)
            self._last_progress_edit[msg.id] = now
        except Exception:
            pass

    async def _fetch_all_spotify_tracks(self, playlist_id: str) -> List[Any]:
        all_items: List[Any] = []
        offset = 0
        limit = 100
        while len(all_items) < MAX_ITEMS:
            # P31: fields= cuts payload by ~80%
            resp = await TidalHandler._run_blocking(
                lambda o=offset: self.sp.playlist_tracks(
                    playlist_id, limit=limit, offset=o,
                    fields="items(track(name,artists(name),external_ids)),next"
                ),
                timeout=20.0,
            )
            items = resp.get("items", [])
            all_items.extend(i for i in items if i.get("track"))
            if not resp.get("next"):
                break
            offset += limit
            await asyncio.sleep(0)
        return all_items[:MAX_ITEMS]

    async def _fetch_all_spotify_album_tracks(self, album_id: str) -> Tuple[List[Any], str]:
        all_items: List[Any] = []
        album_name = album_id
        try:
            alb = await TidalHandler._run_blocking(lambda: self.sp.album(album_id), timeout=15.0)
            album_name = alb.get("name", album_id)
            tracks = alb.get("tracks", {})
            items = tracks.get("items", [])
            all_items.extend(items)
            next_url = tracks.get("next")
            while next_url and len(all_items) < MAX_ITEMS:
                resp = await TidalHandler._run_blocking(
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
            req = self.yt.playlistItems().list(**kwargs)
            resp = await TidalHandler._run_blocking(req.execute, timeout=20.0)
            for item in resp.get("items", []):
                title = item.get("snippet", {}).get("title", "").lower()
                if title not in YOUTUBE_SKIP_TITLES:
                    all_items.append(item)
            page_token = resp.get("nextPageToken")
            if not page_token or len(all_items) >= MAX_ITEMS:
                break
            await asyncio.sleep(0)
        return all_items[:MAX_ITEMS]

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

        lock = self._get_guild_lock(ctx.guild.id)
        if lock.locked():
            await ctx.send(embed=_error_embed("Already processing a playlist in this server."))
            return

        filter_remixes = await self.config.guild(ctx.guild).filter_remixes()
        player = await self._ensure_player(ctx)
        if not player:
            return

        cancel_event = self._get_cancel_event(ctx.guild.id)
        # P15: precompute truncated name once, not on every progress update
        trunc_name = truncate(name, 50)
        total = len(items)

        async with lock:
            # P11: pmsg initialised to None so finally block is safe
            pmsg = None
            initial_embed = discord.Embed(
                title=Messages.PROGRESS_QUEUEING.format(name=trunc_name, count=total),
                color=color,
            )
            if thumbnail_url:
                initial_embed.set_thumbnail(url=thumbnail_url)
            pmsg = await ctx.send(embed=initial_embed)
            queued, skipped, last_up = 0, 0, 0

            try:
                for i, item in enumerate(items, 1):
                    # P39: cancel check before any async work
                    if cancel_event.is_set():
                        break

                    if not getattr(player, "is_connected", True):
                        reconnected = False
                        for attempt in range(VC_RECONNECT_RETRIES):
                            await asyncio.sleep(VC_RECONNECT_DELAY)
                            new_player = await self._get_player(ctx, connect=True)
                            if new_player and getattr(new_player, "is_connected", False):
                                player = new_player
                                reconnected = True
                                log.info(f"Reconnected to VC after disconnect (attempt {attempt + 1})")
                                break
                        if not reconnected:
                            log.warning("Could not reconnect to VC, stopping queue")
                            break

                    query = item_processor(item)
                    success = False

                    if query and _is_tidal_track(query):
                        success = await self._load_and_queue_track(
                            ctx, query, show_embed=False, skip_audio_res=True
                        )
                    elif query:
                        tracks = await self.tidal.search(query, filter_remixes=filter_remixes)
                        if tracks:
                            success = await self._load_and_queue_track(
                                ctx, tracks[0], show_embed=False, skip_audio_res=True
                            )

                    queued += success
                    skipped += not success

                    if i - last_up >= BATCH_UPDATE_INTERVAL or i == total:
                        embed = discord.Embed(
                            title=Messages.PROGRESS_QUEUEING.format(name=trunc_name, count=total),
                            description=Messages.SUCCESS_PARTIAL_QUEUE.format(
                                queued=queued, total=total, skipped=skipped
                            ),
                            color=color,
                        )
                        if thumbnail_url:
                            embed.set_thumbnail(url=thumbnail_url)
                        await self._edit_progress_message(pmsg, embed)
                        last_up = i
                        await asyncio.sleep(0)

                final_embed = discord.Embed(
                    title=Messages.SUCCESS_PARTIAL_QUEUE.format(
                        queued=queued, total=total, skipped=skipped
                    ),
                    description=f"Source: {truncate(name, 100)}",
                    color=color,
                )
                if thumbnail_url:
                    final_embed.set_thumbnail(url=thumbnail_url)
                try:
                    await pmsg.edit(embed=final_embed)
                    self._last_progress_edit[pmsg.id] = asyncio.get_running_loop().time()
                except Exception:
                    pass

            except Exception as e:
                log.error(f"Queue processing error: {e}")
                try:
                    if pmsg:
                        await pmsg.edit(embed=_error_embed(Messages.ERROR_FETCH_FAILED))
                except Exception:
                    pass
            finally:
                cancel_event.clear()
                if pmsg:
                    self._last_progress_edit.pop(pmsg.id, None)

    # P33: single is_logged_in call, result stored
    async def _check_ready(self, ctx: commands.Context) -> bool:
        if not self._initialized:
            await ctx.send(embed=_error_embed(Messages.ERROR_STILL_LOADING))
            return False
        if not TIDALAPI_AVAILABLE:
            await ctx.send(embed=_error_embed(Messages.ERROR_NO_TIDALAPI))
            return False
        logged_in = await self.tidal.is_logged_in()
        if not logged_in:
            await ctx.send(embed=_error_embed(Messages.ERROR_NOT_AUTHENTICATED))
            return False
        if not LAVALINK_AVAILABLE:
            await ctx.send(embed=_error_embed(Messages.ERROR_NO_AUDIO_COG))
            return False
        return True

    # --- URL Handlers ---

    # P19: early return on first match
    async def _handle_tidal_url(self, ctx: commands.Context, url: str) -> None:
        for k, p in TIDAL_URL_PATTERNS.items():
            if m := p.search(url):
                func = getattr(self, f"_handle_{k}", None)
                if func:
                    await func(ctx, m.group(1))
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

    # P37: parallelize get_album and thumbnail fetch
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

        tracks, thumb = await asyncio.gather(
            self.tidal.get_items(alb),
            _get_thumb(),
        )
        await self._process_track_list(
            ctx, tracks, getattr(alb, "name", aid), lambda t: t,
            thumbnail_url=thumb,
        )

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

    # =========================================================================
    # Commands
    # =========================================================================

    @commands.guild_only()
    @commands.cooldown(1, 5, commands.BucketType.user)
    @commands.hybrid_command(name="tplay")
    async def tplay(self, ctx: commands.Context, *, query: str) -> None:
        """
        Play from Tidal: URL, Spotify/YouTube playlist, ISRC, or search query.

        Examples:
          >tplay https://tidal.com/browse/track/12345
          >tplay isrc:USRC12345678
          >tplay https://open.spotify.com/track/...
          >tplay Daft Punk - Get Lucky
        """
        if not await self._check_ready(ctx):
            return

        if "tidal.com" in query:
            await self._handle_tidal_url(ctx, query)
            return

        if isrc_match := ISRC_PATTERN.match(query.strip()):
            track = await self.tidal.get_track_by_isrc(isrc_match.group(1).upper())
            if track:
                await self._load_and_queue_track(ctx, track)
            else:
                await ctx.send(embed=_error_embed(Messages.ERROR_NO_TRACKS_FOUND))
            return

        # Spotify single track
        if m := SPOTIFY_TRACK_PATTERN.search(query):
            if not (SPOTIFY_AVAILABLE and self.sp):
                await ctx.send(embed=_error_embed(Messages.ERROR_NO_SPOTIFY))
                return
            try:
                sp_track = await TidalHandler._run_blocking(lambda: self.sp.track(m.group(1)), timeout=15.0)
                search_q = f"{sp_track['name']} {sp_tra