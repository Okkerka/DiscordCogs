"""
TidalPlayer - Tidal music integration for Red Discord Bot
Features: Hi-Res Audio, Album Art, Spotify/YT Importing, MixV2, Video URLs,
          Hybrid Slash Commands, Similar Albums, UserPlaylist Mgmt, Rich UI
"""

import asyncio
import importlib.metadata
import logging
import re
from datetime import datetime, timedelta
from typing import Any, Callable, Coroutine, Dict, List, Optional, Set, Tuple, TypedDict

import discord
from redbot.core import Config, commands
from redbot.core.bot import Red
from redbot.core.utils.chat_formatting import pagify
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

EMOJI_OK = "\u2705"
EMOJI_NO = "\u274c"
EMOJI_WARN = "\u26a0\ufe0f"
EMOJI_LOADING = "\u23f3"

REACTION_NUMBERS = ("1\ufe0f\u20e3", "2\ufe0f\u20e3", "3\ufe0f\u20e3", "4\ufe0f\u20e3", "5\ufe0f\u20e3")
CANCEL_EMOJI = "\u274c"

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
    return f"{text[:limit-3]}..." if len(text) > limit else text


def make_tidal_share_url(content_type: str, content_id: Any) -> str:
    """
    Build a tidal.com/browse/<type>/<id> share page URL.
    Opens the Tidal share page where users can choose app, Spotify, YouTube, etc.
    """
    return f"https://tidal.com/browse/{content_type}/{content_id}"


def _is_tidal_track(obj: Any) -> bool:
    """Safely check if obj is a Tidal track/video using isinstance when available."""
    if TIDAL_MODELS_AVAILABLE and TidalTrack is not None:
        return isinstance(obj, TidalTrack)
    return hasattr(obj, "id") and (hasattr(obj, "get_stream") or hasattr(obj, "get_url"))


# ---------------------------------------------------------------------------
# Interactive track selection using discord.ui.View (buttons, no reactions)
# ---------------------------------------------------------------------------

class TrackSelectView(discord.ui.View):
    """Button-based track picker — replaces reaction-based selection."""

    def __init__(self, tracks: List[Any], author: discord.User, timeout: float = 30.0):
        super().__init__(timeout=timeout)
        self.tracks = tracks[:5]
        self.author = author
        self.selected: Optional[Any] = None
        self._event = asyncio.Event()

        for i, track in enumerate(self.tracks):
            btn = discord.ui.Button(
                label=str(i + 1),
                style=discord.ButtonStyle.primary,
                custom_id=f"track_{i}",
            )
            btn.callback = self._make_callback(i)
            self.add_item(btn)

        cancel_btn = discord.ui.Button(
            label="Cancel",
            style=discord.ButtonStyle.danger,
            custom_id="cancel",
        )
        cancel_btn.callback = self._cancel_callback
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

    async def _cancel_callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.author.id:
            await interaction.response.send_message("Not your selection.", ephemeral=True)
            return
        self.selected = None
        self._event.set()
        self.stop()
        await interaction.response.defer()

    async def wait_for_selection(self) -> Optional[Any]:
        await self._event.wait()
        return self.selected

    async def on_timeout(self):
        self._event.set()


