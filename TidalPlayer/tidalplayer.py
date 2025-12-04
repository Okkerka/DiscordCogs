from redbot.core import commands, Config
from redbot.core.utils.menus import menu, DEFAULT_CONTROLS
from redbot.core.bot import Red
import discord
import logging
import asyncio
import re
from collections import OrderedDict
from typing import Dict, List, Optional, Any, Callable, TypedDict, Union
from datetime import datetime
from functools import wraps
from contextlib import asynccontextmanager
import time

try:
    import lavalink
    LAVALINK_AVAILABLE = True
except ImportError:
    LAVALINK_AVAILABLE = False

try:
    import tidalapi
    TIDALAPI_AVAILABLE = True
except ImportError:
    TIDALAPI_AVAILABLE = False

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


# =============================================================================
# TYPE DEFINITIONS
# =============================================================================

class TrackMeta(TypedDict):
    """Type definition for track metadata"""
    title: str
    artist: str
    album: Optional[str]
    duration: int
    quality: str


class QueueResult(TypedDict):
    """Type definition for queue operation results"""
    queued: int
    skipped: int
    total: int


# =============================================================================
# CONSTANTS
# =============================================================================

MAX_QUEUE_SIZE = 1000
MAX_CACHE_SIZE = 500  # Limit cache to prevent memory bloat
BATCH_UPDATE_INTERVAL = 5
API_SEMAPHORE_LIMIT = 3
SEARCH_RETRY_ATTEMPTS = 3
COG_IDENTIFIER = 160819386
INTERACTIVE_TIMEOUT = 30
RETRY_BASE_DELAY = 0.5
RETRY_MAX_DELAY = 5.0
API_TIMEOUT = 30.0  # Timeout for API calls
CACHE_TTL = 300  # 5 minutes cache TTL for guild settings

REACTION_NUMBERS = ("1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣")  # Tuple for immutability
CANCEL_EMOJI = "❌"

QUALITY_LABELS: Dict[str, str] = {
    "HI_RES": "HI-RES (MQA)",
    "HI_RES_LOSSLESS": "HI-RES LOSSLESS",
    "LOSSLESS": "LOSSLESS (FLAC)",
    "HIGH": "HIGH (320kbps)",
    "LOW": "LOW (96kbps)"
}

FILTER_KEYWORDS = frozenset({  # Frozenset for O(1) lookups
    'sped up', 'slowed', 'tiktok',
    'reverb', '8d audio', 'bass boosted'
})

# URL patterns - compile once at module level
TIDAL_TRACK_PATTERN = re.compile(r"tidal\.com/(?:browse/)?track/(\d+)")
TIDAL_ALBUM_PATTERN = re.compile(r"tidal\.com/(?:browse/)?album/(\d+)")
TIDAL_PLAYLIST_PATTERN = re.compile(r"tidal\.com/(?:browse/)?playlist/([a-f0-9-]+)")
TIDAL_URL_PATTERN = re.compile(r"tidal\.com/(?:browse/)?(track|album|playlist)")
SPOTIFY_PLAYLIST_PATTERN = re.compile(r"open\.spotify\.com/playlist/([a-zA-Z0-9]+)")
YOUTUBE_PLAYLIST_PATTERN = re.compile(r"youtube\.com/.*[?&]list=([a-zA-Z0-9_-]+)")


# =============================================================================
# MESSAGES
# =============================================================================

class Messages:
    """Centralized message strings"""
    ERROR_NO_TIDALAPI = "tidalapi not installed. Run: `[p]pipinstall tidalapi`"
    ERROR_NOT_AUTHENTICATED = "Not authenticated. Run: `>tidalsetup`"
    ERROR_NO_AUDIO_COG = "Audio cog not loaded. Run: `[p]load audio`"
    ERROR_NO_LAVALINK = "Lavalink not available."
    ERROR_NO_PLAYER = "No active player. Join a voice channel first."
    ERROR_NO_TRACKS_FOUND = "No tracks found."
    ERROR_INVALID_URL = "Invalid {platform} {content_type} URL"
    ERROR_CONTENT_UNAVAILABLE = "Content unavailable (private/region-locked)"
    ERROR_NO_TRACKS_IN_CONTENT = "No tracks in {content_type}"
    ERROR_FETCH_FAILED = "Could not fetch playlist."
    ERROR_NO_SPOTIFY = "Spotify not configured. Run: `>tidalplay spotify <client_id> <client_secret>`"
    ERROR_NO_YOUTUBE = "YouTube not configured. Run: `>tidalplay youtube <api_key>`"
    ERROR_INSTALL_SPOTIFY = "Install spotipy: `pip install spotipy`"
    ERROR_INSTALL_YOUTUBE = "Install: `pip install google-api-python-client`"
    ERROR_TIMEOUT = "Selection timed out."
    ERROR_API_TIMEOUT = "API request timed out. Please try again."

    SUCCESS_QUEUED = "Queued {count} tracks from {name}"
    SUCCESS_PARTIAL_QUEUE = "Queued {queued}/{total} ({skipped} not found on Tidal)"
    SUCCESS_QUEUE_CLEARED = "Queue cleared."
    SUCCESS_TOKENS_CLEARED = "Tokens cleared. Run:\n1. `[p]pipinstall --force-reinstall tidalapi`\n2. Restart bot\n3. `>tidalsetup`"
    SUCCESS_TIDAL_SETUP = "Tidal setup complete!"
    SUCCESS_SPOTIFY_CONFIGURED = "Spotify configured."
    SUCCESS_YOUTUBE_CONFIGURED = "YouTube configured."
    SUCCESS_FILTER_ENABLED = "Remix/TikTok filter enabled."
    SUCCESS_FILTER_DISABLED = "Remix/TikTok filter disabled."
    SUCCESS_INTERACTIVE_ENABLED = "Interactive search enabled."
    SUCCESS_INTERACTIVE_DISABLED = "Interactive search disabled."

    STATUS_CHOOSE_TRACK = "React with a number to select a track, or {cancel} to cancel"
    PROGRESS_QUEUEING = "Queueing {name} ({count} tracks)..."
    PROGRESS_FETCHING_SPOTIFY = "Fetching Spotify playlist..."
    PROGRESS_FETCHING_YOUTUBE = "Fetching YouTube playlist..."
    PROGRESS_QUEUEING_SPOTIFY = "Queueing {count} tracks from Spotify..."
    PROGRESS_QUEUEING_YOUTUBE = "Queueing {count} videos from YouTube..."
    PROGRESS_UPDATE = "{queued} queued, {skipped} skipped ({current}/{total})"
    STATUS_STOPPING = "Stopping playlist queueing..."
    STATUS_CANCELLED = "Cancelled. {queued} queued, {skipped} skipped."
    STATUS_EMPTY_QUEUE = "Queue is empty."
    STATUS_PLAYING = "Playing from Tidal"


