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

EMOJI_OK = "\u2705"
EMOJI_NO = "\u274c"
EMOJI_WARN = "\u26a0\ufe0f"
EMOJI_LOADING = "\u23f3"

# P16: Cached Color constants — avoids allocating new Color objects on every embed
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

# P9: Pre-compiled filter regex — single pass through title instead of O(k*n) any() loop
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


def truncate(text: str, limit: int) -> str:
    # P17-adjacent: precompute cut index once
    cut = limit - 3
    return f"{text[:cut]}..." if len(text) > limit else text


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
    """Build a consistent red error embed."""
    return discord.Embed(description=f"{EMOJI_NO} {message}", color=COLOR_RED)


def _success_embed(message: str) -> discord.Embed:
    """Build a consistent green success embed."""
    return discord.Embed(description=f"{EMOJI_OK} {message}", color=COLOR_GREEN)


# ---------------------------------------------------------------------------
# Interactive track selection using discord.ui.View (buttons)
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

        # P3: All track buttons on row=0 (up to 5 per row), cancel on row=1
        # Previously each button had its own row which caused a Discord API crash
        for i, track in enumerate(self.tracks):
            name = getattr(track, "full_name", None) or getattr(track, "name", f"Track {i+1}")
            artist = getattr(getattr(track, "artist", None), "name", "")
            raw_label = f"{artist} \u2014 {name}" if artist else name
            label = truncate(raw_label, 80)
            btn = discord.ui.Button(
                label=label,
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

    def _make_callback(self, index: int):
        async def callback(interaction: discord.Interaction):
            if interaction.user.id != self.author.id:
                await interaction.response.send_message("Not your selection.", ephemeral=True)
                return
            self.selected = self.tracks[index]
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
                # P20: Check status attribute first (fast path) before expensive string ops
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
                # P8: Capture is_logged_in result once, reuse below without a second network call
                logged_in = await self.is_logged_in()
                if logged_in:
                    expiry_time = await self._run_blocking(lambda: self.session.expiry_time, timeout=5.0)
                    if expiry_time:
                        until_expiry = (expiry_time - datetime.now()).total_seconds()
                        sleep_secs = max(60, until_expiry - 7200)
            except Exception:
                pass
            await asyncio.sleep(sleep_secs)
            try:
                # Reuse cached value — if cache hasn't expired yet we skip another check
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
        # P7: Separate semaphore blocks for mix_v2 and mix fallback
        # so a failed mix_v2 doesn't hold the slot during the retry gap
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
        """Create a new playlist on the bot's Tidal account."""
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
        # P30 (prep): detect sparse_album support once before the loop
        _sparse_supported: Optional[bool] = None
        while len(all_items) < MAX_ITEMS:
            # P6: yield at TOP of loop so other coroutines can run between pages
            await asyncio.sleep(0)
            async with self.api_semaphore:
                try:
                    def _fetch(o=offset, sparse=_sparse_supported):
                        if sparse is False:
                            return list(container.items(limit=PAGINATION_LIMIT, offset=o))
                        try:
                            result = list(container.items(limit=PAGINATION_LIMIT, offset=o, sparse_album=True))
                            return result
                        except TypeError:
                            return list(container.items(limit=PAGINATION_LIMIT, offset=o))
                    chunk = await self._run_with_backoff(_fetch, timeout=25.0)
                    # Record sparse support based on first page result (no exception = supported)
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
                urls = await self._run_wi