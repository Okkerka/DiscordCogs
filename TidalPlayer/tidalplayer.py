"""
TidalPlayer - Tidal music integration for Red Discord Bot
Features: High-Res Audio, Album Art, Spotify/YT Importing, Debug Tools,
          MixV2, Video URLs, Hybrid Slash Commands, Similar Albums, UserPlaylist Mgmt
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

EMOJI_OK = "\u2705"
EMOJI_NO = "\u274c"
EMOJI_WARN = "\u26a0\ufe0f"
EMOJI_LOADING = "\u23f3"

REACTION_NUMBERS = ("1\ufe0f\u20e3", "2\ufe0f\u20e3", "3\ufe0f\u20e3", "4\ufe0f\u20e3", "5\ufe0f\u20e3")
CANCEL_EMOJI = "\u274c"

# MQA (HI_RES) removed in tidalapi 0.8.0
QUALITY_LABELS = {
    "HI_RES_LOSSLESS": "HI-RES LOSSLESS (FLAC)",
    "LOSSLESS": "LOSSLESS (FLAC)",
    "HIGH": "HIGH (320kbps)",
    "LOW": "LOW (96kbps)",
}

FILTER_KEYWORDS = frozenset(
    {"sped up", "slowed", "tiktok", "reverb", "8d audio", "bass boosted"}
)

TIDAL_URL_PATTERNS = {
    "track": re.compile(r"tidal\.com/(?:browse/)?track/(\d+)"),
    "video": re.compile(r"tidal\.com/(?:browse/)?video/(\d+)"),
    "album": re.compile(r"tidal\.com/(?:browse/)?album/(\d+)"),
    "playlist": re.compile(r"tidal\.com/(?:browse/)?playlist/([a-f0-9-]+)"),
    "mix": re.compile(r"tidal\.com/(?:browse/)?mix/([a-f0-9A-Z_-]+)"),
}

SPOTIFY_PLAYLIST_PATTERN = re.compile(r"open\.spotify\.com/playlist/([a-zA-Z0-9]+)")
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


class Messages:
    ERROR_NO_TIDALAPI = "tidalapi not installed. Run: `[p]pipinstall tidalapi`"
    ERROR_NOT_AUTHENTICATED = "Not authenticated. Run: `>tidalsetup`"
    ERROR_NO_AUDIO_COG = "Audio cog not loaded. Run: `[p]load audio`"
    ERROR_NO_PLAYER = "No active player. Join a voice channel first."
    ERROR_NO_TRACKS_FOUND = "No tracks found."
    ERROR_INVALID_URL = "Invalid {platform} {content_type} URL"
    ERROR_CONTENT_UNAVAILABLE = "Content unavailable (private/region-locked)"
    ERROR_LAVALINK_FAILED = "Playback failed: Could not retrieve Tidal stream."
    ERROR_STILL_LOADING = "\u23f3 TidalPlayer is still initializing, please wait a moment."

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
    ERROR_NO_SPOTIFY = "Spotify not configured."
    ERROR_NO_YOUTUBE = "YouTube not configured."
    ERROR_NOT_USER_PLAYLIST = "That playlist is not a user-owned playlist."
    ERROR_PLAYLIST_WRITE_FAILED = "Playlist operation failed."


def truncate(text: str, limit: int) -> str:
    """Optimized truncation using slicing."""
    return f"{text[:limit-3]}..." if len(text) > limit else text


def make_tidal_share_url(content_type: str, content_id: Any) -> str:
    """
    Build the correct Tidal share/browse page URL.
    tidal.com/browse/<type>/<id> — opens the Tidal web player share page
    where users can choose to open in app, Spotify, YouTube, etc.
    """
    return f"https://tidal.com/browse/{content_type}/{content_id}"


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

    async def _run_blocking(self, func: Callable[[], Any], timeout: float = 10.0) -> Any:
        """Execute blocking I/O in executor with timeout."""
        return await asyncio.wait_for(
            asyncio.get_running_loop().run_in_executor(None, func),
            timeout=timeout,
        )

    async def _run_with_backoff(self, func: Callable[[], Any], timeout: float = 10.0) -> Any:
        """
        Execute blocking I/O with exponential backoff on HTTP 429 / rate-limit errors.
        Retries up to RATELIMIT_MAX_RETRIES times before raising.
        """
        delay = RATELIMIT_BACKOFF_BASE
        for attempt in range(RATELIMIT_MAX_RETRIES):
            try:
                return await self._run_blocking(func, timeout=timeout)
            except Exception as e:
                err = str(e).lower()
                is_ratelimit = (
                    "429" in err
                    or "too many requests" in err
                    or "rate limit" in err
                    or "ratelimit" in err
                )
                if is_ratelimit and attempt < RATELIMIT_MAX_RETRIES - 1:
                    wait = min(delay, RATELIMIT_BACKOFF_MAX)
                    log.warning(f"Rate limited by Tidal, retrying in {wait:.1f}s (attempt {attempt + 1})")
                    await asyncio.sleep(wait)
                    delay *= 2
                else:
                    raise

    async def initialize(self, creds: Dict[str, Any]) -> None:
        """Load session from stored credentials."""
        if not self.session or not creds.get("access_token"):
            return

        try:
            expiry = (
                datetime.fromtimestamp(creds["expiry_time"])
                if creds.get("expiry_time")
                else None
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
        """Start the hourly token refresh loop."""
        if self._refresh_task:
            self._refresh_task.cancel()
        self._refresh_task = asyncio.create_task(self._auto_refresh_tokens())

    def unload(self) -> None:
        """Clean up resources."""
        if self._refresh_task:
            self._refresh_task.cancel()

    def invalidate_login_cache(self) -> None:
        """Force re-check of login status on next call."""
        self._login_cache = None
        self._login_cache_time = 0.0

    async def is_logged_in(self) -> bool:
        """Check if Tidal session is valid (cached, with retry on timeout)."""
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
                log.warning(
                    f"Timed out checking Tidal login status (attempt {attempt + 1}/{LOGIN_CHECK_RETRIES})"
                )
                if attempt < LOGIN_CHECK_RETRIES - 1:
                    await asyncio.sleep(2)
            except Exception:
                self._login_cache = False
                self._login_cache_time = asyncio.get_running_loop().time()
                return False

        return self._login_cache if self._login_cache is not None else False

    async def _auto_refresh_tokens(self) -> None:
        """Background task to refresh tokens hourly."""
        while True:
            await asyncio.sleep(3600)
            try:
                if not await self.is_logged_in():
                    continue

                try:
                    expiry_time = await self._run_blocking(
                        lambda: self.session.expiry_time, timeout=5.0
                    )
                except Exception as e:
                    log.warning(f"Token refresh state read failed: {e}")
                    continue

                if expiry_time and datetime.now() + timedelta(hours=2) > expiry_time:
                    log.info("Refreshing Tidal tokens...")

                    try:
                        if (
                            hasattr(self.session, "request")
                            and hasattr(self.session.request, "refresh_token")
                        ):
                            await self._run_blocking(
                                self.session.request.refresh_token, timeout=15.0
                            )
                            log.info("Tidal session token refreshed via request.refresh_token")
                    except Exception as e:
                        log.warning(f"Session token refresh call failed: {e}")

                    try:
                        def _get_state():
                            return (
                                self.session.expiry_time,
                                self.session.token_type,
                                self.session.access_token,
                                self.session.refresh_token,
                            )
                        expiry_time, token_type, access, refresh = await self._run_blocking(
                            _get_state, timeout=5.0
                        )
                    except Exception as e:
                        log.warning(f"Token state read after refresh failed: {e}")
                        continue

                    await asyncio.gather(
                        self.config.token_type.set(token_type),
                        self.config.access_token.set(access),
                        self.config.refresh_token.set(refresh),
                        self.config.expiry_time.set(
                            int(expiry_time.timestamp()) if expiry_time else None
                        ),
                    )
                    self._login_cache = True
                    self._login_cache_time = asyncio.get_running_loop().time()
            except Exception as e:
                log.error(f"Token refresh failed: {e}")

    async def search(self, query: str, filter_remixes: bool = False) -> List[Any]:
        """Search Tidal for tracks matching query."""
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
        """Fetch a track by ISRC code (tidalapi 0.8.0+)."""
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
        """Fetch a single track by ID."""
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
        """Fetch a single video by ID (tidalapi 0.7+)."""
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
        """Fetch an album by ID."""
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
        """Fetch a playlist by ID."""
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
        """
        Fetch a Tidal mix by ID.
        Tries MixV2 first (tidalapi 0.8+), falls back to legacy mix().
        """
        if not self.session:
            return None
        async with self.api_semaphore:
            # Try MixV2 first
            if hasattr(self.session, "mix_v2"):
                try:
                    result = await self._run_with_backoff(
                        lambda: self.session.mix_v2(mix_id), timeout=10.0
                    )
                    if result:
                        return result
                except Exception:
                    pass
            # Fallback to legacy mix()
            if hasattr(self.session, "mix"):
                try:
                    return await self._run_with_backoff(
                        lambda: self.session.mix(mix_id), timeout=10.0
                    )
                except Exception:
                    pass
            return None

    async def get_similar_albums(self, album: Any) -> List[Any]:
        """Get similar albums to the given album (tidalapi 0.8+)."""
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
        """Get the editorial review for an album (tidalapi 0.8+)."""
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
        """Get the current user's own playlists."""
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
        """Add a track to a UserPlaylist."""
        if not playlist:
            return False
        if not hasattr(playlist, "add"):
            return False
        async with self.api_semaphore:
            try:
                await self._run_with_backoff(
                    lambda: playlist.add([track_id]), timeout=10.0
                )
                return True
            except Exception as e:
                log.error(f"add_track_to_playlist failed: {e}")
                return False

    async def remove_track_from_playlist(self, playlist: Any, track_id: int) -> bool:
        """Remove a track from a UserPlaylist."""
        if not playlist:
            return False
        if not hasattr(playlist, "remove_by_id"):
            return False
        async with self.api_semaphore:
            try:
                await self._run_with_backoff(
                    lambda: playlist.remove_by_id(track_id), timeout=10.0
                )
                return True
            except Exception as e:
                log.error(f"remove_track_from_playlist failed: {e}")
                return False

    async def get_items(self, container: Any) -> List[Any]:
        """
        Paginate items/tracks from a container (Album/Playlist) using
        limit/offset params (tidalapi 0.8.0+), with a fallback to legacy fetch.
        Caps at MAX_ITEMS tracks total.
        """
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
        return items

    async def _paginate_items(self, container: Any) -> List[Any]:
        """Paginate through container items using limit/offset (tidalapi 0.8.0+)."""
        all_items: List[Any] = []
        offset = 0

        while len(all_items) < MAX_ITEMS:
            async with self.api_semaphore:
                try:
                    def _fetch(o=offset):
                        try:
                            return list(container.items(
                                limit=PAGINATION_LIMIT, offset=o, sparse_album=True
                            ))
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
        """
        Get (bit_depth, sample_rate) from album via get_audio_resolution()
        (tidalapi 0.7.5+). Returns None if unavailable.
        """
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
        """
        Get the direct audio stream URL from Tidal.

        Tries in order:
          1. tidalapi >=0.7: track.get_stream().get_urls() -> List[str]
          2. Legacy fallback: track.get_url()
          3. Web URL fallback: tidal.com/browse/track/<id>
        """
        track_id = getattr(track, "id", None)

        async with self.api_semaphore:
            try:
                def _get_urls() -> List[str]:
                    stream = track.get_stream()
                    return stream.get_urls()

                urls = await self._run_with_backoff(_get_urls, timeout=15.0)
                if urls:
                    log.debug(f"Got stream URL via get_stream().get_urls() for track {track_id}")
                    return urls[0]
            except asyncio.TimeoutError:
                log.debug(f"get_stream().get_urls() timed out for track {track_id}")
            except AttributeError:
                log.debug(f"get_stream() unavailable on track {track_id}, trying legacy")
            except Exception as e:
                log.debug(f"get_stream().get_urls() failed for track {track_id}: {e}")

            try:
                url = await self._run_with_backoff(track.get_url, timeout=10.0)
                if url:
                    log.debug(f"Got stream URL via legacy get_url() for track {track_id}")
                    return url
            except asyncio.TimeoutError:
                log.debug(f"Legacy get_url() timed out for track {track_id}")
            except Exception as e:
                log.debug(f"Legacy get_url() failed for track {track_id}: {e}")

        if track_id:
            url = make_tidal_share_url("track", track_id)
            log.debug(f"Falling back to web URL for track {track_id}: {url}")
            return url
        return None

    def _extract_tracks(self, result: Any) -> List[Any]:
        """Extract track list from search result."""
        if hasattr(result, "tracks"):
            t = result.tracks
            if isinstance(t, list):
                return t
            if hasattr(t, "items"):
                return getattr(t, "items", [])
            return []
        if isinstance(result, dict):
            t = result.get("tracks", [])
            if isinstance(t, list):
                return t
            if hasattr(t, "items"):
                return getattr(t, "items", [])
            return []
        return result if isinstance(result, list) else []

    def _filter_tracks(self, tracks: List[Any]) -> List[Any]:
        """Filter out remixes and TikTok versions."""
        if not tracks:
            return []
        return [
            t
            for t in tracks
            if not any(
                kw in (getattr(t, "name", "") or "").lower()
                for kw in FILTER_KEYWORDS
            )
        ]


