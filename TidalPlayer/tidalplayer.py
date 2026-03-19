"""
TidalPlayer - Tidal music integration for Red Discord Bot
Features: High-Res Audio, Album Art, Spotify/YT Importing, Debug Tools
"""

import asyncio
import importlib.metadata
import logging
import re
from datetime import datetime, timedelta
from typing import Any, Callable, Coroutine, Dict, List, Optional, Set, TypedDict

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
        TIDAL_MODELS_AVAILABLE = False
    TIDALAPI_AVAILABLE = True
except ImportError:
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

# --- Constants ---
COG_IDENTIFIER = 160819386
API_SEMAPHORE_LIMIT = 5
INTERACTIVE_TIMEOUT = 30
BATCH_UPDATE_INTERVAL = 10
LOGIN_CACHE_TTL = 300.0
PROGRESS_EDIT_RATELIMIT = 1.5
LOGIN_CHECK_TIMEOUT = 10.0
LOGIN_CHECK_RETRIES = 2

REACTION_NUMBERS = ("1\ufe0f\u20e3", "2\ufe0f\u20e3", "3\ufe0f\u20e3", "4\ufe0f\u20e3", "5\ufe0f\u20e3")
CANCEL_EMOJI = "\u274c"

QUALITY_LABELS = {
    "HI_RES": "HI-RES (MQA)",
    "HI_RES_LOSSLESS": "HI-RES LOSSLESS",
    "LOSSLESS": "LOSSLESS (FLAC)",
    "HIGH": "HIGH (320kbps)",
    "LOW": "LOW (96kbps)",
}

FILTER_KEYWORDS = frozenset(
    {"sped up", "slowed", "tiktok", "reverb", "8d audio", "bass boosted"}
)

TIDAL_URL_PATTERNS = {
    "track": re.compile(r"tidal\.com/(?:browse/)?track/(\d+)"),
    "album": re.compile(r"tidal\.com/(?:browse/)?album/(\d+)"),
    "playlist": re.compile(r"tidal\.com/(?:browse/)?playlist/([a-f0-9-]+)"),
    "mix": re.compile(r"tidal\.com/(?:browse/)?mix/([a-f0-9]+)"),
}

SPOTIFY_PLAYLIST_PATTERN = re.compile(r"open\.spotify\.com/playlist/([a-zA-Z0-9]+)")
YOUTUBE_PLAYLIST_PATTERN = re.compile(r"youtube\.com/.*[?&]list=([a-zA-Z0-9_-]+)")


class TrackMeta(TypedDict):
    title: str
    artist: str
    album: Optional[str]
    duration: int
    quality: str
    image: Optional[str]


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