# ---------------------------------------------------------------------------
# TidalHandler — all low-level Tidal API calls
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
        """Execute blocking I/O in executor with timeout."""
        return await asyncio.wait_for(
            asyncio.get_running_loop().run_in_executor(None, func),
            timeout=timeout,
        )

    async def _run_with_backoff(self, func: Callable[[], Any], timeout: float = 10.0) -> Any:
        """
        Execute blocking I/O with exponential backoff on HTTP 429 / rate-limit errors.
        Checks specific exception types first, then falls back to string matching.
        """
        delay = RATELIMIT_BACKOFF_BASE
        last_exc: Optional[Exception] = None
        for attempt in range(RATELIMIT_MAX_RETRIES):
            try:
                return await self._run_blocking(func, timeout=timeout)
            except Exception as e:
                last_exc = e
                # Check specific tidalapi exception types first
                exc_type = type(e).__name__.lower()
                err_str = str(e).lower()
                is_ratelimit = (
                    "429" in err_str
                    or "too many requests" in err_str
                    or "rate limit" in err_str
                    or "ratelimit" in err_str
                    or "toomanyrequests" in exc_type
                    or "ratelimit" in exc_type
                )
                if is_ratelimit and attempt < RATELIMIT_MAX_RETRIES - 1:
                    wait = min(delay, RATELIMIT_BACKOFF_MAX)
                    log.warning(f"Rate limited by Tidal, retrying in {wait:.1f}s (attempt {attempt + 1})")
                    await asyncio.sleep(wait)
                    delay *= 2
                else:
                    raise
        raise last_exc  # unreachable but satisfies type checkers

    async def initialize(self, creds: Dict[str, Any]) -> None:
        """Load session from stored credentials."""
        if not self.session or not creds.get("access_token"):
            return
        try:
            expiry = (
                datetime.fromtimestamp(creds["expiry_time"])
                if creds.get("expiry_time") else None
            )

            def _load() -> None:
                self.session.load_oauth_session(
                    creds["token_type"],
                    creds["access_token"],
                    creds["refresh_token"],
                    expiry,
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
        if (
            self._login_cache is not None
            and (now - self._login_cache_time) < LOGIN_CACHE_TTL
        ):
            return self._login_cache
        for attempt in range(LOGIN_CHECK_RETRIES):
            try:
                result = bool(
                    await self._run_blocking(self.session.check_login, timeout=LOGIN_CHECK_TIMEOUT)
                )
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
        """Background task: refresh tokens, sleeping until close to actual expiry."""
        while True:
            # Sleep until 2 hours before expiry (or 1 hour if unknown)
            sleep_secs = 3600
            try:
                if await self.is_logged_in():
                    expiry_time = await self._run_blocking(
                        lambda: self.session.expiry_time, timeout=5.0
                    )
                    if expiry_time:
                        until_expiry = (expiry_time - datetime.now()).total_seconds()
                        sleep_secs = max(60, until_expiry - 7200)  # wake 2h before expiry
            except Exception:
                pass

            await asyncio.sleep(sleep_secs)

            try:
                if not await self.is_logged_in():
                    continue
                expiry_time = await self._run_blocking(
                    lambda: self.session.expiry_time, timeout=5.0
                )
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
                    return (
                        self.session.expiry_time,
                        self.session.token_type,
                        self.session.access_token,
                        self.session.refresh_token,
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
                return await self._run_with_backoff(
                    lambda: self.session.track(track_id), timeout=10.0
                )
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
                return await self._run_with_backoff(
                    lambda: self.session.video(video_id), timeout=10.0
                )
            except Exception as e:
                log.debug(f"Failed to fetch video {video_id}: {e}")
                return None

    async def get_album(self, album_id: str) -> Optional[Any]:
        if not self.session:
            return None
        async with self.api_semaphore:
            try:
                return await self._run_with_backoff(
                    lambda: self.session.album(album_id), timeout=10.0
                )
            except Exception:
                return None

    async def get_playlist(self, playlist_id: str) -> Optional[Any]:
        if not self.session:
            return None
        async with self.api_semaphore:
            try:
                return await self._run_with_backoff(
                    lambda: self.session.playlist(playlist_id), timeout=10.0
                )
            except Exception:
                return None

    async def get_mix(self, mix_id: str) -> Optional[Any]:
        if not self.session:
            return None
        async with self.api_semaphore:
            if hasattr(self.session, "mix_v2"):
                try:
                    result = await self._run_with_backoff(
                        lambda: self.session.mix_v2(mix_id), timeout=10.0
                    )
                    if result:
                        return result
                except Exception:
                    pass
            if hasattr(self.session, "mix"):
                try:
                    return await self._run_with_backoff(
                        lambda: self.session.mix(mix_id), timeout=10.0
                    )
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
        while len(all_items) < MAX_ITEMS:
            async with self.api_semaphore:
                try:
                    def _fetch(o=offset):
                        try:
                            return list(container.items(limit=PAGINATION_LIMIT, offset=o, sparse_album=True))
                        except TypeError:
                            return list(container.items(limit=PAGINATION_LIMIT, offset=o))
                    chunk = await self._run_with_backoff(_fetch, timeout=25.0)
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
            await asyncio.sleep(0)
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

            try:
                url = await self._run_with_backoff(track.get_url, timeout=10.0)
                if url:
                    return url
            except Exception as e:
                log.debug(f"get_url() failed for track {track_id}: {e}")

        if track_id:
            return make_tidal_share_url("track", track_id)
        return None

    def _extract_tracks(self, result: Any) -> List[Any]:
        if hasattr(result, "tracks"):
            t = result.tracks
            return t if isinstance(t, list) else getattr(t, "items", [])
        if isinstance(result, dict):
            t = result.get("tracks", [])
            return t if isinstance(t, list) else getattr(t, "items", [])
        return result if isinstance(result, list) else []

    def _filter_tracks(self, tracks: List[Any]) -> List[Any]:
        if not tracks:
            return []
        return [
            t for t in tracks
            if not any(
                kw in (getattr(t, "name", "") or "").lower()
                for kw in FILTER_KEYWORDS
            )
        ]


# ---------------------------------------------------------------------------
# TidalPlayer Cog
# ---------------------------------------------------------------------------

class TidalPlayer(commands.Cog):
    """Play music from Tidal with full metadata support."""

    __slots__ = (
        "bot", "config", "tidal", "sp", "yt", "_tasks", "_guild_locks",
        "_cancel_events", "_last_progress_edit", "_initialized",
        "_current_meta"  # guild_id -> TrackMeta of last played track
    )

    def __init__(self, bot: Red):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=COG_IDENTIFIER, force_registration=True)
        self.config.register_global(
            token_type=None,
            access_token=None,
            refresh_token=None,
            expiry_time=None,
            spotify_client_id=None,
            spotify_client_secret=None,
            youtube_api_key=None,
            _schema_version=2,
        )
        self.config.register_guild(filter_remixes=True, interactive_search=False)

        self.tidal = TidalHandler(bot, self.config)
        self.sp: Optional[Any] = None
        self.yt: Optional[Any] = None
        self._tasks: Set[asyncio.Task] = set()
        self._guild_locks: Dict[int, asyncio.Lock] = {}
        self._cancel_events: Dict[int, asyncio.Event] = {}
        self._last_progress_edit: Dict[int, float] = {}
        self._current_meta: Dict[int, TrackMeta] = {}
        self._initialized: bool = False

    async def cog_load(self) -> None:
        await self._migrate_config()
        await self._initialize_apis()

    async def _migrate_config(self) -> None:
        """Migrate config schema between versions. Logs any issues, never crashes."""
        try:
            version = await self.config._schema_version()
            if version is None:
                # v1 -> v2: no structural changes, just stamp the version
                await self.config._schema_version.set(2)
                log.info("TidalPlayer: config migrated to schema v2")
        except Exception as e:
            log.warning(f"Config migration check failed (non-fatal): {e}")

    def _create_task(self, coro: Coroutine[Any, Any, Any]) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def _get_guild_lock(self, guild_id: int) -> asyncio.Lock:
        if guild_id not in self._guild_locks:
            self._guild_locks[guild_id] = asyncio.Lock()
        return self._guild_locks[guild_id]

    def _get_cancel_event(self, guild_id: int) -> asyncio.Event:
        if guild_id not in self._cancel_events:
            self._cancel_events[guild_id] = asyncio.Event()
        return self._cancel_events[guild_id]

    def cog_unload(self) -> None:
        for ev in self._cancel_events.values():
            ev.set()
        for task in list(self._tasks):
            task.cancel()
        self.tidal.unload()
        self.sp = None
        self.yt = None
        # Clean up guild state
        self._guild_locks.clear()
        self._cancel_events.clear()
        self._current_meta.clear()
        log.info("TidalPlayer cog unloaded")

    @commands.Cog.listener()
    async def on_guild_remove(self, guild: discord.Guild) -> None:
        """Clean up per-guild state when bot leaves a guild."""
        self._guild_locks.pop(guild.id, None)
        self._cancel_events.pop(guild.id, None)
        self._current_meta.pop(guild.id, None)

    async def _initialize_apis(self) -> None:
        creds = await self.config.all()
        results = await asyncio.gather(
            self.tidal.initialize(creds),
            self._initialize_spotify(creds),
            self._initialize_youtube(creds),
            return_exceptions=True,
        )
        for r in results:
            if isinstance(r, Exception):
                log.error(f"API init error: {r}")
        self._initialized = True
        self.tidal.start_refresh_loop()
        log.info("TidalPlayer fully initialized")

    async def _initialize_spotify(self, creds: Dict[str, Any]) -> None:
        if not SPOTIFY_AVAILABLE:
            return
        cid = creds.get("spotify_client_id")
        csec = creds.get("spotify_client_secret")
        if cid and csec:
            try:
                def _build():
                    return spotipy.Spotify(
                        client_credentials_manager=SpotifyClientCredentials(cid, csec)
                    )
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

    async def _extract_meta(
        self, track: Any, skip_audio_res: bool = False
    ) -> TrackMeta:
        """
        Extract metadata from a Tidal track or video object.
        skip_audio_res=True skips the extra HTTP call for Hi-Res info
        (use during bulk queueing to avoid 50+ extra API calls per album).
        """
        full_name = getattr(track, "full_name", None)
        name = full_name or getattr(track, "name", "Unknown") or "Unknown"
        artist_obj = getattr(track, "artist", None)
        artist = getattr(artist_obj, "name", "Unknown") if artist_obj else "Unknown"
        album_obj = getattr(track, "album", None)
        album = getattr(album_obj, "name", None) if album_obj else None
        duration = int(getattr(track, "duration", 0) or 0)
        quality = getattr(track, "audio_quality", "LOSSLESS") or "LOSSLESS"
        track_id = getattr(track, "id", None)

        # Detect video correctly: video_quality is non-None on videos
        is_video = getattr(track, "video_quality", None) is not None
        content_type = "video" if is_video else "track"
        share_url = make_tidal_share_url(content_type, track_id) if track_id else None

        meta: TrackMeta = {
            "title": name,
            "artist": artist,
            "album": album,
            "duration": duration,
            "quality": quality,
            "image": None,
            "share_url": share_url,
            "audio_resolution": None,
            "track_id": track_id,
        }

        try:
            if album_obj and hasattr(album_obj, "image"):
                meta["image"] = album_obj.image(dimensions=640)
            elif album_obj and hasattr(album_obj, "cover") and album_obj.cover:
                uuid = album_obj.cover.replace("-", "/")
                meta["image"] = f"https://resources.tidal.com/images/{uuid}/640x640.jpg"
        except Exception:
            pass

        # Only fetch audio resolution for single track embeds, not bulk queue
        if quality == "HI_RES_LOSSLESS" and album_obj and not skip_audio_res:
            res = await self.tidal.get_audio_resolution(album_obj)
            if res:
                bit_depth, sample_rate = res
                khz = sample_rate // 1000 if sample_rate >= 1000 else sample_rate
                meta["audio_resolution"] = f"HI-RES LOSSLESS ({bit_depth}-bit / {khz}kHz)"

        return meta

    def _format_duration(self, seconds: int) -> str:
        m, s = divmod(seconds, 60)
        return f"{m // 60}:{m % 60:02d}:{s:02d}" if m >= 60 else f"{m:02d}:{s:02d}"

    async def _get_player(self, ctx: commands.Context, connect: bool = False) -> Optional[Any]:
        """Get the Lavalink player. Only attempts VC connection if connect=True."""
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
        """Get player and connect to VC if needed. Returns None and sends error if unavailable."""
        player = await self._get_player(ctx, connect=True)
        if not player:
            await ctx.send(Messages.ERROR_NO_PLAYER)
        return player

    async def _load_and_queue_track(
        self, ctx: commands.Context, tidal_track: Any, show_embed: bool = True,
        skip_audio_res: bool = False
    ) -> bool:
        meta = await self._extract_meta(tidal_track, skip_audio_res=skip_audio_res)
        player = await self._get_player(ctx, connect=True)
        if not player:
            await ctx.send(Messages.ERROR_NO_PLAYER)
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
            await ctx.send(Messages.ERROR_LAVALINK_FAILED)
            return False

        loaded_track.title = truncate(meta["title"], 100)
        loaded_track.author = (
            f"{meta['artist']} - {meta['album']}" if meta.get("album") else meta["artist"]
        )
        player.add(ctx.author, loaded_track)
        if not player.current:
            await player.play()

        # Store meta for >tnp
        self._current_meta[ctx.guild.id] = meta

        if show_embed:
            await self._send_now_playing(ctx, meta)
        return True

    def _build_now_playing_embed(self, meta: TrackMeta) -> discord.Embed:
        """Build the now-playing embed from a TrackMeta dict."""
        desc_parts = [f"**{meta['title']}**", meta["artist"]]
        if meta.get("album"):
            desc_parts.append(f"_{meta['album']}_")
        embed = discord.Embed(
            title=Messages.STATUS_PLAYING,
            description="\n".join(desc_parts),
            color=discord.Color.blue(),
        )
        quality_display = (
            meta.get("audio_resolution")
            or QUALITY_LABELS.get(meta["quality"], meta["quality"])
        )
        embed.add_field(name="Quality", value=quality_display, inline=True)
        if meta.get("share_url"):
            embed.add_field(
                name="Open in TIDAL",
                value=f"[Share Page]({meta['share_url']})",
                inline=True,
            )
        embed.set_footer(text=f"Duration: {self._format_duration(meta['duration'])}")
        if meta.get("image"):
            embed.set_thumbnail(url=meta["image"])
        return embed

    async def _send_now_playing(self, ctx: commands.Context, meta: TrackMeta) -> None:
        await ctx.send(embed=self._build_now_playing_embed(meta))

    async def _interactive_select(
        self, ctx: commands.Context, tracks: List[Any]
    ) -> Optional[Any]:
        """Show button-based track selection menu."""
        if not tracks:
            return None
        top = tracks[:5]
        desc = [
            f"**{i + 1}.** {getattr(t, 'full_name', None) or getattr(t, 'name', 'Unknown')} — "
            f"{getattr(getattr(t, 'artist', None), 'name', 'Unknown')}"
            for i, t in enumerate(top)
        ]
        embed = discord.Embed(
            title="Select a Track",
            description="\n".join(desc),
            color=discord.Color.blue(),
        )
        view = TrackSelectView(top, ctx.author, timeout=float(INTERACTIVE_TIMEOUT))
        msg = await ctx.send(embed=embed, view=view)
        selected = await view.wait_for_selection()
        try:
            await msg.delete()
        except Exception:
            pass
        if selected is None and not view.is_finished():
            await ctx.send(Messages.ERROR_TIMEOUT)
        return selected

    async def _edit_progress_message(
        self, msg: discord.Message, embed: discord.Embed
    ) -> None:
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
        while True:
            resp = await TidalHandler._run_blocking(
                lambda o=offset: self.sp.playlist_tracks(playlist_id, limit=limit, offset=o),
                timeout=20.0,
            )
            items = resp.get("items", [])
            all_items.extend(i for i in items if i.get("track"))
            if not resp.get("next"):
                break
            offset += limit
            await asyncio.sleep(0)
        return all_items

    async def _fetch_all_youtube_tracks(self, playlist_id: str) -> List[Any]:
        all_items: List[Any] = []
        page_token: Optional[str] = None
        while True:
            kwargs: Dict[str, Any] = {
                "part": "snippet",
                "playlistId": playlist_id,
                "maxResults": 50,
            }
            if page_token:
                kwargs["pageToken"] = page_token
            req = self.yt.playlistItems().list(**kwargs)
            resp = await TidalHandler._run_blocking(req.execute, timeout=20.0)
            # Filter deleted/private videos
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
    ) -> None:
        if not items:
            await ctx.send(Messages.ERROR_NO_TRACKS_FOUND)
            return

        lock = self._get_guild_lock(ctx.guild.id)
        if lock.locked():
            await ctx.send("Already processing a playlist in this server.")
            return

        filter_remixes = await self.config.guild(ctx.guild).filter_remixes()
        player = await self._ensure_player(ctx)
        if not player:
            return

        cancel_event = self._get_cancel_event(ctx.guild.id)

        async with lock:
            pmsg = await ctx.send(
                Messages.PROGRESS_QUEUEING.format(name=truncate(name, 50), count=len(items))
            )
            queued, skipped, last_up = 0, 0, 0
            total = len(items)

            try:
                for i, item in enumerate(items, 1):
                    if cancel_event.is_set():
                        break

                    # Graceful VC reconnect if disconnected mid-queue
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
                            title=Messages.PROGRESS_QUEUEING.format(
                                name=truncate(name, 50), count=total
                            ),
                            description=Messages.SUCCESS_PARTIAL_QUEUE.format(
                                queued=queued, total=total, skipped=skipped
                            ),
                            color=color,
                        )
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
                try:
                    await pmsg.edit(embed=final_embed)
                    self._last_progress_edit[pmsg.id] = asyncio.get_running_loop().time()
                except Exception:
                    pass

            except Exception as e:
                log.error(f"Queue processing error: {e}")
                try:
                    await pmsg.edit(content=Messages.ERROR_FETCH_FAILED)
                except Exception:
                    pass
            finally:
                cancel_event.clear()
                self._last_progress_edit.pop(pmsg.id, None)

    async def _check_ready(self, ctx: commands.Context) -> bool:
        """Check all dependencies are ready. Does NOT connect to VC."""
        if not self._initialized:
            await ctx.send(Messages.ERROR_STILL_LOADING)
            return False
        if not TIDALAPI_AVAILABLE:
            await ctx.send(Messages.ERROR_NO_TIDALAPI)
            return False
        if not await self.tidal.is_logged_in():
            await ctx.send(Messages.ERROR_NOT_AUTHENTICATED)
            return False
        if not LAVALINK_AVAILABLE:
            await ctx.send(Messages.ERROR_NO_AUDIO_COG)
            return False
        return True

    # --- URL Handlers ---

    async def _handle_tidal_url(self, ctx: commands.Context, url: str) -> None:
        for k, p in TIDAL_URL_PATTERNS.items():
            if m := p.search(url):
                func = getattr(self, f"_handle_{k}", None)
                if func:
                    await func(ctx, m.group(1))
                return
        await ctx.send(Messages.ERROR_INVALID_URL.format(platform="Tidal", content_type="link"))

    async def _handle_track(self, ctx: commands.Context, tid: str) -> None:
        t = await self.tidal.get_track(tid)
        if t:
            await self._load_and_queue_track(ctx, t)
        else:
            await ctx.send(Messages.ERROR_NO_TRACKS_FOUND)

    async def _handle_video(self, ctx: commands.Context, vid: str) -> None:
        v = await self.tidal.get_video(vid)
        if v:
            await self._load_and_queue_track(ctx, v)
        else:
            await ctx.send(Messages.ERROR_NO_TRACKS_FOUND)

    async def _handle_album(self, ctx: commands.Context, aid: str) -> None:
        alb = await self.tidal.get_album(aid)
        if alb:
            tracks = await self.tidal.get_items(alb)
            await self._process_track_list(ctx, tracks, getattr(alb, "name", aid), lambda t: t)
        else:
            await ctx.send(Messages.ERROR_CONTENT_UNAVAILABLE)

    async def _handle_playlist(self, ctx: commands.Context, pid: str) -> None:
        pl = await self.tidal.get_playlist(pid)
        if pl:
            tracks = await self.tidal.get_items(pl)
            await self._process_track_list(ctx, tracks, getattr(pl, "name", pid), lambda t: t)
        else:
            await ctx.send(Messages.ERROR_CONTENT_UNAVAILABLE)

    async def _handle_mix(self, ctx: commands.Context, mid: str) -> None:
        mix = await self.tidal.get_mix(mid)
        if mix:
            items = await self.tidal.get_items(mix)
            name = (
                getattr(mix, "title", None)
                or getattr(mix, "name", None)
                or "Tidal Mix"
            )
            await self._process_track_list(ctx, items, name, lambda t: t, discord.Color.purple())
        else:
            await ctx.send(Messages.ERROR_CONTENT_UNAVAILABLE)

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
                await ctx.send(Messages.ERROR_NO_TRACKS_FOUND)
            return

        # Spotify single track
        if m := SPOTIFY_TRACK_PATTERN.search(query):
            if not (SPOTIFY_AVAILABLE and self.sp):
                await ctx.send(Messages.ERROR_NO_SPOTIFY)
                return
            try:
                sp_track = await TidalHandler._run_blocking(
                    lambda: self.sp.track(m.group(1)), timeout=15.0
                )
                search_q = f"{sp_track['name']} {sp_track['artists'][0]['name']}"
                tracks = await self.tidal.search(search_q)
                if tracks:
                    await self._load_and_queue_track(ctx, tracks[0])
                else:
                    await ctx.send(Messages.ERROR_NO_TRACKS_FOUND)
            except Exception as e:
                log.error(f"Spotify track fetch error: {e}")
                await ctx.send(Messages.ERROR_FETCH_FAILED)
            return

        # Spotify playlist
        if m := SPOTIFY_PLAYLIST_PATTERN.search(query):
            if not (SPOTIFY_AVAILABLE and self.sp):
                await ctx.send(Messages.ERROR_NO_SPOTIFY)
                return
            try:
                items = await self._fetch_all_spotify_tracks(m.group(1))
                await self._process_track_list(
                    ctx, items, "Spotify Playlist",
                    lambda i: f"{i['track']['name']} {i['track']['artists'][0]['name']}",
                    discord.Color.green(),
                )
            except asyncio.TimeoutError:
                await ctx.send(Messages.ERROR_FETCH_FAILED)
            except Exception as e:
                log.error(f"Spotify fetch error: {e}")
                await ctx.send(Messages.ERROR_FETCH_FAILED)
            return

        # YouTube playlist
        if m := YOUTUBE_PLAYLIST_PATTERN.search(query):
            if not (YOUTUBE_API_AVAILABLE and self.yt):
                await ctx.send(Messages.ERROR_NO_YOUTUBE)
                return
            try:
                items = await self._fetch_all_youtube_tracks(m.group(1))
                await self._process_track_list(
                    ctx, items, "YouTube Playlist",
                    lambda i: i["snippet"]["title"],
                    discord.Color.red(),
                )
            except asyncio.TimeoutError:
                await ctx.send(Messages.ERROR_FETCH_FAILED)
            except Exception as e:
                log.error(f"YouTube fetch error: {e}")
                await ctx.send(Messages.ERROR_FETCH_FAILED)
            return

        # Plain text search
        filter_remixes = await self.config.guild(ctx.guild).filter_remixes()
        tracks = await self.tidal.search(query, filter_remixes=filter_remixes)
        if not tracks:
            await ctx.send(Messages.ERROR_NO_TRACKS_FOUND)
            return

        if await self.config.guild(ctx.guild).interactive_search():
            sel = await self._interactive_select(ctx, tracks)
            if sel:
                await self._load_and_queue_track(ctx, sel)
        else:
            await self._load_and_queue_track(ctx, tracks[0])

    @commands.guild_only()
    @commands.cooldown(1, 3, commands.BucketType.user)
    @commands.hybrid_command(name="tsearch")
    async def tsearch(self, ctx: commands.Context, *, query: str) -> None:
        """
        Search Tidal and pick from the top 5 results using buttons.
        Always shows the picker regardless of >tinteractive setting.
        """
        if not await self._check_ready(ctx):
            return
        filter_remixes = await self.config.guild(ctx.guild).filter_remixes()
        tracks = await self.tidal.search(query, filter_remixes=filter_remixes)
        if not tracks:
            await ctx.send(Messages.ERROR_NO_TRACKS_FOUND)
            return
        sel = await self._interactive_select(ctx, tracks)
        if sel:
            await self._load_and_queue_track(ctx, sel)

    @commands.guild_only()
    @commands.hybrid_command(name="tnp", aliases=["tnowplaying"])
    async def tnp(self, ctx: commands.Context) -> None:
        """
        Show the rich Tidal embed for the currently playing track.
        """
        meta = self._current_meta.get(ctx.guild.id)
        if not meta:
            await ctx.send(Messages.ERROR_NOT_PLAYING)
            return
        await self._send_now_playing(ctx, meta)

    @commands.guild_only()
    @commands.hybrid_command(name="tskip")
    async def tskip(self, ctx: commands.Context) -> None:
        """Skip the current track."""
        player = await self._get_player(ctx)
        if not player or not player.current:
            await ctx.send(Messages.ERROR_NOT_PLAYING)
            return
        await player.skip()
        await ctx.send("\u23ed\ufe0f Skipped.")

    @commands.guild_only()
    @commands.hybrid_command(name="tprev")
    async def tprev(self, ctx: commands.Context) -> None:
        """Restart the current track from the beginning."""
        player = await self._get_player(ctx)
        if not player or not player.current:
            await ctx.send(Messages.ERROR_NOT_PLAYING)
            return
        await player.seek(0)
        await ctx.send("\u23ee\ufe0f Restarted track.")

    @commands.guild_only()
    @commands.hybrid_command(name="tqueue")
    async def tqueue(self, ctx: commands.Context) -> None:
        """
        Show the current queue with Tidal track info.
        """
        player = await self._get_player(ctx)
        if not player:
            await ctx.send(Messages.ERROR_NO_PLAYER)
            return

        queue = list(getattr(player, "queue", []))
        current = getattr(player, "current", None)

        if not current and not queue:
            await ctx.send(Messages.ERROR_NO_QUEUE)
            return

        lines: List[str] = []
        if current:
            dur = self._format_duration(current.duration // 1000 if current.duration > 10000 else current.duration)
            lines.append(f"**\u25b6 Now:** {truncate(current.title, 60)} `[{dur}]`")

        for i, t in enumerate(queue[:MAX_ITEMS], 1):
            dur = self._format_duration(t.duration // 1000 if t.duration > 10000 else t.duration)
            lines.append(f"**{i}.** {truncate(t.title, 60)} `[{dur}]`")

        if not lines:
            await ctx.send(Messages.ERROR_NO_QUEUE)
            return

        # Paginate using Red's SimpleMenu
        pages: List[discord.Embed] = []
        chunks = [lines[i:i + QUEUE_PAGE_SIZE] for i in range(0, len(lines), QUEUE_PAGE_SIZE)]
        total_pages = len(chunks)
        for page_num, chunk in enumerate(chunks, 1):
            embed = discord.Embed(
                title=f"Queue — {len(queue)} track(s)",
                description="\n".join(chunk),
                color=discord.Color.blue(),
            )
            embed.set_footer(text=f"Page {page_num}/{total_pages}")
            pages.append(embed)

        if len(pages) == 1:
            await ctx.send(embed=pages[0])
        else:
            await SimpleMenu(pages).start(ctx)

    @commands.guild_only()
    @commands.hybrid_command(name="tstop")
    async def tstop(self, ctx: commands.Context) -> None:
        """Stop current playlist queueing operation."""
        cancel_event = self._get_cancel_event(ctx.guild.id)
        if not self._get_guild_lock(ctx.guild.id).locked():
            await ctx.send("No playlist is currently being queued.")
            return
        cancel_event.set()
        await ctx.send(Messages.STATUS_STOPPING)

    @commands.guild_only()
    @commands.hybrid_command(name="tfilter")
    async def tfilter(self, ctx: commands.Context) -> None:
        """Toggle remix/TikTok/sped-up track filtering."""
        curr = await self.config.guild(ctx.guild).filter_remixes()
        await self.config.guild(ctx.guild).filter_remixes.set(not curr)
        await ctx.send(
            Messages.SUCCESS_FILTER_ENABLED if not curr else Messages.SUCCESS_FILTER_DISABLED
        )

    @commands.guild_only()
    @commands.hybrid_command(name="tinteractive")
    async def tinteractive(self, ctx: commands.Context) -> None:
        """Toggle interactive (button-based) search mode."""
        curr = await self.config.guild(ctx.guild).interactive_search()
        await self.config.guild(ctx.guild).interactive_search.set(not curr)
        await ctx.send(
            Messages.SUCCESS_INTERACTIVE_ENABLED if not curr else Messages.SUCCESS_INTERACTIVE_DISABLED
        )

    @commands.guild_only()
    @commands.hybrid_command(name="tsimilar")
    async def tsimilar(self, ctx: commands.Context, *, album_url: str) -> None:
        """
        Show albums similar to a given Tidal album URL.
        Usage: >tsimilar https://tidal.com/browse/album/12345
        """
        if not await self._check_ready(ctx):
            return
        m = TIDAL_URL_PATTERNS["album"].search(album_url)
        if not m:
            await ctx.send(Messages.ERROR_INVALID_URL.format(platform="Tidal", content_type="album URL"))
            return
        alb = await self.tidal.get_album(m.group(1))
        if not alb:
            await ctx.send(Messages.ERROR_CONTENT_UNAVAILABLE)
            return
        similar = await self.tidal.get_similar_albums(alb)
        if not similar:
            await ctx.send("No similar albums found (requires tidalapi 0.8+).")
            return
        lines = []
        for i, sa in enumerate(similar[:8], 1):
            sa_name = getattr(sa, "name", "Unknown")
            sa_artist = getattr(getattr(sa, "artist", None), "name", "Unknown")
            sa_id = getattr(sa, "id", None)
            sa_url = make_tidal_share_url("album", sa_id) if sa_id else None
            lines.append(
                f"**{i}.** [{sa_name}]({sa_url}) \u2014 {sa_artist}" if sa_url
                else f"**{i}.** {sa_name} \u2014 {sa_artist}"
            )
        embed = discord.Embed(
            title=f"Similar to: {truncate(getattr(alb, 'name', 'Unknown'), 50)}",
            description="\n".join(lines),
            color=discord.Color.teal(),
        )
        await ctx.send(embed=embed)

    @commands.guild_only()
    @commands.hybrid_command(name="talbuminfo")
    async def talbuminfo(self, ctx: commands.Context, *, album_url: str) -> None:
        """
        Show info and editorial review for a Tidal album.
        Usage: >talbuminfo https://tidal.com/browse/album/12345
        """
        if not TIDALAPI_AVAILABLE or not await self.tidal.is_logged_in():
            await ctx.send(Messages.ERROR_NOT_AUTHENTICATED)
            return
        m = TIDAL_URL_PATTERNS["album"].search(album_url)
        if not m:
            await ctx.send(Messages.ERROR_INVALID_URL.format(platform="Tidal", content_type="album URL"))
            return
        alb = await self.tidal.get_album(m.group(1))
        if not alb:
            await ctx.send(Messages.ERROR_CONTENT_UNAVAILABLE)
            return

        name = getattr(alb, "name", "Unknown")
        artist = getattr(getattr(alb, "artist", None), "name", "Unknown")
        alb_id = getattr(alb, "id", None)
        release_date = getattr(alb, "release_date", None) or getattr(alb, "tidal_release_date", None)
        num_tracks = getattr(alb, "num_tracks", None)
        quality = getattr(alb, "audio_quality", None)
        share_url = make_tidal_share_url("album", alb_id) if alb_id else None

        embed = discord.Embed(
            title=name, description=artist,
            color=discord.Color.blurple(), url=share_url,
        )
        if release_date:
            embed.add_field(name="Released", value=str(release_date)[:10], inline=True)
        if num_tracks:
            embed.add_field(name="Tracks", value=str(num_tracks), inline=True)
        if quality:
            embed.add_field(name="Quality", value=QUALITY_LABELS.get(quality, quality), inline=True)
        try:
            if hasattr(alb, "image"):
                embed.set_thumbnail(url=alb.image(dimensions=320))
        except Exception:
            pass
        review = await self.tidal.get_album_review(alb)
        if review:
            embed.add_field(name="Review", value=truncate(review, 1000), inline=False)
        if share_url:
            embed.add_field(name="Open", value=f"[Share Page]({share_url})", inline=True)
        await ctx.send(embed=embed)

    @commands.guild_only()
    @commands.is_owner()
    @commands.hybrid_command(name="tdebug")
    async def tdebug(self, ctx: commands.Context) -> None:
        """Check Tidal connection status and library versions."""
        tidal_status = f"{EMOJI_NO} Not Connected"
        if await self.tidal.is_logged_in():
            tidal_status = f"{EMOJI_OK} Logged In"
        elif self.tidal.session:
            tidal_status = f"{EMOJI_WARN} Session Invalid/Expired"

        lavalink_status = f"{EMOJI_NO} Not Loaded"
        if LAVALINK_AVAILABLE:
            try:
                player = lavalink.get_player(ctx.guild.id)
                lavalink_status = f"{EMOJI_OK} Loaded (Connected: {player.is_connected})"
            except Exception:
                lavalink_status = f"{EMOJI_WARN} Loaded but no player found"

        try:
            tidal_ver = importlib.metadata.version("tidalapi")
        except Exception:
            tidal_ver = "Unknown"

        sess = self.tidal.session
        isrc_icon = EMOJI_OK if (sess and hasattr(sess, "get_tracks_by_isrc")) else EMOJI_NO
        mixv2_icon = EMOJI_OK if (sess and hasattr(sess, "mix_v2")) else EMOJI_NO
        video_icon = EMOJI_OK if (sess and hasattr(sess, "video")) else EMOJI_NO
        upl_icon = EMOJI_OK if TIDAL_USER_PLAYLIST_AVAILABLE else EMOJI_NO
        sp_track_icon = EMOJI_OK if (SPOTIFY_AVAILABLE and self.sp) else EMOJI_NO

        msg = (
            f"**TidalPlayer Debug**\n"
            f"**TidalAPI:** `{tidal_ver}`\n"
            f"**Initialized:** {EMOJI_OK if self._initialized else EMOJI_LOADING}\n"
            f"**Tidal:** {tidal_status}\n"
            f"**Lavalink:** {lavalink_status}\n"
            f"**YouTube:** {EMOJI_OK if self.yt else EMOJI_NO}\n"
            f"**Spotify:** {EMOJI_OK if self.sp else EMOJI_NO} (track URL: {sp_track_icon})\n"
            f"**ISRC:** {isrc_icon} | **MixV2:** {mixv2_icon} | **Video:** {video_icon}\n"
            f"**UserPlaylist:** {upl_icon}\n"
            f"**Pagination:** {EMOJI_OK} (limit={PAGINATION_LIMIT}, max={MAX_ITEMS})\n"
            f"**Rate-limit Backoff:** {EMOJI_OK} ({RATELIMIT_MAX_RETRIES} retries, base={RATELIMIT_BACKOFF_BASE}s)\n"
            f"**VC Reconnect:** {EMOJI_OK} ({VC_RECONNECT_RETRIES} retries)"
        )
        await ctx.send(msg)

    @commands.is_owner()
    @commands.command(name="tidalsetup")
    async def tidalsetup(self, ctx: commands.Context) -> None:
        """Set up Tidal OAuth authentication."""
        if not TIDALAPI_AVAILABLE:
            await ctx.send(Messages.ERROR_NO_TIDALAPI)
            return
        session = self.tidal.session
        if not session:
            await ctx.send("Tidal Session failed to initialize.")
            return
        try:
            login, future = await TidalHandler._run_blocking(session.login_oauth, timeout=60.0)
            e = discord.Embed(
                title="Tidal OAuth",
                description=f"[Click here to authenticate]({login.verification_uri_complete})",
                color=0x00B2FF,
            )
            try:
                await ctx.author.send(embed=e)
                await ctx.send("Check your DMs for the authentication link.")
            except discord.Forbidden:
                await ctx.send(embed=e)
            try:
                await TidalHandler._run_blocking(lambda: future.result(300), timeout=305.0)
            except asyncio.TimeoutError:
                await ctx.send("OAuth flow timed out.")
                return
            if await self.tidal.is_logged_in():
                try:
                    expiry_time = session.expiry_time
                except Exception:
                    expiry_time = None
                await asyncio.gather(
                    self.config.token_type.set(session.token_type),
                    self.config.access_token.set(session.access_token),
                    self.config.refresh_token.set(session.refresh_token),
                    self.config.expiry_time.set(int(expiry_time.timestamp()) if expiry_time else None),
                )
                self.tidal.invalidate_login_cache()
                await ctx.send(Messages.SUCCESS_TIDAL_SETUP)
            else:
                await ctx.send("Login failed.")
        except Exception as e:
            await ctx.send(f"Error: {e}")

    @commands.is_owner()
    @commands.group(name="tidalplay")
    async def tidalplay(self, ctx: commands.Context) -> None:
        """TidalPlayer configuration commands."""
        if ctx.invoked_subcommand is None:
            await ctx.send_help(ctx.command)

    @tidalplay.command(name="spotify")
    async def tidalplay_spotify(self, ctx: commands.Context, cid: str, csec: str) -> None:
        """Configure Spotify API credentials (send in DMs for security)."""
        # Delete the message to avoid exposing credentials in channel history
        try:
            await ctx.message.delete()
        except Exception:
            pass
        await asyncio.gather(
            self.config.spotify_client_id.set(cid),
            self.config.spotify_client_secret.set(csec),
        )
        await self._initialize_spotify(await self.config.all())
        await ctx.author.send(Messages.SUCCESS_SPOTIFY_CONFIGURED)

    @tidalplay.command(name="youtube")
    async def tidalplay_youtube(self, ctx: commands.Context, key: str) -> None:
        """Configure YouTube API key (send in DMs for security)."""
        try:
            await ctx.message.delete()
        except Exception:
            pass
        await self.config.youtube_api_key.set(key)
        await self._initialize_youtube(await self.config.all())
        await ctx.author.send(Messages.SUCCESS_YOUTUBE_CONFIGURED)

    @tidalplay.command(name="cleartokens")
    async def tidalplay_cleartokens(self, ctx: commands.Context) -> None:
        """Clear all stored tokens and credentials."""
        await self.config.clear_all()
        self.tidal.invalidate_login_cache()
        await ctx.send(Messages.SUCCESS_TOKENS_CLEARED)

    # =========================================================================
    # UserPlaylist Management — owner only (modifies bot account's Tidal)
    # =========================================================================

    @commands.is_owner()
    @commands.guild_only()
    @commands.group(name="tpl")
    async def tpl(self, ctx: commands.Context) -> None:
        """
        Manage the bot account's Tidal playlists. Owner only.
        \u26a0\ufe0f These commands modify the Tidal account the bot is logged into.
        """
        if ctx.invoked_subcommand is None:
            await ctx.send_help(ctx.command)

    @tpl.command(name="list")
    async def tpl_list(self, ctx: commands.Context) -> None:
        """List the bot account's Tidal playlists."""
        if not TIDALAPI_AVAILABLE or not await self.tidal.is_logged_in():
            await ctx.send(Messages.ERROR_NOT_AUTHENTICATED)
            return
        playlists = await self.tidal.get_user_playlists()
        if not playlists:
            await ctx.send("No user playlists found.")
            return
        lines = []
        for i, pl in enumerate(playlists[:20], 1):
            pl_name = getattr(pl, "name", "Unknown")
            pl_id = getattr(pl, "id", None)
            num = getattr(pl, "num_tracks", "?")
            share = make_tidal_share_url("playlist", pl_id) if pl_id else None
            id_str = f"`{pl_id}`" if pl_id else ""
            lines.append(
                f"**{i}.** [{pl_name}]({share}) {id_str} \u2014 {num} tracks" if share
                else f"**{i}.** {pl_name} {id_str} \u2014 {num} tracks"
            )
        embed = discord.Embed(
            title="Tidal Playlists (Bot Account)",
            description="\n".join(lines),
            color=discord.Color.blue(),
        )
        await ctx.send(embed=embed)

    @tpl.command(name="add")
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def tpl_add(self, ctx: commands.Context, playlist_id: str, track_url_or_id: str) -> None:
        """
        Add a track to a Tidal playlist.
        Usage: >tpl add <playlist_id> <track_url_or_id>
        Accepts full Tidal track URLs or bare track IDs.
        """
        if not TIDALAPI_AVAILABLE or not await self.tidal.is_logged_in():
            await ctx.send(Messages.ERROR_NOT_AUTHENTICATED)
            return
        track_id = self._resolve_track_id(track_url_or_id)
        if track_id is None:
            await ctx.send(Messages.ERROR_INVALID_URL.format(platform="Tidal", content_type="track URL or ID"))
            return
        pl = await self.tidal.get_playlist(playlist_id)
        if not pl:
            await ctx.send(Messages.ERROR_CONTENT_UNAVAILABLE)
            return
        if TIDAL_USER_PLAYLIST_AVAILABLE and not isinstance(pl, TidalUserPlaylist):
            await ctx.send(Messages.ERROR_NOT_USER_PLAYLIST)
            return
        success = await self.tidal.add_track_to_playlist(pl, track_id)
        if success:
            await ctx.send(f"{EMOJI_OK} Track added to **{getattr(pl, 'name', playlist_id)}**.")
        else:
            await ctx.send(Messages.ERROR_PLAYLIST_WRITE_FAILED)

    @tpl.command(name="remove")
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def tpl_remove(self, ctx: commands.Context, playlist_id: str, track_url_or_id: str) -> None:
        """
        Remove a track from a Tidal playlist.
        Usage: >tpl remove <playlist_id> <track_url_or_id>
        """
        if not TIDALAPI_AVAILABLE or not await self.tidal.is_logged_in():
            await ctx.send(Messages.ERROR_NOT_AUTHENTICATED)
            return
        track_id = self._resolve_track_id(track_url_or_id)
        if track_id is None:
            await ctx.send(Messages.ERROR_INVALID_URL.format(platform="Tidal", content_type="track URL or ID"))
            return
        pl = await self.tidal.get_playlist(playlist_id)
        if not pl:
            await ctx.send(Messages.ERROR_CONTENT_UNAVAILABLE)
            return
        if TIDAL_USER_PLAYLIST_AVAILABLE and not isinstance(pl, TidalUserPlaylist):
            await ctx.send(Messages.ERROR_NOT_USER_PLAYLIST)
            return
        success = await self.tidal.remove_track_from_playlist(pl, track_id)
        if success:
            await ctx.send(f"{EMOJI_OK} Track removed from **{getattr(pl, 'name', playlist_id)}**.")
        else:
            await ctx.send(Messages.ERROR_PLAYLIST_WRITE_FAILED)

    @tpl.command(name="save")
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def tpl_save(self, ctx: commands.Context, playlist_id: str) -> None:
        """
        Save the currently playing track to a Tidal playlist.
        Usage: >tpl save <playlist_id>
        """
        if not TIDALAPI_AVAILABLE or not await self.tidal.is_logged_in():
            await ctx.send(Messages.ERROR_NOT_AUTHENTICATED)
            return
        meta = self._current_meta.get(ctx.guild.id)
        if not meta or not meta.get("track_id"):
            await ctx.send(Messages.ERROR_NOT_PLAYING)
            return
        pl = await self.tidal.get_playlist(playlist_id)
        if not pl:
            await ctx.send(Messages.ERROR_CONTENT_UNAVAILABLE)
            return
        if TIDAL_USER_PLAYLIST_AVAILABLE and not isinstance(pl, TidalUserPlaylist):
            await ctx.send(Messages.ERROR_NOT_USER_PLAYLIST)
            return
        success = await self.tidal.add_track_to_playlist(pl, meta["track_id"])
        if success:
            await ctx.send(
                f"{EMOJI_OK} **{meta['title']}** saved to **{getattr(pl, 'name', playlist_id)}**."
            )
        else:
            await ctx.send(Messages.ERROR_PLAYLIST_WRITE_FAILED)

    def _resolve_track_id(self, value: str) -> Optional[int]:
        """Accept either a full Tidal track URL or a bare numeric track ID."""
        # Bare integer ID
        if value.isdigit():
            return int(value)
        # Full URL
        m = TIDAL_URL_PATTERNS["track"].search(value)
        if m:
            return int(m.group(1))
        return None


async def setup(bot: Red):
    await bot.add_cog(TidalPlayer(bot))