# =============================================================================
# EXCEPTIONS
# =============================================================================

class TidalPlayerError(Exception):
    """Base exception for TidalPlayer"""
    pass


class AuthenticationError(TidalPlayerError):
    """Authentication related errors"""
    pass


class APIError(TidalPlayerError):
    """API request errors"""
    pass


class RateLimitError(APIError):
    """Rate limit exceeded"""
    pass


# =============================================================================
# UTILITY CLASSES
# =============================================================================

class LRUCache(OrderedDict):
    """
    Thread-safe LRU cache with max size.
    More efficient than lru_cache for instance methods.
    """

    def __init__(self, maxsize: int = 128):
        super().__init__()
        self.maxsize = maxsize
        self._lock = asyncio.Lock()

    def get(self, key: Any, default: Any = None) -> Any:
        try:
            self.move_to_end(key)
            return self[key]
        except KeyError:
            return default

    def set(self, key: Any, value: Any) -> None:
        if key in self:
            self.move_to_end(key)
        self[key] = value
        if len(self) > self.maxsize:
            self.popitem(last=False)

    async def async_get(self, key: Any, default: Any = None) -> Any:
        async with self._lock:
            return self.get(key, default)

    async def async_set(self, key: Any, value: Any) -> None:
        async with self._lock:
            self.set(key, value)


class CachedGuildSettings:
    """Guild settings with TTL-based cache invalidation"""

    def __init__(self, settings: Dict[str, Any], timestamp: float):
        self.settings = settings
        self.timestamp = timestamp

    def is_valid(self, ttl: float = CACHE_TTL) -> bool:
        return (time.time() - self.timestamp) < ttl


# =============================================================================
# DECORATORS
# =============================================================================

def async_retry(
    max_attempts: int = SEARCH_RETRY_ATTEMPTS,
    base_delay: float = RETRY_BASE_DELAY,
    exceptions: tuple = (Exception,)
):
    """
    Retry decorator with exponential backoff.
    Only retries on specified exceptions.
    """
    def decorator(func: Callable) -> Callable:
        @wraps(func)
        async def wrapper(*args, **kwargs):
            last_exception = None
            for attempt in range(max_attempts):
                try:
                    return await func(*args, **kwargs)
                except exceptions as e:
                    last_exception = e
                    if attempt < max_attempts - 1:
                        delay = min(base_delay * (2 ** attempt), RETRY_MAX_DELAY)
                        log.warning(
                            f"Attempt {attempt + 1}/{max_attempts} failed for "
                            f"{func.__name__}: {type(e).__name__}: {e}"
                        )
                        await asyncio.sleep(delay)
            raise last_exception
        return wrapper
    return decorator


# =============================================================================
# CUSTOM TRACK CLASS
# =============================================================================

class TidalTrack:
    """
    Custom track wrapper that mimics lavalink.Track but with Tidal metadata.
    Uses __slots__ for memory efficiency.
    """

    __slots__ = (
        'track_identifier', 'uri', 'title', 'author', 'length',
        'requester', 'album', 'quality', 'seekable', 'is_stream',
        'position', 'thumbnail', 'extra', '_is_tidal_track'
    )

    def __init__(
        self,
        track_identifier: str,
        uri: str,
        title: str,
        author: str,
        length: int,
        requester: discord.Member,
        album: Optional[str] = None,
        quality: str = "LOSSLESS",
        **kwargs
    ):
        self.track_identifier = track_identifier
        self.uri = uri
        self.title = title
        self.author = author
        self.length = length
        self.requester = requester
        self.album = album
        self.quality = quality
        self.seekable = True
        self.is_stream = False
        self.position = 0
        self.thumbnail = None
        self.extra = kwargs
        self._is_tidal_track = True

    @classmethod
    def from_tidal(
        cls,
        tidal_track: Any,
        audio_url: str,
        requester: discord.Member,
        track_identifier: str
    ) -> "TidalTrack":
        """Create TidalTrack from tidalapi track object"""
        # Use getattr with defaults for safety
        title = getattr(tidal_track, "name", None) or "Unknown"

        artist = "Unknown"
        if hasattr(tidal_track, "artist") and tidal_track.artist:
            artist = getattr(tidal_track.artist, "name", None) or "Unknown"

        album = None
        if hasattr(tidal_track, "album") and tidal_track.album:
            album = getattr(tidal_track.album, "name", None)

        duration = getattr(tidal_track, "duration", 0) or 0
        quality = getattr(tidal_track, "audio_quality", "LOSSLESS") or "LOSSLESS"

        return cls(
            track_identifier=track_identifier,
            uri=audio_url,
            title=title,
            author=artist,
            length=int(duration) * 1000,  # Convert to ms
            requester=requester,
            album=album,
            quality=quality
        )

    def __repr__(self) -> str:
        return f"<TidalTrack title={self.title!r} author={self.author!r}>"


# =============================================================================
# MAIN COG
# =============================================================================