def truncate(text: str, limit: int) -> str:
    """Optimized truncation using slicing."""
    return f"{text[:limit-3]}..." if len(text) > limit else text


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

            now = asyncio.get_running_loop().time()
            self._login_cache = True
            self._login_cache_time = now
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

                def _get_expiry():
                    return self.session.expiry_time

                try:
                    expiry_time = await self._run_blocking(_get_expiry, timeout=5.0)
                except Exception as e:
                    log.warning(f"Token refresh state read failed: {e}")
                    continue

                if expiry_time and datetime.now() + timedelta(hours=2) > expiry_time:
                    log.info("Refreshing Tidal tokens...")

                    # Trigger actual session token refresh if supported
                    try:
                        if hasattr(self.session, "request") and hasattr(self.session.request, "refresh_token"):
                            await self._run_blocking(self.session.request.refresh_token, timeout=15.0)
                            log.info("Tidal session token refreshed via request.refresh_token")
                    except Exception as e:
                        log.warning(f"Session token refresh call failed: {e}")

                    def _get_state():
                        return (
                            self.session.expiry_time,
                            self.session.token_type,
                            self.session.access_token,
                            self.session.refresh_token,
                        )

                    try:
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
                    if TIDAL_MODELS_AVAILABLE and "TidalTrack" in globals():
                        return self.session.search(query, models=[TidalTrack])
                    return self.session.search(query)

                result = await self._run_blocking(run_search, timeout=10.0)
                tracks = self._extract_tracks(result)
                return self._filter_tracks(tracks) if filter_remixes else tracks
            except asyncio.TimeoutError:
                log.warning(f"Tidal search timeout for '{query}'")
                return []
            except Exception as e:
                log.error(f"Search failed for '{query}': {e}")
                return []

    async def get_track(self, track_id: str) -> Optional[Any]:
        """Fetch a single track by ID."""
        if not self.session:
            return None
        async with self.api_semaphore:
            try:
                return await self._run_blocking(
                    lambda: self.session.track(track_id), timeout=10.0
                )
            except asyncio.TimeoutError:
                log.warning(f"Tidal get_track timeout for id {track_id}")
                return None
            except Exception as e:
                log.debug(f"Failed to fetch track {track_id}: {e}")
                return None

    async def get_album(self, album_id: str) -> Optional[Any]:
        """Fetch an album by ID."""
        if not self.session:
            return None
        async with self.api_semaphore:
            try:
                return await self._run_blocking(
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
                return await self._run_blocking(
                    lambda: self.session.playlist(playlist_id), timeout=10.0
                )
            except Exception:
                return None

    async def get_mix(self, mix_id: str) -> Optional[Any]:
        """Fetch a Tidal mix by ID."""
        if not self.session:
            return None
        async with self.api_semaphore:
            try:
                if not hasattr(self.session, "mix"):
                    return None
                return await self._run_blocking(
                    lambda: self.session.mix(mix_id), timeout=10.0
                )
            except Exception:
                return None

    async def get_items(self, container: Any) -> List[Any]:
        """Safely fetch items/tracks from a container (Album/Playlist)."""
        def _fetch():
            if hasattr(container, "tracks"):
                val = container.tracks
                return val() if callable(val) else val
            if hasattr(container, "items"):
                val = container.items
                return val() if callable(val) else val
            return []

        async with self.api_semaphore:
            try:
                items = await self._run_blocking(_fetch, timeout=20.0)
            except asyncio.TimeoutError:
                log.error("Timed out extracting items from Tidal container")
                return []
            except Exception as e:
                log.error(f"Failed to extract items: {e}")
                return []

        if len(items) > 1000:
            log.warning(f"Truncating Tidal container from {len(items)} to 1000 items")
            return list(items)[:1000]
        return items

    async def get_stream_url(self, track: Any) -> Optional[str]:
        """
        Get the direct audio stream URL from Tidal.

        Tries in order:
          1. tidalapi >=0.7: track.get_stream().get_urls()[0]
          2. Legacy fallback: track.get_url()
          3. Web URL fallback: tidal.com/browse/track/<id>
        """
        track_id = getattr(track, "id", None)

        # --- Method 1: tidalapi >=0.7 stream API ---
        async with self.api_semaphore:
            try:
                def _get_stream_urls():
                    stream = track.get_stream()
                    return stream.get_urls()

                urls = await self._run_blocking(_get_stream_urls, timeout=15.0)
                if urls:
                    log.debug(f"Got stream URL via get_stream().get_urls() for track {track_id}")
                    return urls[0]
            except asyncio.TimeoutError:
                log.debug(f"get_stream().get_urls() timed out for track {track_id}")
            except AttributeError:
                log.debug(f"get_stream() not available on track {track_id}, trying legacy")
            except Exception as e:
                log.debug(f"get_stream().get_urls() failed for track {track_id}: {e}")

            # --- Method 2: Legacy get_url() ---
            try:
                url = await self._run_blocking(track.get_url, timeout=10.0)
                if url:
                    log.debug(f"Got stream URL via legacy get_url() for track {track_id}")
                    return url
            except asyncio.TimeoutError:
                log.debug(f"Legacy get_url() timed out for track {track_id}")
            except Exception as e:
                log.debug(f"Legacy get_url() failed for track {track_id}: {e}")

        # --- Method 3: Web URL fallback (Lavalink may be able to resolve) ---
        if track_id:
            url = f"https://tidal.com/browse/track/{track_id}"
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

        self._create_task(self._initialize_apis())

    def _create_task(self, coro: Coroutine[Any, Any, Any]) -> asyncio.Task:
        """Create and track a background task."""
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def _get_guild_lock(self, guild_id: int) -> asyncio.Lock:
        """Get or create a lock for guild operations."""
        if guild_id not in self._guild_locks:
            self._guild_locks[guild_id] = asyncio.Lock()
        return self._guild_locks[guild_id]

    def _get_cancel_event(self, guild_id: int) -> asyncio.Event:
        """Get or create a cancel event for guild operations."""
        if guild_id not in self._cancel_events:
            self._cancel_events[guild_id] = asyncio.Event()
        return self._cancel_events[guild_id]

    async def _run_blocking_io(self, func: Callable[[], Any], timeout: float = 10.0) -> Any:
        """Execute blocking I/O in executor with timeout."""
        return await asyncio.wait_for(
            asyncio.get_running_loop().run_in_executor(None, func),
            timeout=timeout,
        )

    def cog_unload(self) -> None:
        """Clean up resources on cog unload."""
        for ev in self._cancel_events.values():
            ev.set()
        for task in list(self._tasks):
            task.cancel()
        self.tidal.unload()
        self.sp = None
        self.yt = None
        log.info("TidalPlayer cog unloaded")

    async def _initialize_apis(self) -> None:
        """Initialize all external APIs in parallel."""
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
        """Initialize Spotify API client."""
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
        """Initialize YouTube API client."""
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

    def _extract_meta(self, track: Any) -> TrackMeta:
        """Extract metadata from Tidal track object."""
        name = getattr(track, "name", "Unknown") or "Unknown"
        artist_obj = getattr(track, "artist", None)
        artist = getattr(artist_obj, "name", "Unknown") if artist_obj else "Unknown"
        album_obj = getattr(track, "album", None)
        album = getattr(album_obj, "name", None) if album_obj else None
        duration = int(getattr(track, "duration", 0) or 0)
        quality = getattr(track, "audio_quality", "LOSSLESS") or "LOSSLESS"

        meta: TrackMeta = {
            "title": name,
            "artist": artist,
            "album": album,
            "duration": duration,
            "quality": quality,
            "image": None,
        }

        try:
            if album_obj and hasattr(album_obj, "cover") and album_obj.cover:
                uuid = album_obj.cover.replace("-", "/")
                meta["image"] = f"https://resources.tidal.com/images/{uuid}/640x640.jpg"
        except Exception:
            pass

        return meta

    def _format_duration(self, seconds: int) -> str:
        """Format duration in HH:MM:SS or MM:SS."""
        m, s = divmod(seconds, 60)
        return f"{m // 60}:{m % 60:02d}:{s:02d}" if m >= 60 else f"{m:02d}:{s:02d}"

    async def _get_player(self, ctx: commands.Context) -> Optional[Any]:
        """Get or create Lavalink player for guild."""
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
        """Load a Tidal track into Lavalink and queue it."""
        meta = self._extract_meta(tidal_track)
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
        """Send now playing embed with track metadata."""
        desc_parts = [f"**{meta['title']}**", meta['artist']]
        if meta.get("album"):
            desc_parts.append(f"_{meta['album']}_")

        embed = discord.Embed(
            title=Messages.STATUS_PLAYING,
            description="\n".join(desc_parts),
            color=discord.Color.blue(),
        )
        embed.add_field(
            name="Quality",
            value=QUALITY_LABELS.get(meta["quality"], "LOSSLESS"),
            inline=True,
        )
        embed.set_footer(text=f"Duration: {self._format_duration(meta['duration'])}")
        if meta.get("image"):
            embed.set_thumbnail(url=meta["image"])

        await ctx.send(embed=embed)

    async def _interactive_select(
        self, ctx: commands.Context, tracks: List[Any]
    ) -> Optional[Any]:
        """Show interactive track selection menu."""
        if not tracks:
            return None

        top = tracks[:5]
        desc = [
            f"**{i + 1}.** {t.name} - {getattr(t.artist, 'name', 'Unknown') if hasattr(t, 'artist') else 'Unknown'}"
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
        """Add reaction buttons to message."""
        try:
            reactions = list(REACTION_NUMBERS[:count]) + [CANCEL_EMOJI]
            for emoji in reactions:
                await msg.add_reaction(emoji)
        except Exception:
            pass

    async def _edit_progress_message(
        self, msg: discord.Message, embed: discord.Embed
    ) -> None:
        """Edit progress message with rate limiting."""
        now = asyncio.get_running_loop().time()
        if now - self._last_progress_edit.get(msg.id, 0.0) < PROGRESS_EDIT_RATELIMIT:
            return
        try:
            await msg.edit(embed=embed)
            self._last_progress_edit[msg.id] = now
        except Exception:
            pass

    async def _process_track_list(
        self,
        ctx: commands.Context,
        items: List[Any],
        name: str,
        item_processor: Callable[[Any], Any],
        color: discord.Color = discord.Color.blue(),
    ) -> None:
        """Process and queue a list of tracks with progress updates."""
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

                    if query and (hasattr(query, "id") or hasattr(query, "get_url") or hasattr(query, "get_stream")):
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
        """Verify all dependencies are ready."""
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

    # --- Command Handlers ---

    async def _handle_tidal_url(self, ctx: commands.Context, url: str) -> None:
        """Route Tidal URL to appropriate handler."""
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
        """Handle single track URL."""
        t = await self.tidal.get_track(tid)
        if t:
            await self._load_and_queue_track(ctx, t, show_embed=True)
        else:
            await ctx.send(Messages.ERROR_NO_TRACKS_FOUND)

    async def _handle_album(self, ctx: commands.Context, aid: str) -> None:
        """Handle album URL."""
        alb = await self.tidal.get_album(aid)
        if alb:
            tracks = await self.tidal.get_items(alb)
            await self._process_track_list(ctx, tracks, alb.name, lambda t: t)
        else:
            await ctx.send(Messages.ERROR_CONTENT_UNAVAILABLE)

    async def _handle_playlist(self, ctx: commands.Context, pid: str) -> None:
        """Handle playlist URL."""
        pl = await self.tidal.get_playlist(pid)
        if pl:
            tracks = await self.tidal.get_items(pl)
            await self._process_track_list(ctx, tracks, pl.name, lambda t: t)
        else:
            await ctx.send(Messages.ERROR_CONTENT_UNAVAILABLE)

    async def _handle_mix(self, ctx: commands.Context, mid: str) -> None:
        """Handle Tidal mix URL."""
        mix = await self.tidal.get_mix(mid)
        if mix:
            items = await self.tidal.get_items(mix)
            await self._process_track_list(
                ctx, items, f"Mix: {mid}", lambda t: t, discord.Color.purple()
            )
        else:
            await ctx.send(Messages.ERROR_CONTENT_UNAVAILABLE)

    # --- Commands ---

    @commands.cooldown(1, 5, commands.BucketType.user)
    @commands.command(name="tplay")
    async def tplay(self, ctx: commands.Context, *, query: str) -> None:
        """Play a track, album, playlist, or search query."""
        if not await self._check_ready(ctx):
            return

        if "tidal.com" in query:
            await self._handle_tidal_url(ctx, query)
            return

        if m := SPOTIFY_PLAYLIST_PATTERN.search(query):
            if not (SPOTIFY_AVAILABLE and self.sp):
                await ctx.send(Messages.ERROR_NO_SPOTIFY)
                return
            try:
                pl = await self._run_blocking_io(
                    lambda: self.sp.playlist(m.group(1)), timeout=20.0
                )
                await self._process_track_list(
                    ctx,
                    pl["tracks"]["items"],
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
                req = self.yt.playlistItems().list(
                    part="snippet", playlistId=m.group(1), maxResults=50
                )
                resp = await self._run_blocking_io(req.execute, timeout=20.0)
                await self._process_track_list(
                    ctx,
                    resp.get("items", []),
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

    @commands.command(name="tstop")
    async def tstop(self, ctx: commands.Context) -> None:
        """Stop current playlist queueing operation."""
        cancel_event = self._get_cancel_event(ctx.guild.id)
        cancel_event.set()
        await ctx.send(Messages.STATUS_STOPPING)

    @commands.command(name="tfilter")
    async def tfilter(self, ctx: commands.Context) -> None:
        """Toggle remix/TikTok filtering."""
        curr = await self.config.guild(ctx.guild).filter_remixes()
        await self.config.guild(ctx.guild).filter_remixes.set(not curr)
        await ctx.send(
            Messages.SUCCESS_FILTER_ENABLED if not curr else Messages.SUCCESS_FILTER_DISABLED
        )

    @commands.command(name="tinteractive")
    async def tinteractive(self, ctx: commands.Context) -> None:
        """Toggle interactive search mode."""
        curr = await self.config.guild(ctx.guild).interactive_search()
        await self.config.guild(ctx.guild).interactive_search.set(not curr)
        await ctx.send(
            Messages.SUCCESS_INTERACTIVE_ENABLED
            if not curr
            else Messages.SUCCESS_INTERACTIVE_DISABLED
        )

    @commands.is_owner()
    @commands.command(name="tdebug")
    async def tdebug(self, ctx: commands.Context) -> None:
        """Check Tidal connection status and versions."""
        tidal_status = "\u274c Not Connected"
        if await self.tidal.is_logged_in():
            tidal_status = "\u2705 Logged In"
        elif self.tidal.session:
            tidal_status = "\u26a0\ufe0f Session Invalid/Expired"

        lavalink_status = "\u274c Not Loaded"
        if LAVALINK_AVAILABLE:
            try:
                player = lavalink.get_player(ctx.guild.id)
                lavalink_status = f"\u2705 Loaded (Connected: {player.is_connected})"
            except Exception:
                lavalink_status = "\u26a0\ufe0f Loaded but no player found"

        try:
            tidal_ver = importlib.metadata.version("tidalapi")
        except Exception:
            tidal_ver = "Unknown"

        msg = (
            f"**TidalPlayer Debug**\n"
            f"**TidalAPI Version:** `{tidal_ver}`\n"
            f"**Initialized:** {'\u2705' if self._initialized else '\u23f3 Loading...'}\n"
            f"**Tidal Status:** {tidal_status}\n"
            f"**Lavalink Status:** {lavalink_status}\n"
            f"**YouTube API:** {'\u2705' if self.yt else '\u274c'}\n"
            f"**Spotify API:** {'\u2705' if self.sp else '\u274c'}"
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
            l, f = await self._run_blocking_io(session.login_oauth, timeout=60.0)
            e = discord.Embed(
                title="Tidal OAuth",
                description=f"[Click]({l.verification_uri_complete})",
                color=0x00B2FF,
            )
            try:
                await ctx.author.send(embed=e)
                await ctx.send("Check DMs.")
            except discord.Forbidden:
                await ctx.send(embed=e)

            try:
                await self._run_blocking_io(lambda: f.result(300), timeout=305.0)
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


async def setup(bot: Red):
    await bot.add_cog(TidalPlayer(bot))