class TidalPlayer(commands.Cog):
    """Play music from Tidal with full metadata support in native queue."""

    __slots__ = (
        "bot", "config", "tidal", "sp", "yt", "_tasks", "_guild_locks",
        "_cancel_events", "_last_progress_edit", "_initialized"
    )

    def __init__(self, bot: Red):
        self.bot = bot
        self.config = Config.get_conf(
            self, identifier=COG_IDENTIFIER, force_registration=True
        )

        self.config.register_global(
            token_type=None,
            access_token=None,
            refresh_token=None,
            expiry_time=None,
            spotify_client_id=None,
            spotify_client_secret=None,
            youtube_api_key=None,
        )
        self.config.register_guild(
            filter_remixes=True, interactive_search=False
        )

        self.tidal = TidalHandler(bot, self.config)
        self.sp: Optional[Any] = None
        self.yt: Optional[Any] = None

        self._tasks: Set[asyncio.Task] = set()
        self._guild_locks: Dict[int, asyncio.Lock] = {}
        self._cancel_events: Dict[int, asyncio.Event] = {}
        self._last_progress_edit: Dict[int, float] = {}
        self._initialized: bool = False

    async def cog_load(self) -> None:
        """Called by Red after __init__ - safe place for async setup."""
        await self._initialize_apis()

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

    async def _run_blocking_io(self, func: Callable[[], Any], timeout: float = 10.0) -> Any:
        return await asyncio.wait_for(
            asyncio.get_running_loop().run_in_executor(None, func),
            timeout=timeout,
        )

    def cog_unload(self) -> None:
        for ev in self._cancel_events.values():
            ev.set()
        for task in list(self._tasks):
            task.cancel()
        self.tidal.unload()
        self.sp = None
        self.yt = None
        log.info("TidalPlayer cog unloaded")

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
                self.sp = await self._run_blocking_io(_build, timeout=15.0)
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
                self.yt = await self._run_blocking_io(
                    lambda: build("youtube", "v3", developerKey=key),
                    timeout=15.0,
                )
            except asyncio.TimeoutError:
                log.error("YouTube API init timed out")
            except Exception as e:
                log.error(f"YouTube init failed: {e}")

    # --- Core Logic ---

    async def _extract_meta(self, track: Any) -> TrackMeta:
        """
        Extract metadata from a Tidal track or video object.
        Uses full_name (0.7.2+), share_url via browse page, and
        get_audio_resolution (0.7.5+) for Hi-Res bit depth/sample rate.
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

        # Use browse page URL — opens the Tidal share page (choose app, Spotify, YouTube, etc.)
        # Direct tidal.com links require a login redirect; browse page works publicly
        content_type = "video" if hasattr(track, "video_quality") else "track"
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
        }

        try:
            if album_obj and hasattr(album_obj, "image"):
                meta["image"] = album_obj.image(dimensions=640)
            elif album_obj and hasattr(album_obj, "cover") and album_obj.cover:
                uuid = album_obj.cover.replace("-", "/")
                meta["image"] = f"https://resources.tidal.com/images/{uuid}/640x640.jpg"
        except Exception:
            pass

        if quality == "HI_RES_LOSSLESS" and album_obj:
            res = await self.tidal.get_audio_resolution(album_obj)
            if res:
                bit_depth, sample_rate = res
                khz = sample_rate // 1000 if sample_rate >= 1000 else sample_rate
                meta["audio_resolution"] = f"HI-RES LOSSLESS ({bit_depth}-bit / {khz}kHz)"

        return meta

    def _format_duration(self, seconds: int) -> str:
        m, s = divmod(seconds, 60)
        return f"{m // 60}:{m % 60:02d}:{s:02d}" if m >= 60 else f"{m:02d}:{s:02d}"

    async def _get_player(self, ctx: commands.Context) -> Optional[Any]:
        if not LAVALINK_AVAILABLE:
            return None
        try:
            return lavalink.get_player(ctx.guild.id)
        except Exception:
            pass
        if ctx.author.voice and ctx.author.voice.channel:
            try:
                await lavalink.connect(ctx.author.voice.channel)
                return lavalink.get_player(ctx.guild.id)
            except Exception as e:
                log.debug(f"Failed to connect to VC: {e}")
        return None

    async def _load_and_queue_track(
        self, ctx: commands.Context, tidal_track: Any, show_embed: bool = True
    ) -> bool:
        meta = await self._extract_meta(tidal_track)
        player = await self._get_player(ctx)

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

        if show_embed:
            await self._send_now_playing(ctx, meta)

        return True

    async def _send_now_playing(self, ctx: commands.Context, meta: TrackMeta) -> None:
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

        # Opens tidal.com/browse/track/<id> — the public share page
        if meta.get("share_url"):
            embed.add_field(
                name="Open in TIDAL",
                value=f"[Share Page]({meta['share_url']})",
                inline=True,
            )

        embed.set_footer(text=f"Duration: {self._format_duration(meta['duration'])}")
        if meta.get("image"):
            embed.set_thumbnail(url=meta["image"])

        await ctx.send(embed=embed)

    async def _interactive_select(
        self, ctx: commands.Context, tracks: List[Any]
    ) -> Optional[Any]:
        if not tracks:
            return None

        top = tracks[:5]
        desc = [
            f"**{i + 1}.** {getattr(t, 'full_name', None) or t.name} - "
            f"{getattr(t.artist, 'name', 'Unknown') if hasattr(t, 'artist') else 'Unknown'}"
            for i, t in enumerate(top)
        ]

        embed = discord.Embed(
            title="Select Track",
            description="\n".join(desc),
            color=discord.Color.blue(),
        )
        embed.set_footer(text=f"React with 1-{len(top)} or {CANCEL_EMOJI}")
        msg = await ctx.send(embed=embed)
        self._create_task(self._add_reactions(msg, len(top)))

        def check(r, u):
            return (
                u == ctx.author
                and str(r.emoji) in REACTION_NUMBERS[: len(top)] + (CANCEL_EMOJI,)
                and r.message.id == msg.id
            )

        try:
            r, _ = await self.bot.wait_for(
                "reaction_add", timeout=INTERACTIVE_TIMEOUT, check=check
            )
            try:
                await msg.delete()
            except Exception:
                pass
            if str(r.emoji) == CANCEL_EMOJI:
                return None
            return top[REACTION_NUMBERS.index(str(r.emoji))]
        except asyncio.TimeoutError:
            try:
                await msg.delete()
            except Exception:
                pass
            await ctx.send(Messages.ERROR_TIMEOUT)
            return None

    async def _add_reactions(self, msg: discord.Message, count: int) -> None:
        try:
            for emoji in list(REACTION_NUMBERS[:count]) + [CANCEL_EMOJI]:
                await msg.add_reaction(emoji)
        except Exception:
            pass

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
        """Paginate through all tracks in a Spotify playlist."""
        all_items: List[Any] = []
        offset = 0
        limit = 100
        while True:
            resp = await self._run_blocking_io(
                lambda o=offset: self.sp.playlist_tracks(
                    playlist_id, limit=limit, offset=o
                ),
                timeout=20.0,
            )
            items = resp.get("items", [])
            all_items.extend(i for i in items if i.get("track"))
            if not resp.get("next"):
                break
            offset += limit
            await asyncio.sleep(0)
        return all_items

    async def _fetch_all_youtube_tracks(
        self, playlist_id: str
    ) -> List[Any]:
        """Paginate through all items in a YouTube playlist via nextPageToken."""
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
            resp = await self._run_blocking_io(req.execute, timeout=20.0)
            all_items.extend(resp.get("items", []))
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
        player = await self._get_player(ctx)
        if not player:
            await ctx.send(Messages.ERROR_NO_PLAYER)
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
                    if cancel_event.is_set() or not getattr(player, "is_connected", True):
                        break

                    query = item_processor(item)
                    success = False

                    if query and (
                        hasattr(query, "id")
                        or hasattr(query, "get_url")
                        or hasattr(query, "get_stream")
                    ):
                        success = await self._load_and_queue_track(
                            ctx, query, show_embed=False
                        )
                    elif query:
                        tracks = await self.tidal.search(query, filter_remixes=filter_remixes)
                        if tracks:
                            success = await self._load_and_queue_track(
                                ctx, tracks[0], show_embed=False
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
        if not await self._get_player(ctx):
            await ctx.send(Messages.ERROR_NO_PLAYER)
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
        await ctx.send(
            Messages.ERROR_INVALID_URL.format(platform="Tidal", content_type="link")
        )

    async def _handle_track(self, ctx: commands.Context, tid: str) -> None:
        t = await self.tidal.get_track(tid)
        if t:
            await self._load_and_queue_track(ctx, t, show_embed=True)
        else:
            await ctx.send(Messages.ERROR_NO_TRACKS_FOUND)

    async def _handle_video(self, ctx: commands.Context, vid: str) -> None:
        """Handle Tidal video URL — queues the audio stream of the video."""
        v = await self.tidal.get_video(vid)
        if v:
            await self._load_and_queue_track(ctx, v, show_embed=True)
        else:
            await ctx.send(Messages.ERROR_NO_TRACKS_FOUND)

    async def _handle_album(self, ctx: commands.Context, aid: str) -> None:
        alb = await self.tidal.get_album(aid)
        if alb:
            tracks = await self.tidal.get_items(alb)
            await self._process_track_list(ctx, tracks, alb.name, lambda t: t)
        else:
            await ctx.send(Messages.ERROR_CONTENT_UNAVAILABLE)

    async def _handle_playlist(self, ctx: commands.Context, pid: str) -> None:
        pl = await self.tidal.get_playlist(pid)
        if pl:
            tracks = await self.tidal.get_items(pl)
            await self._process_track_list(ctx, tracks, pl.name, lambda t: t)
        else:
            await ctx.send(Messages.ERROR_CONTENT_UNAVAILABLE)

    async def _handle_mix(self, ctx: commands.Context, mid: str) -> None:
        mix = await self.tidal.get_mix(mid)
        if mix:
            items = await self.tidal.get_items(mix)
            name = getattr(mix, "title", None) or getattr(mix, "name", None) or f"Mix: {mid}"
            await self._process_track_list(
                ctx, items, name, lambda t: t, discord.Color.purple()
            )
        else:
            await ctx.send(Messages.ERROR_CONTENT_UNAVAILABLE)

    # --- Commands ---

    @commands.guild_only()
    @commands.cooldown(1, 5, commands.BucketType.user)
    @commands.hybrid_command(name="tplay")
    async def tplay(self, ctx: commands.Context, *, query: str) -> None:
        """
        Play a track, album, playlist, mix, or video from Tidal.

        Supports Tidal URLs, Spotify playlist URLs, YouTube playlist URLs,
        ISRC lookup (`isrc:USRC12345678`), and plain text search.
        """
        if not await self._check_ready(ctx):
            return

        if "tidal.com" in query:
            await self._handle_tidal_url(ctx, query)
            return

        if isrc_match := ISRC_PATTERN.match(query.strip()):
            track = await self.tidal.get_track_by_isrc(isrc_match.group(1).upper())
            if track:
                await self._load_and_queue_track(ctx, track, show_embed=True)
            else:
                await ctx.send(Messages.ERROR_NO_TRACKS_FOUND)
            return

        if m := SPOTIFY_PLAYLIST_PATTERN.search(query):
            if not (SPOTIFY_AVAILABLE and self.sp):
                await ctx.send(Messages.ERROR_NO_SPOTIFY)
                return
            try:
                items = await self._fetch_all_spotify_tracks(m.group(1))
                await self._process_track_list(
                    ctx,
                    items,
                    "Spotify",
                    lambda i: f"{i['track']['name']} {i['track']['artists'][0]['name']}",
                    discord.Color.green(),
                )
                return
            except asyncio.TimeoutError:
                log.error("Spotify playlist fetch timeout")
                await ctx.send(Messages.ERROR_FETCH_FAILED)
                return
            except Exception as e:
                log.error(f"Spotify fetch error: {e}")
                await ctx.send(Messages.ERROR_FETCH_FAILED)
                return

        if m := YOUTUBE_PLAYLIST_PATTERN.search(query):
            if not (YOUTUBE_API_AVAILABLE and self.yt):
                await ctx.send(Messages.ERROR_NO_YOUTUBE)
                return
            try:
                items = await self._fetch_all_youtube_tracks(m.group(1))
                await self._process_track_list(
                    ctx,
                    items,
                    "YouTube",
                    lambda i: i["snippet"]["title"],
                    discord.Color.red(),
                )
                return
            except asyncio.TimeoutError:
                log.error("YouTube playlist fetch timeout")
                await ctx.send(Messages.ERROR_FETCH_FAILED)
                return
            except Exception as e:
                log.error(f"YouTube fetch error: {e}")
                await ctx.send(Messages.ERROR_FETCH_FAILED)
                return

        filter_remixes = await self.config.guild(ctx.guild).filter_remixes()
        tracks = await self.tidal.search(query, filter_remixes=filter_remixes)

        if not tracks:
            await ctx.send(Messages.ERROR_NO_TRACKS_FOUND)
            return

        if await self.config.guild(ctx.guild).interactive_search():
            sel = await self._interactive_select(ctx, tracks)
            if sel:
                await self._load_and_queue_track(ctx, sel, show_embed=True)
        else:
            await self._load_and_queue_track(ctx, tracks[0], show_embed=True)

    @commands.guild_only()
    @commands.hybrid_command(name="tstop")
    async def tstop(self, ctx: commands.Context) -> None:
        """Stop current playlist queueing operation."""
        self._get_cancel_event(ctx.guild.id).set()
        await ctx.send(Messages.STATUS_STOPPING)

    @commands.guild_only()
    @commands.hybrid_command(name="tfilter")
    async def tfilter(self, ctx: commands.Context) -> None:
        """Toggle remix/TikTok track filtering."""
        curr = await self.config.guild(ctx.guild).filter_remixes()
        await self.config.guild(ctx.guild).filter_remixes.set(not curr)
        await ctx.send(
            Messages.SUCCESS_FILTER_ENABLED if not curr else Messages.SUCCESS_FILTER_DISABLED
        )

    @commands.guild_only()
    @commands.hybrid_command(name="tinteractive")
    async def tinteractive(self, ctx: commands.Context) -> None:
        """Toggle interactive search mode (reaction-based track picker)."""
        curr = await self.config.guild(ctx.guild).interactive_search()
        await self.config.guild(ctx.guild).interactive_search.set(not curr)
        await ctx.send(
            Messages.SUCCESS_INTERACTIVE_ENABLED
            if not curr
            else Messages.SUCCESS_INTERACTIVE_DISABLED
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
            sa_url = make_tidal_share_url("album", sa_id) if sa_id else ""
            lines.append(f"**{i}.** [{sa_name}]({sa_url}) — {sa_artist}" if sa_url else f"**{i}.** {sa_name} — {sa_artist}")

        alb_name = getattr(alb, "name", "Unknown")
        embed = discord.Embed(
            title=f"Similar to: {truncate(alb_name, 50)}",
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
            title=name,
            description=artist,
            color=discord.Color.blurple(),
            url=share_url,
        )
        if release_date:
            embed.add_field(name="Released", value=str(release_date)[:10], inline=True)
        if num_tracks:
            embed.add_field(name="Tracks", value=str(num_tracks), inline=True)
        if quality:
            embed.add_field(
                name="Quality",
                value=QUALITY_LABELS.get(quality, quality),
                inline=True,
            )

        # Album art
        try:
            if hasattr(alb, "image"):
                embed.set_thumbnail(url=alb.image(dimensions=320))
        except Exception:
            pass

        # Editorial review (tidalapi 0.8+)
        review = await self.tidal.get_album_review(alb)
        if review:
            embed.add_field(
                name="Review",
                value=truncate(review, 1000),
                inline=False,
            )

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
        similar_icon = EMOJI_OK if TIDALAPI_AVAILABLE else EMOJI_NO
        upl_icon = EMOJI_OK if TIDAL_USER_PLAYLIST_AVAILABLE else EMOJI_NO

        msg = (
            f"**TidalPlayer Debug**\n"
            f"**TidalAPI Version:** `{tidal_ver}`\n"
            f"**Initialized:** {EMOJI_OK if self._initialized else EMOJI_LOADING}\n"
            f"**Tidal Status:** {tidal_status}\n"
            f"**Lavalink:** {lavalink_status}\n"
            f"**YouTube API:** {EMOJI_OK if self.yt else EMOJI_NO}\n"
            f"**Spotify API:** {EMOJI_OK if self.sp else EMOJI_NO}\n"
            f"**ISRC Lookup:** {isrc_icon}\n"
            f"**MixV2:** {mixv2_icon}\n"
            f"**Video URLs:** {video_icon}\n"
            f"**Similar Albums:** {similar_icon}\n"
            f"**UserPlaylist Mgmt:** {upl_icon}\n"
            f"**Pagination:** {EMOJI_OK} (limit={PAGINATION_LIMIT}, max={MAX_ITEMS})\n"
            f"**Rate-limit Backoff:** {EMOJI_OK} (max {RATELIMIT_MAX_RETRIES} retries)"
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
            login, future = await self._run_blocking_io(session.login_oauth, timeout=60.0)
            e = discord.Embed(
                title="Tidal OAuth",
                description=f"[Click here to authenticate]({login.verification_uri_complete})",
                color=0x00B2FF,
            )
            try:
                await ctx.author.send(embed=e)
                await ctx.send("Check DMs for the authentication link.")
            except discord.Forbidden:
                await ctx.send(embed=e)

            try:
                await self._run_blocking_io(lambda: future.result(300), timeout=305.0)
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
                    self.config.expiry_time.set(
                        int(expiry_time.timestamp()) if expiry_time else None
                    ),
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
        """Configure Spotify API credentials."""
        await asyncio.gather(
            self.config.spotify_client_id.set(cid),
            self.config.spotify_client_secret.set(csec),
        )
        await self._initialize_spotify(await self.config.all())
        await ctx.send(Messages.SUCCESS_SPOTIFY_CONFIGURED)

    @tidalplay.command(name="youtube")
    async def tidalplay_youtube(self, ctx: commands.Context, key: str) -> None:
        """Configure YouTube API key."""
        await self.config.youtube_api_key.set(key)
        await self._initialize_youtube(await self.config.all())
        await ctx.send(Messages.SUCCESS_YOUTUBE_CONFIGURED)

    @tidalplay.command(name="cleartokens")
    async def tidalplay_cleartokens(self, ctx: commands.Context) -> None:
        """Clear all stored tokens and credentials."""
        await self.config.clear_all()
        self.tidal.invalidate_login_cache()
        await ctx.send(Messages.SUCCESS_TOKENS_CLEARED)

    # --- UserPlaylist Management ---

    @commands.guild_only()
    @commands.group(name="tpl")
    async def tpl(self, ctx: commands.Context) -> None:
        """Manage your Tidal user playlists."""
        if ctx.invoked_subcommand is None:
            await ctx.send_help(ctx.command)

    @tpl.command(name="list")
    async def tpl_list(self, ctx: commands.Context) -> None:
        """List your Tidal playlists."""
        if not TIDALAPI_AVAILABLE or not await self.tidal.is_logged_in():
            await ctx.send(Messages.ERROR_NOT_AUTHENTICATED)
            return

        playlists = await self.tidal.get_user_playlists()
        if not playlists:
            await ctx.send("No user playlists found.")
            return

        lines = []
        for i, pl in enumerate(playlists[:15], 1):
            pl_name = getattr(pl, "name", "Unknown")
            pl_id = getattr(pl, "id", None)
            num = getattr(pl, "num_tracks", "?")
            share = make_tidal_share_url("playlist", pl_id) if pl_id else ""
            lines.append(f"**{i}.** [{pl_name}]({share}) — {num} tracks" if share else f"**{i}.** {pl_name} — {num} tracks")

        embed = discord.Embed(
            title="Your Tidal Playlists",
            description="\n".join(lines),
            color=discord.Color.blue(),
        )
        await ctx.send(embed=embed)

    @tpl.command(name="add")
    async def tpl_add(
        self,
        ctx: commands.Context,
        playlist_id: str,
        track_url: str,
    ) -> None:
        """
        Add a track to one of your Tidal playlists.
        Usage: >tpl add <playlist_id> <tidal_track_url>
        """
        if not TIDALAPI_AVAILABLE or not await self.tidal.is_logged_in():
            await ctx.send(Messages.ERROR_NOT_AUTHENTICATED)
            return

        m = TIDAL_URL_PATTERNS["track"].search(track_url)
        if not m:
            await ctx.send(Messages.ERROR_INVALID_URL.format(platform="Tidal", content_type="track URL"))
            return

        track_id = int(m.group(1))
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
    async def tpl_remove(
        self,
        ctx: commands.Context,
        playlist_id: str,
        track_url: str,
    ) -> None:
        """
        Remove a track from one of your Tidal playlists.
        Usage: >tpl remove <playlist_id> <tidal_track_url>
        """
        if not TIDALAPI_AVAILABLE or not await self.tidal.is_logged_in():
            await ctx.send(Messages.ERROR_NOT_AUTHENTICATED)
            return

        m = TIDAL_URL_PATTERNS["track"].search(track_url)
        if not m:
            await ctx.send(Messages.ERROR_INVALID_URL.format(platform="Tidal", content_type="track URL"))
            return

        track_id = int(m.group(1))
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


async def setup(bot: Red):
    await bot.add_cog(TidalPlayer(bot))