class TidalPlayer(commands.Cog):
    """Play music from Tidal with full metadata support."""

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
            track_metadata=[],
            cancel_queue=False,
            filter_remixes=True,
            interactive_search=False
        )

        # Session management
        self.session: Optional["tidalapi.Session"] = None
        if TIDALAPI_AVAILABLE:
            self.session = tidalapi.Session()

        self.sp: Optional["spotipy.Spotify"] = None
        self.yt: Optional[Any] = None

        # Concurrency control
        self.api_semaphore = asyncio.Semaphore(API_SEMAPHORE_LIMIT)

        # Optimized caches
        self._guild_settings_cache: Dict[int, CachedGuildSettings] = {}
        self._track_meta_cache: LRUCache = LRUCache(maxsize=MAX_CACHE_SIZE)
        self._quality_label_cache: Dict[str, str] = {}

        # Flags
        self._initialized = False

        # Start initialization
        self.bot.loop.create_task(self._initialize_apis())

    def cog_unload(self) -> None:
        """Cleanup on cog unload"""
        self._guild_settings_cache.clear()
        self._track_meta_cache.clear()
        self._quality_label_cache.clear()
        log.info("TidalPlayer cog unloaded - caches cleared")

    # =========================================================================
    # INITIALIZATION
    # =========================================================================

    async def _initialize_apis(self) -> None:
        """Initialize all API connections"""
        await self.bot.wait_until_ready()

        try:
            creds = await self.config.all()

            # Initialize in parallel for faster startup
            await asyncio.gather(
                self._initialize_tidal(creds),
                self._initialize_spotify(creds),
                self._initialize_youtube(creds),
                return_exceptions=True  # Don't fail if one fails
            )

            self._initialized = True
            log.info("TidalPlayer APIs initialized")

        except Exception as e:
            log.error(f"API initialization error: {e}", exc_info=True)

    async def _initialize_tidal(self, creds: Dict[str, Any]) -> None:
        """Initialize Tidal session"""
        if not TIDALAPI_AVAILABLE or not self.session:
            return

        required = ("token_type", "access_token", "refresh_token")
        if not all(creds.get(k) for k in required):
            log.debug("Tidal credentials incomplete, skipping auto-login")
            return

        try:
            expiry = creds.get("expiry_time")
            # Check if token is still valid
            if expiry and datetime.fromtimestamp(expiry) <= datetime.now():
                log.warning("Tidal token expired, requires re-authentication")
                return

            await asyncio.wait_for(
                self.bot.loop.run_in_executor(
                    None,
                    lambda: self.session.load_oauth_session(
                        creds["token_type"],
                        creds["access_token"],
                        creds["refresh_token"],
                        expiry
                    )
                ),
                timeout=API_TIMEOUT
            )

            if self.session.check_login():
                log.info("Tidal session loaded successfully")
            else:
                log.warning("Tidal session load returned but login check failed")

        except asyncio.TimeoutError:
            log.error("Tidal session load timed out")
        except Exception as e:
            log.error(f"Tidal session load failed: {e}")

    async def _initialize_spotify(self, creds: Dict[str, Any]) -> None:
        """Initialize Spotify client"""
        if not SPOTIFY_AVAILABLE:
            return

        client_id = creds.get("spotify_client_id")
        client_secret = creds.get("spotify_client_secret")

        if not (client_id and client_secret):
            return

        try:
            self.sp = spotipy.Spotify(
                client_credentials_manager=SpotifyClientCredentials(
                    client_id, client_secret
                )
            )
            # Test connection
            await asyncio.wait_for(
                self.bot.loop.run_in_executor(
                    None, lambda: self.sp.search("test", limit=1)
                ),
                timeout=API_TIMEOUT
            )
            log.info("Spotify client initialized")

        except asyncio.TimeoutError:
            log.error("Spotify initialization timed out")
            self.sp = None
        except Exception as e:
            log.error(f"Spotify initialization failed: {e}")
            self.sp = None

    async def _initialize_youtube(self, creds: Dict[str, Any]) -> None:
        """Initialize YouTube client"""
        if not YOUTUBE_API_AVAILABLE:
            return

        api_key = creds.get("youtube_api_key")
        if not api_key:
            return

        try:
            self.yt = build("youtube", "v3", developerKey=api_key)
            log.info("YouTube client initialized")
        except Exception as e:
            log.error(f"YouTube initialization failed: {e}")
            self.yt = None

    # =========================================================================
    # CACHING UTILITIES
    # =========================================================================

    async def _get_guild_settings(
        self, guild_id: int, force_refresh: bool = False
    ) -> Dict[str, Any]:
        """Get guild settings with TTL cache"""
        cached = self._guild_settings_cache.get(guild_id)

        if not force_refresh and cached and cached.is_valid():
            return cached.settings

        settings = await self.config.guild_from_id(guild_id).all()
        self._guild_settings_cache[guild_id] = CachedGuildSettings(
            settings, time.time()
        )
        return settings

    def _invalidate_guild_cache(self, guild_id: int) -> None:
        """Invalidate guild settings cache"""
        self._guild_settings_cache.pop(guild_id, None)

    def _get_quality_label(self, quality: str) -> str:
        """Get quality label with caching (replaces broken lru_cache)"""
        if quality not in self._quality_label_cache:
            self._quality_label_cache[quality] = QUALITY_LABELS.get(
                quality, "LOSSLESS (FLAC)"
            )
        return self._quality_label_cache[quality]

    # =========================================================================
    # METADATA UTILITIES
    # =========================================================================

    def _extract_meta(self, track: Any) -> TrackMeta:
        """Extract metadata from Tidal track object"""
        try:
            title = getattr(track, "name", None) or "Unknown"

            artist = "Unknown"
            if hasattr(track, "artist") and track.artist:
                artist = getattr(track.artist, "name", None) or "Unknown"

            album = None
            if hasattr(track, "album") and track.album:
                album = getattr(track.album, "name", None)

            duration = int(getattr(track, "duration", 0) or 0)
            quality = getattr(track, "audio_quality", "LOSSLESS") or "LOSSLESS"

            return TrackMeta(
                title=title,
                artist=artist,
                album=album,
                duration=duration,
                quality=quality
            )
        except Exception as e:
            log.error(f"Metadata extraction error: {e}")
            return TrackMeta(
                title="Unknown",
                artist="Unknown",
                album=None,
                duration=0,
                quality="LOSSLESS"
            )

    @staticmethod
    def _format_duration(seconds: int) -> str:
        """Format seconds to MM:SS or HH:MM:SS"""
        if seconds >= 3600:
            hours, remainder = divmod(seconds, 3600)
            minutes, secs = divmod(remainder, 60)
            return f"{hours}:{minutes:02d}:{secs:02d}"
        minutes, secs = divmod(seconds, 60)
        return f"{minutes:02d}:{secs:02d}"

    def _filter_tracks(self, tracks: List[Any]) -> List[Any]:
        """Filter out remixes/altered versions - optimized with frozenset"""
        if not tracks:
            return tracks

        filtered = [
            track for track in tracks
            if not any(
                kw in (getattr(track, "name", "") or "").lower()
                for kw in FILTER_KEYWORDS
            )
        ]
        return filtered if filtered else tracks

    # =========================================================================
    # METADATA STORAGE
    # =========================================================================

    async def _add_meta(self, guild_id: int, meta: TrackMeta) -> bool:
        """Add track metadata to guild queue"""
        try:
            async with self.config.guild_from_id(guild_id).track_metadata() as queue:
                if len(queue) >= MAX_QUEUE_SIZE:
                    log.warning(f"Queue full for guild {guild_id}")
                    return False
                queue.append(dict(meta))  # Convert TypedDict to regular dict
                return True
        except Exception as e:
            log.error(f"Add metadata error: {e}")
            return False

    async def _pop_meta(self, guild_id: int) -> None:
        """Remove first track from metadata queue"""
        try:
            async with self.config.guild_from_id(guild_id).track_metadata() as queue:
                if queue:
                    queue.pop(0)
        except Exception as e:
            log.error(f"Pop metadata error: {e}")

    async def _clear_meta(self, guild_id: int) -> None:
        """Clear all track metadata for guild"""
        try:
            await self.config.guild_from_id(guild_id).track_metadata.set([])
        except Exception as e:
            log.error(f"Clear metadata error: {e}")

    # =========================================================================
    # CANCEL FLAG MANAGEMENT
    # =========================================================================

    async def _should_cancel(self, guild_id: int) -> bool:
        """Check if queue operation should be cancelled"""
        try:
            settings = await self._get_guild_settings(guild_id)
            return settings.get("cancel_queue", False)
        except Exception:
            return False

    async def _set_cancel(self, guild_id: int, value: bool) -> None:
        """Set cancel flag for guild"""
        try:
            await self.config.guild_from_id(guild_id).cancel_queue.set(value)
            self._invalidate_guild_cache(guild_id)
        except Exception as e:
            log.error(f"Set cancel error: {e}")

    # =========================================================================
    # TIDAL API
    # =========================================================================

    @async_retry(exceptions=(APIError, asyncio.TimeoutError))
    async def _search_tidal(self, query: str, guild_id: int) -> List[Any]:
        """Search Tidal with rate limiting and retry"""
        async with self.api_semaphore:
            try:
                result = await asyncio.wait_for(
                    self.bot.loop.run_in_executor(
                        None, self.session.search, query
                    ),
                    timeout=API_TIMEOUT
                )

                tracks = result.get("tracks", [])

                settings = await self._get_guild_settings(guild_id)
                if settings.get("filter_remixes", True) and tracks:
                    tracks = self._filter_tracks(tracks)

                return tracks

            except asyncio.TimeoutError:
                log.error(f"Tidal search timed out for: {query}")
                raise
            except Exception as e:
                log.error(f"Tidal search failed for '{query}': {e}")
                raise APIError(f"Tidal search failed: {e}")

    async def _get_track_url(self, track: Any) -> Optional[str]:
        """Get audio URL from Tidal track with timeout"""
        try:
            return await asyncio.wait_for(
                self.bot.loop.run_in_executor(None, track.get_url),
                timeout=API_TIMEOUT
            )
        except asyncio.TimeoutError:
            log.error("Tidal URL fetch timed out")
            return None
        except Exception as e:
            log.error(f"Failed to get track URL: {e}")
            return None

    # =========================================================================
    # CHECKS
    # =========================================================================

    async def _check_ready(self, ctx: commands.Context) -> bool:
        """Check if cog is ready to handle commands"""
        if not TIDALAPI_AVAILABLE:
            await ctx.send(Messages.ERROR_NO_TIDALAPI)
            return False

        if not self.session or not self.session.check_login():
            await ctx.send(Messages.ERROR_NOT_AUTHENTICATED)
            return False

        if not self.bot.get_cog("Audio"):
            await ctx.send(Messages.ERROR_NO_AUDIO_COG)
            return False

        return True

    # =========================================================================
    # LAVALINK INTEGRATION
    # =========================================================================

    async def _get_player(self, ctx: commands.Context) -> Optional[Any]:
        """Get or create lavalink player"""
        if not LAVALINK_AVAILABLE:
            return None

        try:
            return lavalink.get_player(ctx.guild.id)
        except KeyError:
            # Try to create player via Audio cog
            if ctx.author.voice and ctx.author.voice.channel:
                try:
                    await lavalink.connect(ctx.author.voice.channel)
                    return lavalink.get_player(ctx.guild.id)
                except Exception as e:
                    log.debug(f"Could not create player: {e}")
            return None

    async def _load_and_queue_track(
        self,
        ctx: commands.Context,
        tidal_track: Any,
        show_embed: bool = True
    ) -> bool:
        """Load Tidal track and add to queue with metadata"""
        try:
            meta = self._extract_meta(tidal_track)

            # Get audio URL
            url = await self._get_track_url(tidal_track)
            if not url:
                log.error(f"Failed to get URL for: {meta['title']}")
                return False

            # Try direct lavalink first
            player = await self._get_player(ctx)
            if player:
                try:
                    result = await asyncio.wait_for(
                        player.load_tracks(url),
                        timeout=API_TIMEOUT
                    )

                    if result and result.tracks:
                        ll_track = result.tracks[0]

                        custom_track = TidalTrack.from_tidal(
                            tidal_track=tidal_track,
                            audio_url=url,
                            requester=ctx.author,
                            track_identifier=ll_track.track_identifier
                        )

                        self._track_meta_cache.set(url, meta)
                        player.add(ctx.author, custom_track)

                        if not player.current:
                            await player.play()

                        if show_embed:
                            await self._send_now_playing(ctx, meta)

                        await self._add_meta(ctx.guild.id, meta)
                        return True

                except asyncio.TimeoutError:
                    log.warning(f"Lavalink load timed out for: {meta['title']}")
                except Exception as e:
                    log.warning(f"Direct lavalink failed: {e}")

            # Fallback to Audio cog
            return await self._fallback_play(ctx, meta, url, show_embed)

        except Exception as e:
            log.error(f"Track load error: {e}", exc_info=True)
            return False

    async def _fallback_play(
        self,
        ctx: commands.Context,
        meta: TrackMeta,
        url: str,
        show_embed: bool
    ) -> bool:
        """Fallback to Audio cog for playback"""
        try:
            if show_embed:
                await self._send_now_playing(ctx, meta)

            await self._add_meta(ctx.guild.id, meta)
            self._track_meta_cache.set(url, meta)

            audio_cog = self.bot.get_cog("Audio")
            if not audio_cog:
                return False

            # Suppress Audio cog message
            original_send = ctx.send

            async def filtered_send(*args, **kwargs):
                embed = kwargs.get("embed")
                if not embed:
                    embed = args[0] if args and isinstance(args[0], discord.Embed) else None
                if embed and "Track Enqueued" in (getattr(embed, "title", "") or ""):
                    return None
                return await original_send(*args, **kwargs)

            ctx.send = filtered_send
            try:
                await audio_cog.command_play(ctx, query=url)
                return True
            finally:
                ctx.send = original_send

        except Exception as e:
            log.error(f"Fallback play error: {e}")
            return False

    async def _send_now_playing(self, ctx: commands.Context, meta: TrackMeta) -> None:
        """Send now playing embed"""
        description = f"**{meta['title']}** • {meta['artist']}"
        if meta.get("album"):
            description += f"\n_{meta['album']}_"

        embed = discord.Embed(
            title=Messages.STATUS_PLAYING,
            description=description,
            color=discord.Color.blue()
        )
        embed.add_field(
            name="Quality",
            value=self._get_quality_label(meta["quality"]),
            inline=True
        )
        embed.set_footer(text=f"Duration: {self._format_duration(meta['duration'])}")
        await ctx.send(embed=embed)

    # =========================================================================
    # PLAY METHODS
    # =========================================================================

    async def _play(
        self, ctx: commands.Context, track: Any, show_embed: bool = True
    ) -> bool:
        """Main play method"""
        return await self._load_and_queue_track(ctx, track, show_embed)

    async def _search_and_queue(
        self, ctx: commands.Context, query: str, track_name: str
    ) -> bool:
        """Search and queue a track by query"""
        try:
            tracks = await self._search_tidal(query, ctx.guild.id)
            if not tracks:
                log.debug(f"No Tidal match for: {track_name}")
                return False
            return await self._play(ctx, tracks[0], show_embed=False)
        except Exception as e:
            log.error(f"Search and queue failed for '{track_name}': {e}")
            return False

    # =========================================================================
    # INTERACTIVE SELECTION
    # =========================================================================

    async def _interactive_select(
        self, ctx: commands.Context, tracks: List[Any]
    ) -> Optional[Any]:
        """Reaction-based track selection"""
        if not tracks:
            return None

        results_to_show = min(5, len(tracks))
        lines = []

        for i, track in enumerate(tracks[:results_to_show]):
            meta = self._extract_meta(track)
            duration = self._format_duration(meta["duration"])
            lines.append(
                f"{REACTION_NUMBERS[i]} **{meta['title']}** - {meta['artist']} ({duration})"
            )

        embed = discord.Embed(
            title="Search Results",
            description="\n".join(lines),
            color=discord.Color.blue()
        )
        embed.set_footer(
            text=Messages.STATUS_CHOOSE_TRACK.format(cancel=CANCEL_EMOJI)
        )

        msg = await ctx.send(embed=embed)

        # Add reactions
        for i in range(results_to_show):
            await msg.add_reaction(REACTION_NUMBERS[i])
        await msg.add_reaction(CANCEL_EMOJI)

        def check(reaction: discord.Reaction, user: discord.User) -> bool:
            return (
                user == ctx.author
                and reaction.message.id == msg.id
                and (
                    str(reaction.emoji) in REACTION_NUMBERS[:results_to_show]
                    or str(reaction.emoji) == CANCEL_EMOJI
                )
            )

        try:
            reaction, _ = await self.bot.wait_for(
                'reaction_add', check=check, timeout=INTERACTIVE_TIMEOUT
            )

            await msg.delete()

            if str(reaction.emoji) == CANCEL_EMOJI:
                await ctx.send("Cancelled.")
                return None

            choice = REACTION_NUMBERS.index(str(reaction.emoji))
            return tracks[choice]

        except asyncio.TimeoutError:
            await msg.delete()
            await ctx.send(Messages.ERROR_TIMEOUT)
            return None

    # =========================================================================
    # PLAYLIST QUEUEING
    # =========================================================================

    async def _queue_playlist_batch(
        self,
        ctx: commands.Context,
        tracks: List[Any],
        playlist_name: str,
        progress_msg: Optional[discord.Message] = None
    ) -> QueueResult:
        """Queue multiple tracks with progress updates"""
        queued = 0
        skipped = 0
        total = len(tracks)
        last_update = 0

        try:
            for i, track in enumerate(tracks, 1):
                if await self._should_cancel(ctx.guild.id):
                    break

                try:
                    if await self._play(ctx, track, show_embed=False):
                        queued += 1
                    else:
                        skipped += 1

                    # Batch progress updates
                    if progress_msg and (i - last_update >= BATCH_UPDATE_INTERVAL or i == total):
                        try:
                            embed = discord.Embed(
                                title=Messages.PROGRESS_QUEUEING.format(
                                    name=playlist_name, count=total
                                ),
                                description=Messages.PROGRESS_UPDATE.format(
                                    queued=queued, skipped=skipped,
                                    current=i, total=total
                                ),
                                color=discord.Color.blue()
                            )
                            await progress_msg.edit(embed=embed)
                            last_update = i
                        except discord.HTTPException:
                            pass

                    # Small delay to prevent rate limiting
                    await asyncio.sleep(0.05)

                except Exception as e:
                    log.error(f"Error queueing track {i}/{total}: {e}")
                    skipped += 1

            return QueueResult(queued=queued, skipped=skipped, total=total)

        finally:
            await self._set_cancel(ctx.guild.id, False)

    async def _queue_spotify_playlist(
        self, ctx: commands.Context, playlist_id: str
    ) -> None:
        """Queue tracks from Spotify playlist"""
        if not SPOTIFY_AVAILABLE:
            await ctx.send(Messages.ERROR_INSTALL_SPOTIFY)
            return

        if not self.sp:
            await ctx.send(Messages.ERROR_NO_SPOTIFY)
            return

        progress_msg = await ctx.send(Messages.PROGRESS_FETCHING_SPOTIFY)

        try:
            # Fetch playlist with timeout
            playlist = await asyncio.wait_for(
                self.bot.loop.run_in_executor(None, self.sp.playlist, playlist_id),
                timeout=API_TIMEOUT
            )

            if not playlist or "tracks" not in playlist:
                await progress_msg.edit(content=Messages.ERROR_FETCH_FAILED)
                return

            tracks = playlist["tracks"]["items"]
            if not tracks:
                await progress_msg.edit(
                    content=Messages.ERROR_NO_TRACKS_IN_CONTENT.format(
                        content_type="playlist"
                    )
                )
                return

            playlist_name = playlist.get("name", "Spotify Playlist")

            embed = discord.Embed(
                title=Messages.PROGRESS_QUEUEING_SPOTIFY.format(count=len(tracks)),
                description=f"Playlist: {playlist_name}",
                color=discord.Color.green()
            )
            await progress_msg.edit(embed=embed)

            queued = 0
            skipped = 0
            last_update = 0

            for i, item in enumerate(tracks, 1):
                if await self._should_cancel(ctx.guild.id):
                    embed = discord.Embed(
                        title=Messages.STATUS_STOPPING,
                        description=Messages.STATUS_CANCELLED.format(
                            queued=queued, skipped=skipped
                        ),
                        color=discord.Color.orange()
                    )
                    await progress_msg.edit(embed=embed)
                    return

                track = item.get("track")
                if not track:
                    skipped += 1
                    continue

                try:
                    track_name = track.get("name", "")
                    artists = track.get("artists", [])
                    artist_name = artists[0].get("name", "") if artists else ""
                    query = f"{track_name} {artist_name}".strip()

                    if query and await self._search_and_queue(ctx, query, track_name):
                        queued += 1
                    else:
                        skipped += 1

                    if i - last_update >= BATCH_UPDATE_INTERVAL or i == len(tracks):
                        embed = discord.Embed(
                            title=Messages.PROGRESS_QUEUEING_SPOTIFY.format(
                                count=len(tracks)
                            ),
                            description=Messages.PROGRESS_UPDATE.format(
                                queued=queued, skipped=skipped,
                                current=i, total=len(tracks)
                            ),
                            color=discord.Color.green()
                        )
                        await progress_msg.edit(embed=embed)
                        last_update = i

                    await asyncio.sleep(0.05)

                except Exception as e:
                    log.error(f"Spotify track {i} error: {e}")
                    skipped += 1

            embed = discord.Embed(
                title=Messages.SUCCESS_PARTIAL_QUEUE.format(
                    queued=queued, total=len(tracks), skipped=skipped
                ),
                description=f"Playlist: {playlist_name}",
                color=discord.Color.green()
            )
            await progress_msg.edit(embed=embed)

        except asyncio.TimeoutError:
            await progress_msg.edit(content=Messages.ERROR_API_TIMEOUT)
        except Exception as e:
            log.error(f"Spotify playlist error: {e}", exc_info=True)
            await progress_msg.edit(content=Messages.ERROR_FETCH_FAILED)
        finally:
            await self._set_cancel(ctx.guild.id, False)

    async def _queue_youtube_playlist(
        self, ctx: commands.Context, playlist_id: str
    ) -> None:
        """Queue tracks from YouTube playlist"""
        if not YOUTUBE_API_AVAILABLE:
            await ctx.send(Messages.ERROR_INSTALL_YOUTUBE)
            return

        if not self.yt:
            await ctx.send(Messages.ERROR_NO_YOUTUBE)
            return

        progress_msg = await ctx.send(Messages.PROGRESS_FETCHING_YOUTUBE)

        try:
            request = self.yt.playlistItems().list(
                part="snippet",
                playlistId=playlist_id,
                maxResults=50
            )

            response = await asyncio.wait_for(
                self.bot.loop.run_in_executor(None, request.execute),
                timeout=API_TIMEOUT
            )

            if not response or "items" not in response:
                await progress_msg.edit(content=Messages.ERROR_FETCH_FAILED)
                return

            items = response["items"]
            if not items:
                await progress_msg.edit(
                    content=Messages.ERROR_NO_TRACKS_IN_CONTENT.format(
                        content_type="playlist"
                    )
                )
                return

            playlist_title = items[0].get("snippet", {}).get(
                "playlistTitle", "YouTube Playlist"
            )

            embed = discord.Embed(
                title=Messages.PROGRESS_QUEUEING_YOUTUBE.format(count=len(items)),
                description=f"Playlist: {playlist_title}",
                color=discord.Color.red()
            )
            await progress_msg.edit(embed=embed)

            queued = 0
            skipped = 0
            last_update = 0

            for i, item in enumerate(items, 1):
                if await self._should_cancel(ctx.guild.id):
                    embed = discord.Embed(
                        title=Messages.STATUS_STOPPING,
                        description=Messages.STATUS_CANCELLED.format(
                            queued=queued, skipped=skipped
                        ),
                        color=discord.Color.orange()
                    )
                    await progress_msg.edit(embed=embed)
                    return

                try:
                    video_title = item.get("snippet", {}).get("title", "")

                    if video_title and await self._search_and_queue(
                        ctx, video_title, video_title
                    ):
                        queued += 1
                    else:
                        skipped += 1

                    if i - last_update >= BATCH_UPDATE_INTERVAL or i == len(items):
                        embed = discord.Embed(
                            title=Messages.PROGRESS_QUEUEING_YOUTUBE.format(
                                count=len(items)
                            ),
                            description=Messages.PROGRESS_UPDATE.format(
                                queued=queued, skipped=skipped,
                                current=i, total=len(items)
                            ),
                            color=discord.Color.red()
                        )
                        await progress_msg.edit(embed=embed)
                        last_update = i

                    await asyncio.sleep(0.05)

                except Exception as e:
                    log.error(f"YouTube video {i} error: {e}")
                    skipped += 1

            embed = discord.Embed(
                title=Messages.SUCCESS_PARTIAL_QUEUE.format(
                    queued=queued, total=len(items), skipped=skipped
                ),
                description=f"Playlist: {playlist_title}",
                color=discord.Color.red()
            )
            await progress_msg.edit(embed=embed)

        except asyncio.TimeoutError:
            await progress_msg.edit(content=Messages.ERROR_API_TIMEOUT)
        except Exception as e:
            log.error(f"YouTube playlist error: {e}", exc_info=True)
            await progress_msg.edit(content=Messages.ERROR_FETCH_FAILED)
        finally:
            await self._set_cancel(ctx.guild.id, False)

    # =========================================================================
    # URL HANDLERS
    # =========================================================================

    async def _handle_tidal_url(self, ctx: commands.Context, url: str) -> None:
        """Handle Tidal URL (track/album/playlist)"""
        track_match = TIDAL_TRACK_PATTERN.search(url)
        album_match = TIDAL_ALBUM_PATTERN.search(url)
        playlist_match = TIDAL_PLAYLIST_PATTERN.search(url)

        try:
            if track_match:
                await self._handle_tidal_track(ctx, track_match.group(1))
            elif album_match:
                await self._handle_tidal_album(ctx, album_match.group(1))
            elif playlist_match:
                await self._handle_tidal_playlist(ctx, playlist_match.group(1))
            else:
                await ctx.send(Messages.ERROR_INVALID_URL.format(
                    platform="Tidal", content_type="track/album/playlist"
                ))
        except Exception as e:
            log.error(f"Tidal URL error: {e}", exc_info=True)
            await ctx.send(Messages.ERROR_CONTENT_UNAVAILABLE)

    async def _handle_tidal_track(self, ctx: commands.Context, track_id: str) -> None:
        """Handle single Tidal track"""
        try:
            track = await asyncio.wait_for(
                self.bot.loop.run_in_executor(
                    None, self.session.track, track_id
                ),
                timeout=API_TIMEOUT
            )
            if track:
                await self._play(ctx, track)
            else:
                await ctx.send(Messages.ERROR_NO_TRACKS_FOUND)
        except asyncio.TimeoutError:
            await ctx.send(Messages.ERROR_API_TIMEOUT)

    async def _handle_tidal_album(self, ctx: commands.Context, album_id: str) -> None:
        """Handle Tidal album"""
        try:
            album = await asyncio.wait_for(
                self.bot.loop.run_in_executor(None, self.session.album, album_id),
                timeout=API_TIMEOUT
            )

            if not album:
                await ctx.send(Messages.ERROR_CONTENT_UNAVAILABLE)
                return

            tracks = await asyncio.wait_for(
                self.bot.loop.run_in_executor(None, album.tracks),
                timeout=API_TIMEOUT
            )

            if not tracks:
                await ctx.send(Messages.ERROR_NO_TRACKS_IN_CONTENT.format(
                    content_type="album"
                ))
                return

            album_name = getattr(album, "name", "Unknown Album")
            progress_msg = await ctx.send(
                Messages.PROGRESS_QUEUEING.format(name=album_name, count=len(tracks))
            )

            result = await self._queue_playlist_batch(
                ctx, tracks, album_name, progress_msg
            )

            embed = discord.Embed(
                title=Messages.SUCCESS_PARTIAL_QUEUE.format(
                    queued=result["queued"],
                    total=result["total"],
                    skipped=result["skipped"]
                ),
                description=f"Album: {album_name}",
                color=discord.Color.blue()
            )
            await progress_msg.edit(embed=embed)

        except asyncio.TimeoutError:
            await ctx.send(Messages.ERROR_API_TIMEOUT)

    async def _handle_tidal_playlist(
        self, ctx: commands.Context, playlist_id: str
    ) -> None:
        """Handle Tidal playlist"""
        try:
            playlist = await asyncio.wait_for(
                self.bot.loop.run_in_executor(
                    None, self.session.playlist, playlist_id
                ),
                timeout=API_TIMEOUT
            )

            if not playlist:
                await ctx.send(Messages.ERROR_CONTENT_UNAVAILABLE)
                return

            tracks = await asyncio.wait_for(
                self.bot.loop.run_in_executor(None, playlist.tracks),
                timeout=API_TIMEOUT
            )

            if not tracks:
                await ctx.send(Messages.ERROR_NO_TRACKS_IN_CONTENT.format(
                    content_type="playlist"
                ))
                return

            playlist_name = getattr(playlist, "name", "Unknown Playlist")
            progress_msg = await ctx.send(
                Messages.PROGRESS_QUEUEING.format(
                    name=playlist_name, count=len(tracks)
                )
            )

            result = await self._queue_playlist_batch(
                ctx, tracks, playlist_name, progress_msg
            )

            embed = discord.Embed(
                title=Messages.SUCCESS_PARTIAL_QUEUE.format(
                    queued=result["queued"],
                    total=result["total"],
                    skipped=result["skipped"]
                ),
                description=f"Playlist: {playlist_name}",
                color=discord.Color.blue()
            )
            await progress_msg.edit(embed=embed)

        except asyncio.TimeoutError:
            await ctx.send(Messages.ERROR_API_TIMEOUT)

    # =========================================================================
    # COMMANDS
    # =========================================================================

    @commands.command(name="tplay")
    async def tplay(self, ctx: commands.Context, *, query: str) -> None:
        """Play from Tidal, Spotify playlist, or YouTube playlist."""
        if not await self._check_ready(ctx):
            return

        # Check URL patterns (using pre-compiled regexes)
        if TIDAL_URL_PATTERN.search(query):
            await self._handle_tidal_url(ctx, query)
        elif match := SPOTIFY_PLAYLIST_PATTERN.search(query):
            await self._queue_spotify_playlist(ctx, match.group(1))
        elif match := YOUTUBE_PLAYLIST_PATTERN.search(query):
            await self._queue_youtube_playlist(ctx, match.group(1))
        else:
            # Search mode
            try:
                tracks = await self._search_tidal(query, ctx.guild.id)

                if not tracks:
                    await ctx.send(Messages.ERROR_NO_TRACKS_FOUND)
                    return

                settings = await self._get_guild_settings(ctx.guild.id)

                if settings.get("interactive_search", False):
                    selected = await self._interactive_select(ctx, tracks)
                    if selected:
                        await self._play(ctx, selected)
                else:
                    await self._play(ctx, tracks[0])

            except APIError as e:
                await ctx.send(f"{Messages.ERROR_NO_TRACKS_FOUND} ({e})")
            except Exception as e:
                log.error(f"Search error: {e}", exc_info=True)
                await ctx.send(Messages.ERROR_NO_TRACKS_FOUND)

    @commands.command(name="tstop")
    async def tstop(self, ctx: commands.Context) -> None:
        """Stop current playlist queueing."""
        await self._set_cancel(ctx.guild.id, True)
        await ctx.send(Messages.STATUS_STOPPING)

    @commands.command(name="tqueue")
    async def tqueue(self, ctx: commands.Context) -> None:
        """Display the queue with Tidal metadata."""
        try:
            # Try lavalink player first
            player = None
            if LAVALINK_AVAILABLE:
                try:
                    player = lavalink.get_player(ctx.guild.id)
                except KeyError:
                    pass

            queue_items: List[Dict[str, Any]] = []

            if player and player.queue:
                for track in player.queue:
                    if getattr(track, '_is_tidal_track', False):
                        queue_items.append({
                            "title": track.title,
                            "artist": track.author,
                            "duration": track.length // 1000,
                            "quality": getattr(track, 'quality', 'LOSSLESS'),
                            "album": getattr(track, 'album', None)
                        })
                    elif cached := self._track_meta_cache.get(track.uri):
                        queue_items.append(cached)
                    else:
                        queue_items.append({
                            "title": getattr(track, 'title', 'Unknown'),
                            "artist": getattr(track, 'author', 'Unknown'),
                            "duration": getattr(track, 'length', 0) // 1000,
                            "quality": "UNKNOWN",
                            "album": None
                        })
            else:
                queue_items = await self.config.guild(ctx.guild).track_metadata()

            if not queue_items:
                await ctx.send(Messages.STATUS_EMPTY_QUEUE)
                return

            # Paginate
            pages = []
            items_per_page = 10

            for i in range(0, len(queue_items), items_per_page):
                chunk = queue_items[i:i + items_per_page]
                lines = []

                for j, meta in enumerate(chunk, start=i + 1):
                    duration = self._format_duration(meta.get("duration", 0))
                    quality = self._get_quality_label(meta.get("quality", "LOSSLESS"))
                    lines.append(
                        f"**{j}.** {meta.get('title', 'Unknown')}\n"
                        f"    {meta.get('artist', 'Unknown')} • {duration} • {quality}"
                    )

                embed = discord.Embed(
                    title=f"Tidal Queue ({len(queue_items)} tracks)",
                    description="\n".join(lines),
                    color=discord.Color.blue()
                )

                if player and player.current:
                    current_title = getattr(player.current, 'title', 'Unknown')
                    current_author = getattr(player.current, 'author', 'Unknown')
                    embed.add_field(
                        name="Now Playing",
                        value=f"{current_title} - {current_author}",
                        inline=False
                    )

                total_pages = (len(queue_items) - 1) // items_per_page + 1
                embed.set_footer(text=f"Page {len(pages) + 1}/{total_pages}")
                pages.append(embed)

            if len(pages) == 1:
                await ctx.send(embed=pages[0])
            else:
                await menu(ctx, pages, DEFAULT_CONTROLS)

        except Exception as e:
            log.error(f"Queue display error: {e}", exc_info=True)
            await ctx.send("Error displaying queue.")

    @commands.command(name="tclear")
    async def tclear(self, ctx: commands.Context) -> None:
        """Clear the Tidal metadata queue."""
        await self._clear_meta(ctx.guild.id)
        self._track_meta_cache.clear()
        self._invalidate_guild_cache(ctx.guild.id)
        await ctx.send(Messages.SUCCESS_QUEUE_CLEARED)

    @commands.command(name="tfilter")
    async def tfilter(self, ctx: commands.Context) -> None:
        """Toggle remix/TikTok filtering."""
        current = await self.config.guild(ctx.guild).filter_remixes()
        new_value = not current
        await self.config.guild(ctx.guild).filter_remixes.set(new_value)
        self._invalidate_guild_cache(ctx.guild.id)

        msg = Messages.SUCCESS_FILTER_ENABLED if new_value else Messages.SUCCESS_FILTER_DISABLED
        await ctx.send(msg)

    @commands.command(name="tinteractive")
    async def tinteractive(self, ctx: commands.Context) -> None:
        """Toggle interactive search mode."""
        current = await self.config.guild(ctx.guild).interactive_search()
        new_value = not current
        await self.config.guild(ctx.guild).interactive_search.set(new_value)
        self._invalidate_guild_cache(ctx.guild.id)

        msg = Messages.SUCCESS_INTERACTIVE_ENABLED if new_value else Messages.SUCCESS_INTERACTIVE_DISABLED
        await ctx.send(msg)

    @commands.is_owner()
    @commands.command(name="tidalsetup")
    async def tidalsetup(self, ctx: commands.Context) -> None:
        """Set up Tidal OAuth authentication."""
        if not TIDALAPI_AVAILABLE:
            await ctx.send(Messages.ERROR_NO_TIDALAPI)
            return

        try:
            login, future = self.session.login_oauth()

            embed = discord.Embed(
                title="Tidal OAuth Setup",
                description="Click the link below to authenticate:",
                color=discord.Color.blue()
            )
            embed.add_field(
                name="Login URL",
                value=f"[Click here]({login.verification_uri_complete})",
                inline=False
            )
            embed.add_field(
                name="Manual Code",
                value=f"Code: `{login.user_code}`\nURL: {login.verification_uri}",
                inline=False
            )
            embed.set_footer(text="You have 5 minutes to complete login")

            try:
                await ctx.author.send(embed=embed)
                await ctx.send("OAuth link sent to your DMs.")
            except discord.Forbidden:
                await ctx.send(embed=embed)

            log.info(f"[TIDAL OAuth] URL: {login.verification_uri_complete}")

            await self.bot.loop.run_in_executor(None, future.result, 300)

            if self.session.check_login():
                await self.config.token_type.set(self.session.token_type)
                await self.config.access_token.set(self.session.access_token)
                await self.config.refresh_token.set(self.session.refresh_token)

                expiry = None
                if self.session.expiry_time:
                    expiry = int(self.session.expiry_time.timestamp())
                await self.config.expiry_time.set(expiry)

                await ctx.send(Messages.SUCCESS_TIDAL_SETUP)
                log.info("Tidal OAuth successful")
            else:
                await ctx.send("Login failed. Please try again.")

        except Exception as e:
            log.error(f"OAuth error: {e}", exc_info=True)
            await ctx.send(f"Setup failed: {e}")

    @commands.is_owner()
    @commands.group(name="tidalplay")
    async def tidalplay(self, ctx: commands.Context) -> None:
        """Tidal configuration commands."""
        if ctx.invoked_subcommand is None:
            await ctx.send_help(ctx.command)

    @tidalplay.command(name="spotify")
    async def tidalplay_spotify(
        self, ctx: commands.Context, client_id: str, client_secret: str
    ) -> None:
        """Configure Spotify integration."""
        if not SPOTIFY_AVAILABLE:
            await ctx.send(Messages.ERROR_INSTALL_SPOTIFY)
            return

        try:
            await self.config.spotify_client_id.set(client_id)
            await self.config.spotify_client_secret.set(client_secret)

            self.sp = spotipy.Spotify(
                client_credentials_manager=SpotifyClientCredentials(
                    client_id, client_secret
                )
            )

            # Test connection
            await self.bot.loop.run_in_executor(
                None, lambda: self.sp.search("test", limit=1)
            )

            await ctx.send(Messages.SUCCESS_SPOTIFY_CONFIGURED)

        except Exception as e:
            log.error(f"Spotify config error: {e}", exc_info=True)
            await ctx.send(f"Spotify setup failed: {e}")
            self.sp = None

    @tidalplay.command(name="youtube")
    async def tidalplay_youtube(self, ctx: commands.Context, api_key: str) -> None:
        """Configure YouTube integration."""
        if not YOUTUBE_API_AVAILABLE:
            await ctx.send(Messages.ERROR_INSTALL_YOUTUBE)
            return

        try:
            await self.config.youtube_api_key.set(api_key)
            self.yt = build("youtube", "v3", developerKey=api_key)
            await ctx.send(Messages.SUCCESS_YOUTUBE_CONFIGURED)

        except Exception as e:
            log.error(f"YouTube config error: {e}", exc_info=True)
            await ctx.send(f"YouTube setup failed: {e}")
            self.yt = None

    @tidalplay.command(name="cleartokens")
    async def tidalplay_cleartokens(self, ctx: commands.Context) -> None:
        """Clear stored Tidal tokens."""
        await self.config.token_type.set(None)
        await self.config.access_token.set(None)
        await self.config.refresh_token.set(None)
        await self.config.expiry_time.set(None)
        await ctx.send(Messages.SUCCESS_TOKENS_CLEARED)

    # =========================================================================
    # EVENT LISTENERS
    # =========================================================================

    @commands.Cog.listener()
    async def on_red_audio_track_start(
        self, guild: discord.Guild, track: Any, requester: discord.Member
    ) -> None:
        """Handle track start - pop metadata from queue"""
        try:
            await self._pop_meta(guild.id)
        except Exception as e:
            log.error(f"Track start event error: {e}")

    @commands.Cog.listener()
    async def on_red_audio_queue_end(
        self, guild: discord.Guild, track_count: int, total_duration: int
    ) -> None:
        """Handle queue end - clear metadata"""
        try:
            await self._clear_meta(guild.id)
            self._invalidate_guild_cache(guild.id)
        except Exception as e:
            log.error(f"Queue end event error: {e}")


# =============================================================================
# SETUP
# =============================================================================

async def setup(bot: Red) -> None:
    """Add cog to bot"""
    await bot.add_cog(TidalPlayer(bot))
