"""
TidalPlayer - Tidal music integration for Red Discord Bot
Metadata Injection Version - Fixes [p]queue display
"""

import asyncio
import logging
import re
import time
from collections import OrderedDict
from datetime import datetime
from functools import wraps
from typing import Any, Callable, Dict, List, Optional, TypedDict

import discord
from redbot.core import Config, commands
from redbot.core.bot import Red
from redbot.core.utils.menus import DEFAULT_CONTROLS, menu

# We NEED lavalink for this fix
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
# CONSTANTS
# =============================================================================

COG_IDENTIFIER = 160819386
MAX_QUEUE_SIZE = 1000
MAX_CACHE_SIZE = 500
API_SEMAPHORE_LIMIT = 3
SEARCH_RETRY_ATTEMPTS = 3
API_TIMEOUT = 30.0
CACHE_TTL = 300
BATCH_UPDATE_INTERVAL = 5
INTERACTIVE_TIMEOUT = 30
RETRY_BASE_DELAY = 0.5
RETRY_MAX_DELAY = 5.0

REACTION_NUMBERS = ("1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣")
CANCEL_EMOJI = "❌"

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

# Regex Patterns
TIDAL_TRACK_PATTERN = re.compile(r"tidal\.com/(?:browse/)?track/(\d+)")
TIDAL_ALBUM_PATTERN = re.compile(r"tidal\.com/(?:browse/)?album/(\d+)")
TIDAL_PLAYLIST_PATTERN = re.compile(r"tidal\.com/(?:browse/)?playlist/([a-f0-9-]+)")
TIDAL_URL_PATTERN = re.compile(r"tidal\.com/(?:browse/)?(track|album|playlist)")
SPOTIFY_PLAYLIST_PATTERN = re.compile(r"open\.spotify\.com/playlist/([a-zA-Z0-9]+)")
YOUTUBE_PLAYLIST_PATTERN = re.compile(r"youtube\.com/.*[?&]list=([a-zA-Z0-9_-]+)")


# =============================================================================
# TYPES & MESSAGES
# =============================================================================


class TrackMeta(TypedDict):
    title: str
    artist: str
    album: Optional[str]
    duration: int
    quality: str


class QueueResult(TypedDict):
    queued: int
    skipped: int
    total: int


class Messages:
    ERROR_NO_TIDALAPI = "tidalapi not installed. Run: `[p]pipinstall tidalapi`"
    ERROR_NOT_AUTHENTICATED = "Not authenticated. Run: `>tidalsetup`"
    ERROR_NO_AUDIO_COG = "Audio cog not loaded. Run: `[p]load audio`"
    ERROR_NO_PLAYER = "No active player. Join a voice channel first."
    ERROR_NO_TRACKS_FOUND = "No tracks found."
    ERROR_INVALID_URL = "Invalid {platform} {content_type} URL"
    ERROR_CONTENT_UNAVAILABLE = "Content unavailable (private/region-locked)"
    ERROR_NO_TRACKS_IN_CONTENT = "No tracks in {content_type}"
    ERROR_FETCH_FAILED = "Could not fetch playlist."
    ERROR_NO_SPOTIFY = (
        "Spotify not configured. Run: `>tidalplay spotify <client_id> <client_secret>`"
    )
    ERROR_NO_YOUTUBE = "YouTube not configured. Run: `>tidalplay youtube <api_key>`"
    ERROR_INSTALL_SPOTIFY = "Install spotipy: `pip install spotipy`"
    ERROR_INSTALL_YOUTUBE = "Install: `pip install google-api-python-client`"
    ERROR_TIMEOUT = "Selection timed out."
    ERROR_API_TIMEOUT = "API request timed out. Please try again."

    SUCCESS_QUEUED = "Queued {count} tracks from {name}"
    SUCCESS_PARTIAL_QUEUE = "Queued {queued}/{total} ({skipped} not found on Tidal)"
    SUCCESS_QUEUE_CLEARED = "Queue cleared."
    SUCCESS_TOKENS_CLEARED = "Tokens cleared. Run:\\n1. `[p]pipinstall --force-reinstall tidalapi`\\n2. Restart bot\\n3. `>tidalsetup`"
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


class TidalPlayerError(Exception):
    pass


class AuthenticationError(TidalPlayerError):
    pass


class APIError(TidalPlayerError):
    pass


# =============================================================================
# HELPERS
# =============================================================================


class LRUCache(OrderedDict):
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


class CachedGuildSettings:
    def __init__(self, settings: Dict[str, Any], timestamp: float):
        self.settings = settings
        self.timestamp = timestamp

    def is_valid(self, ttl: float = CACHE_TTL) -> bool:
        return (time.time() - self.timestamp) < ttl


def async_retry(
    max_attempts: int = SEARCH_RETRY_ATTEMPTS,
    base_delay: float = RETRY_BASE_DELAY,
    exceptions: tuple = (Exception,),
):
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
                        delay = min(base_delay * (2**attempt), RETRY_MAX_DELAY)
                        await asyncio.sleep(delay)
            raise last_exception

        return wrapper

    return decorator


# =============================================================================
# MAIN COG
# =============================================================================


class TidalPlayer(commands.Cog):
    """Play music from Tidal with full metadata support in native queue."""

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
            interactive_search=False,
        )
        self.session: Optional["tidalapi.Session"] = (
            tidalapi.Session() if TIDALAPI_AVAILABLE else None
        )
        self.sp: Optional["spotipy.Spotify"] = None
        self.yt: Optional[Any] = None
        self.api_semaphore = asyncio.Semaphore(API_SEMAPHORE_LIMIT)
        self._guild_settings_cache: Dict[int, CachedGuildSettings] = {}
        self._track_meta_cache: LRUCache = LRUCache(maxsize=MAX_CACHE_SIZE)
        self._quality_label_cache: Dict[str, str] = {}
        self.bot.loop.create_task(self._initialize_apis())

    def cog_unload(self) -> None:
        self._guild_settings_cache.clear()
        self._track_meta_cache.clear()
        self._quality_label_cache.clear()
        log.info("TidalPlayer cog unloaded")

    # ... INITIALIZATION ...
    async def _initialize_apis(self) -> None:
        await self.bot.wait_until_ready()
        try:
            creds = await self.config.all()
            await asyncio.gather(
                self._initialize_tidal(creds),
                self._initialize_spotify(creds),
                self._initialize_youtube(creds),
                return_exceptions=True,
            )
        except Exception as e:
            log.error(f"API init error: {e}")

    async def _initialize_tidal(self, creds: Dict[str, Any]) -> None:
        if not TIDALAPI_AVAILABLE or not self.session:
            return
        required = ("token_type", "access_token", "refresh_token")
        if not all(creds.get(k) for k in required):
            return
        try:
            expiry_ts = creds.get("expiry_time")
            expiry_dt = datetime.fromtimestamp(expiry_ts) if expiry_ts else None
            await asyncio.wait_for(
                self.bot.loop.run_in_executor(
                    None,
                    lambda: self.session.load_oauth_session(
                        creds["token_type"],
                        creds["access_token"],
                        creds["refresh_token"],
                        expiry_dt,
                    ),
                ),
                timeout=API_TIMEOUT,
            )
            if self.session.check_login():
                log.info("Tidal session loaded")
        except Exception as e:
            log.error(f"Tidal session load failed: {e}")

    async def _initialize_spotify(self, creds: Dict[str, Any]) -> None:
        if not SPOTIFY_AVAILABLE:
            return
        cid, csec = creds.get("spotify_client_id"), creds.get("spotify_client_secret")
        if not (cid and csec):
            return
        try:
            self.sp = spotipy.Spotify(
                client_credentials_manager=SpotifyClientCredentials(cid, csec)
            )
            await asyncio.wait_for(
                self.bot.loop.run_in_executor(
                    None, lambda: self.sp.search("test", limit=1)
                ),
                timeout=API_TIMEOUT,
            )
            log.info("Spotify initialized")
        except Exception:
            self.sp = None

    async def _initialize_youtube(self, creds: Dict[str, Any]) -> None:
        if not YOUTUBE_API_AVAILABLE:
            return
        key = creds.get("youtube_api_key")
        if not key:
            return
        try:
            self.yt = build("youtube", "v3", developerKey=key)
            log.info("YouTube initialized")
        except Exception:
            self.yt = None

    # ... UTILS ...
    async def _get_guild_settings(
        self, guild_id: int, force_refresh: bool = False
    ) -> Dict[str, Any]:
        cached = self._guild_settings_cache.get(guild_id)
        if not force_refresh and cached and cached.is_valid():
            return cached.settings
        settings = await self.config.guild_from_id(guild_id).all()
        self._guild_settings_cache[guild_id] = CachedGuildSettings(
            settings, time.time()
        )
        return settings

    def _invalidate_guild_cache(self, guild_id: int) -> None:
        self._guild_settings_cache.pop(guild_id, None)

    def _get_quality_label(self, quality: str) -> str:
        if quality not in self._quality_label_cache:
            self._quality_label_cache[quality] = QUALITY_LABELS.get(
                quality, "LOSSLESS (FLAC)"
            )
        return self._quality_label_cache[quality]

    def _extract_meta(self, track: Any) -> TrackMeta:
        try:
            title = getattr(track, "name", None) or "Unknown"
            artist = (
                getattr(track.artist, "name", None)
                if hasattr(track, "artist")
                else "Unknown"
            )
            album = (
                getattr(track.album, "name", None) if hasattr(track, "album") else None
            )
            duration = int(getattr(track, "duration", 0) or 0)
            quality = getattr(track, "audio_quality", "LOSSLESS") or "LOSSLESS"
            return TrackMeta(
                title=title,
                artist=artist,
                album=album,
                duration=duration,
                quality=quality,
            )
        except Exception:
            return TrackMeta(
                title="Unknown",
                artist="Unknown",
                album=None,
                duration=0,
                quality="LOSSLESS",
            )

    @staticmethod
    def _format_duration(seconds: int) -> str:
        m, s = divmod(seconds, 60)
        if m >= 60:
            h, m = divmod(m, 60)
            return f"{h}:{m:02d}:{s:02d}"
        return f"{m:02d}:{s:02d}"

    def _filter_tracks(self, tracks: List[Any]) -> List[Any]:
        if not tracks:
            return tracks
        filtered = [
            t
            for t in tracks
            if not any(
                kw in (getattr(t, "name", "") or "").lower() for kw in FILTER_KEYWORDS
            )
        ]
        return filtered if filtered else tracks

    async def _should_cancel(self, guild_id: int) -> bool:
        try:
            return (await self._get_guild_settings(guild_id)).get("cancel_queue", False)
        except Exception:
            return False

    async def _set_cancel(self, guild_id: int, value: bool) -> None:
        try:
            await self.config.guild_from_id(guild_id).cancel_queue.set(value)
            self._invalidate_guild_cache(guild_id)
        except Exception:
            pass

    # =========================================================================
    # CORE PLAYBACK LOGIC (Object Mutation)
    # =========================================================================

    async def _get_player(self, ctx: commands.Context) -> Optional[Any]:
        """Get player, creating it if necessary."""
        if not LAVALINK_AVAILABLE:
            return None
        try:
            return lavalink.get_player(ctx.guild.id)
        except Exception:
            if ctx.author.voice and ctx.author.voice.channel:
                try:
                    await lavalink.connect(ctx.author.voice.channel)
                    return lavalink.get_player(ctx.guild.id)
                except Exception:
                    pass
            return None

    @async_retry(exceptions=(APIError, asyncio.TimeoutError))
    async def _search_tidal(self, query: str, guild_id: int) -> List[Any]:
        async with self.api_semaphore:
            try:
                result = await asyncio.wait_for(
                    self.bot.loop.run_in_executor(None, self.session.search, query),
                    timeout=API_TIMEOUT,
                )
                tracks = result.get("tracks", [])
                settings = await self._get_guild_settings(guild_id)
                if settings.get("filter_remixes", True) and tracks:
                    tracks = self._filter_tracks(tracks)
                return tracks
            except asyncio.TimeoutError:
                raise
            except Exception as e:
                raise APIError(f"Search failed: {e}")

    async def _get_track_url(self, track: Any) -> Optional[str]:
        try:
            return await asyncio.wait_for(
                self.bot.loop.run_in_executor(None, track.get_url), timeout=API_TIMEOUT
            )
        except Exception:
            return None

    async def _check_ready(self, ctx: commands.Context) -> bool:
        if not TIDALAPI_AVAILABLE:
            await ctx.send(Messages.ERROR_NO_TIDALAPI)
            return False
        if not self.session or not self.session.check_login():
            await ctx.send(Messages.ERROR_NOT_AUTHENTICATED)
            return False
        if not LAVALINK_AVAILABLE:
            await ctx.send(Messages.ERROR_NO_LAVALINK)
            return False
        return True

    async def _load_and_queue_track(
        self, ctx: commands.Context, tidal_track: Any, show_embed: bool = True
    ) -> bool:
        """
        Loads track, MUTATES it with Tidal metadata, and queues it.
        This ensures [p]queue sees the correct title/artist.
        """
        try:
            meta = self._extract_meta(tidal_track)
            url = await self._get_track_url(tidal_track)
            if not url:
                return False

            # Get Player
            player = await self._get_player(ctx)
            if not player:
                await ctx.send(Messages.ERROR_NO_PLAYER)
                return False

            # Load REAL track from Lavalink
            try:
                results = await player.load_tracks(url)
            except Exception:
                return False

            if not results or not results.tracks:
                return False

            # Get the playable track object
            track = results.tracks[0]

            # === MAGIC HAPPENS HERE ===
            # We overwrite the metadata on the real Lavalink object
            track.title = meta["title"]
            track.author = meta["artist"]
            # Add extra data if needed
            if not hasattr(track, "extra"):
                track.extra = {}
            track.extra.update(
                {"tidal_album": meta["album"], "tidal_quality": meta["quality"]}
            )
            # ==========================

            # Add to queue
            player.add(ctx.author, track)

            if not player.current:
                await player.play()

            if show_embed:
                await self._send_now_playing(ctx, meta)

            # Also update our cache just in case
            self._track_meta_cache.set(url, meta)

            return True

        except Exception as e:
            log.error(f"Queue error: {e}", exc_info=True)
            return False

    async def _send_now_playing(self, ctx: commands.Context, meta: TrackMeta) -> None:
        desc = f"**{meta['title']}** • {meta['artist']}"
        if meta.get("album"):
            desc += f"\\n_{meta['album']}_"
        embed = discord.Embed(
            title=Messages.STATUS_PLAYING, description=desc, color=discord.Color.blue()
        )
        embed.add_field(
            name="Quality", value=self._get_quality_label(meta["quality"]), inline=True
        )
        embed.set_footer(text=f"Duration: {self._format_duration(meta['duration'])}")
        await ctx.send(embed=embed)

    async def _play(
        self, ctx: commands.Context, track: Any, show_embed: bool = True
    ) -> bool:
        return await self._load_and_queue_track(ctx, track, show_embed)

    async def _search_and_queue(
        self, ctx: commands.Context, query: str, track_name: str
    ) -> bool:
        try:
            tracks = await self._search_tidal(query, ctx.guild.id)
            if not tracks:
                return False
            return await self._play(ctx, tracks[0], show_embed=False)
        except Exception:
            return False

    # ... INTERACTIVE & PLAYLISTS (Standard) ...
    async def _interactive_select(
        self, ctx: commands.Context, tracks: List[Any]
    ) -> Optional[Any]:
        if not tracks:
            return None
        to_show = min(5, len(tracks))
        lines = []
        for i, t in enumerate(tracks[:to_show]):
            m = self._extract_meta(t)
            lines.append(
                f"{REACTION_NUMBERS[i]} **{m['title']}** - {m['artist']} ({self._format_duration(m['duration'])})"
            )

        embed = discord.Embed(
            title="Search Results",
            description="\\n".join(lines),
            color=discord.Color.blue(),
        )
        embed.set_footer(text=Messages.STATUS_CHOOSE_TRACK.format(cancel=CANCEL_EMOJI))
        msg = await ctx.send(embed=embed)

        for i in range(to_show):
            await msg.add_reaction(REACTION_NUMBERS[i])
        await msg.add_reaction(CANCEL_EMOJI)

        def check(r, u):
            return (
                u == ctx.author
                and r.message.id == msg.id
                and str(r.emoji) in REACTION_NUMBERS[:to_show] + (CANCEL_EMOJI,)
            )

        try:
            r, _ = await self.bot.wait_for(
                "reaction_add", check=check, timeout=INTERACTIVE_TIMEOUT
            )
            await msg.delete()
            if str(r.emoji) == CANCEL_EMOJI:
                await ctx.send("Cancelled.")
                return None
            return tracks[REACTION_NUMBERS.index(str(r.emoji))]
        except asyncio.TimeoutError:
            await msg.delete()
            await ctx.send(Messages.ERROR_TIMEOUT)
            return None

    async def _queue_playlist_batch(
        self,
        ctx: commands.Context,
        tracks: List[Any],
        name: str,
        progress_msg: Optional[discord.Message] = None,
    ) -> QueueResult:
        queued, skipped, total, last_up = 0, 0, len(tracks), 0
        try:
            for i, track in enumerate(tracks, 1):
                if await self._should_cancel(ctx.guild.id):
                    break
                try:
                    if await self._play(ctx, track, show_embed=False):
                        queued += 1
                    else:
                        skipped += 1

                    if progress_msg and (
                        i - last_up >= BATCH_UPDATE_INTERVAL or i == total
                    ):
                        embed = discord.Embed(
                            title=Messages.PROGRESS_QUEUEING.format(
                                name=name, count=total
                            ),
                            description=Messages.PROGRESS_UPDATE.format(
                                queued=queued, skipped=skipped, current=i, total=total
                            ),
                            color=discord.Color.blue(),
                        )
                        try:
                            await progress_msg.edit(embed=embed)
                            last_up = i
                        except Exception:
                            pass
                    await asyncio.sleep(0.05)
                except Exception:
                    skipped += 1
            return QueueResult(queued=queued, skipped=skipped, total=total)
        finally:
            await self._set_cancel(ctx.guild.id, False)

    # ... HANDLERS ...
    async def _handle_tidal_url(self, ctx: commands.Context, url: str) -> None:
        tm = TIDAL_TRACK_PATTERN.search(url)
        am = TIDAL_ALBUM_PATTERN.search(url)
        pm = TIDAL_PLAYLIST_PATTERN.search(url)
        try:
            if tm:
                await self._handle_tidal_track(ctx, tm.group(1))
            elif am:
                await self._handle_tidal_album(ctx, am.group(1))
            elif pm:
                await self._handle_tidal_playlist(ctx, pm.group(1))
            else:
                await ctx.send(
                    Messages.ERROR_INVALID_URL.format(
                        platform="Tidal", content_type="content"
                    )
                )
        except Exception:
            await ctx.send(Messages.ERROR_CONTENT_UNAVAILABLE)

    async def _handle_tidal_track(self, ctx: commands.Context, tid: str) -> None:
        try:
            t = await asyncio.wait_for(
                self.bot.loop.run_in_executor(None, self.session.track, tid),
                timeout=API_TIMEOUT,
            )
            if t:
                await self._play(ctx, t)
            else:
                await ctx.send(Messages.ERROR_NO_TRACKS_FOUND)
        except Exception:
            await ctx.send(Messages.ERROR_CONTENT_UNAVAILABLE)

    async def _handle_tidal_album(self, ctx: commands.Context, aid: str) -> None:
        try:
            alb = await asyncio.wait_for(
                self.bot.loop.run_in_executor(None, self.session.album, aid),
                timeout=API_TIMEOUT,
            )
            if not alb:
                await ctx.send(Messages.ERROR_CONTENT_UNAVAILABLE)
                return
            tracks = await asyncio.wait_for(
                self.bot.loop.run_in_executor(None, alb.tracks), timeout=API_TIMEOUT
            )
            if not tracks:
                await ctx.send(
                    Messages.ERROR_NO_TRACKS_IN_CONTENT.format(content_type="album")
                )
                return

            pmsg = await ctx.send(
                Messages.PROGRESS_QUEUEING.format(name=alb.name, count=len(tracks))
            )
            res = await self._queue_playlist_batch(ctx, tracks, alb.name, pmsg)
            await pmsg.edit(
                embed=discord.Embed(
                    title=Messages.SUCCESS_PARTIAL_QUEUE.format(
                        queued=res["queued"], total=res["total"], skipped=res["skipped"]
                    ),
                    description=f"Album: {alb.name}",
                    color=discord.Color.blue(),
                )
            )
        except Exception:
            await ctx.send(Messages.ERROR_API_TIMEOUT)

    async def _handle_tidal_playlist(self, ctx: commands.Context, pid: str) -> None:
        try:
            pl = await asyncio.wait_for(
                self.bot.loop.run_in_executor(None, self.session.playlist, pid),
                timeout=API_TIMEOUT,
            )
            if not pl:
                await ctx.send(Messages.ERROR_CONTENT_UNAVAILABLE)
                return
            tracks = await asyncio.wait_for(
                self.bot.loop.run_in_executor(None, pl.tracks), timeout=API_TIMEOUT
            )
            if not tracks:
                await ctx.send(
                    Messages.ERROR_NO_TRACKS_IN_CONTENT.format(content_type="playlist")
                )
                return

            pmsg = await ctx.send(
                Messages.PROGRESS_QUEUEING.format(name=pl.name, count=len(tracks))
            )
            res = await self._queue_playlist_batch(ctx, tracks, pl.name, pmsg)
            await pmsg.edit(
                embed=discord.Embed(
                    title=Messages.SUCCESS_PARTIAL_QUEUE.format(
                        queued=res["queued"], total=res["total"], skipped=res["skipped"]
                    ),
                    description=f"Playlist: {pl.name}",
                    color=discord.Color.blue(),
                )
            )
        except Exception:
            await ctx.send(Messages.ERROR_API_TIMEOUT)

    async def _queue_spotify_playlist(self, ctx: commands.Context, pl_id: str) -> None:
        if not SPOTIFY_AVAILABLE or not self.sp:
            await ctx.send(Messages.ERROR_NO_SPOTIFY)
            return
        pmsg = await ctx.send(Messages.PROGRESS_FETCHING_SPOTIFY)
        try:
            pl = await asyncio.wait_for(
                self.bot.loop.run_in_executor(None, self.sp.playlist, pl_id),
                timeout=API_TIMEOUT,
            )
            tracks = pl["tracks"]["items"] if pl else []
            if not tracks:
                await pmsg.edit(
                    content=Messages.ERROR_NO_TRACKS_IN_CONTENT.format(
                        content_type="playlist"
                    )
                )
                return

            queued, skipped, last_up = 0, 0, 0
            for i, item in enumerate(tracks, 1):
                if await self._should_cancel(ctx.guild.id):
                    break
                t = item.get("track")
                if not t:
                    skipped += 1
                    continue
                query = f"{t['name']} {t['artists'][0]['name']}"
                if await self._search_and_queue(ctx, query, t["name"]):
                    queued += 1
                else:
                    skipped += 1

                if i - last_up >= BATCH_UPDATE_INTERVAL:
                    try:
                        await pmsg.edit(
                            embed=discord.Embed(
                                title=Messages.PROGRESS_QUEUEING_SPOTIFY.format(
                                    count=len(tracks)
                                ),
                                description=Messages.PROGRESS_UPDATE.format(
                                    queued=queued,
                                    skipped=skipped,
                                    current=i,
                                    total=len(tracks),
                                ),
                                color=discord.Color.green(),
                            )
                        )
                        last_up = i
                    except Exception:
                        pass
                await asyncio.sleep(0.05)
            await pmsg.edit(
                embed=discord.Embed(
                    title=Messages.SUCCESS_PARTIAL_QUEUE.format(
                        queued=queued, total=len(tracks), skipped=skipped
                    ),
                    color=discord.Color.green(),
                )
            )
        except Exception:
            await pmsg.edit(content=Messages.ERROR_FETCH_FAILED)
        finally:
            await self._set_cancel(ctx.guild.id, False)

    async def _queue_youtube_playlist(self, ctx: commands.Context, pl_id: str) -> None:
        if not YOUTUBE_API_AVAILABLE or not self.yt:
            await ctx.send(Messages.ERROR_NO_YOUTUBE)
            return
        pmsg = await ctx.send(Messages.PROGRESS_FETCHING_YOUTUBE)
        try:
            req = self.yt.playlistItems().list(
                part="snippet", playlistId=pl_id, maxResults=50
            )
            resp = await asyncio.wait_for(
                self.bot.loop.run_in_executor(None, req.execute), timeout=API_TIMEOUT
            )
            items = resp.get("items", [])
            if not items:
                await pmsg.edit(
                    content=Messages.ERROR_NO_TRACKS_IN_CONTENT.format(
                        content_type="playlist"
                    )
                )
                return

            queued, skipped, last_up = 0, 0, 0
            for i, item in enumerate(items, 1):
                if await self._should_cancel(ctx.guild.id):
                    break
                title = item["snippet"]["title"]
                if await self._search_and_queue(ctx, title, title):
                    queued += 1
                else:
                    skipped += 1
                if i - last_up >= BATCH_UPDATE_INTERVAL:
                    try:
                        await pmsg.edit(
                            embed=discord.Embed(
                                title=Messages.PROGRESS_QUEUEING_YOUTUBE.format(
                                    count=len(items)
                                ),
                                description=Messages.PROGRESS_UPDATE.format(
                                    queued=queued,
                                    skipped=skipped,
                                    current=i,
                                    total=len(items),
                                ),
                                color=discord.Color.red(),
                            )
                        )
                        last_up = i
                    except Exception:
                        pass
                await asyncio.sleep(0.05)
            await pmsg.edit(
                embed=discord.Embed(
                    title=Messages.SUCCESS_PARTIAL_QUEUE.format(
                        queued=queued, total=len(items), skipped=skipped
                    ),
                    color=discord.Color.red(),
                )
            )
        except Exception:
            await pmsg.edit(content=Messages.ERROR_FETCH_FAILED)
        finally:
            await self._set_cancel(ctx.guild.id, False)

    # ... COMMANDS ...
    @commands.command(name="tplay")
    async def tplay(self, ctx: commands.Context, *, query: str) -> None:
        if not await self._check_ready(ctx):
            return
        if TIDAL_URL_PATTERN.search(query):
            await self._handle_tidal_url(ctx, query)
        elif m := SPOTIFY_PLAYLIST_PATTERN.search(query):
            await self._queue_spotify_playlist(ctx, m.group(1))
        elif m := YOUTUBE_PLAYLIST_PATTERN.search(query):
            await self._queue_youtube_playlist(ctx, m.group(1))
        else:
            try:
                tracks = await self._search_tidal(query, ctx.guild.id)
                if not tracks:
                    await ctx.send(Messages.ERROR_NO_TRACKS_FOUND)
                    return
                settings = await self._get_guild_settings(ctx.guild.id)
                if settings.get("interactive_search", False):
                    sel = await self._interactive_select(ctx, tracks)
                    if sel:
                        await self._play(ctx, sel)
                else:
                    await self._play(ctx, tracks[0])
            except Exception:
                await ctx.send(Messages.ERROR_NO_TRACKS_FOUND)

    @commands.command(name="tstop")
    async def tstop(self, ctx: commands.Context) -> None:
        await self._set_cancel(ctx.guild.id, True)
        await ctx.send(Messages.STATUS_STOPPING)

    @commands.command(name="tqueue")
    async def tqueue(self, ctx: commands.Context) -> None:
        # This command reads from metadata cache primarily, or fallback to lavalink
        try:
            player = await self._get_player(ctx)
            if not player or not player.queue:
                await ctx.send(Messages.STATUS_EMPTY_QUEUE)
                return

            items = []
            for t in player.queue:
                # Read our injected metadata if available
                title = getattr(t, "title", "Unknown")
                author = getattr(t, "author", "Unknown")
                length = getattr(t, "length", 0) // 1000
                items.append(f"{title} - {author} ({self._format_duration(length)})")

            pages = []
            for i in range(0, len(items), 10):
                chunk = items[i : i + 10]
                lines = [f"**{j}.** {l}" for j, l in enumerate(chunk, start=i + 1)]
                embed = discord.Embed(
                    title=f"Queue ({len(items)})",
                    description="\\n".join(lines),
                    color=discord.Color.blue(),
                )
                embed.set_footer(text=f"Page {len(pages) + 1}")
                pages.append(embed)

            if len(pages) == 1:
                await ctx.send(embed=pages[0])
            else:
                await menu(ctx, pages, DEFAULT_CONTROLS)
        except Exception:
            await ctx.send("Queue error.")

    @commands.command(name="tclear")
    async def tclear(self, ctx: commands.Context) -> None:
        try:
            player = await self._get_player(ctx)
            if player:
                player.queue.clear()
            self._track_meta_cache.clear()
            await ctx.send(Messages.SUCCESS_QUEUE_CLEARED)
        except Exception:
            pass

    @commands.command(name="tfilter")
    async def tfilter(self, ctx: commands.Context) -> None:
        curr = await self.config.guild(ctx.guild).filter_remixes()
        await self.config.guild(ctx.guild).filter_remixes.set(not curr)
        self._invalidate_guild_cache(ctx.guild.id)
        await ctx.send(
            Messages.SUCCESS_FILTER_ENABLED
            if not curr
            else Messages.SUCCESS_FILTER_DISABLED
        )

    @commands.command(name="tinteractive")
    async def tinteractive(self, ctx: commands.Context) -> None:
        curr = await self.config.guild(ctx.guild).interactive_search()
        await self.config.guild(ctx.guild).interactive_search.set(not curr)
        self._invalidate_guild_cache(ctx.guild.id)
        await ctx.send(
            Messages.SUCCESS_INTERACTIVE_ENABLED
            if not curr
            else Messages.SUCCESS_INTERACTIVE_DISABLED
        )

    @commands.is_owner()
    @commands.command(name="tidalsetup")
    async def tidalsetup(self, ctx: commands.Context) -> None:
        if not TIDALAPI_AVAILABLE:
            await ctx.send(Messages.ERROR_NO_TIDALAPI)
            return
        try:
            l, f = self.session.login_oauth()
            e = discord.Embed(
                title="Tidal OAuth Setup",
                description="Click below to login:",
                color=discord.Color.blue(),
            )
            e.add_field(
                name="Login URL", value=f"[Click here]({l.verification_uri_complete})"
            )
            try:
                await ctx.author.send(embed=e)
                await ctx.send("Check DMs.")
            except:
                await ctx.send(embed=e)
            await self.bot.loop.run_in_executor(None, f.result, 300)
            if self.session.check_login():
                await self.config.token_type.set(self.session.token_type)
                await self.config.access_token.set(self.session.access_token)
                await self.config.refresh_token.set(self.session.refresh_token)
                await self.config.expiry_time.set(
                    int(self.session.expiry_time.timestamp())
                    if self.session.expiry_time
                    else None
                )
                await ctx.send(Messages.SUCCESS_TIDAL_SETUP)
            else:
                await ctx.send("Login failed.")
        except Exception as err:
            await ctx.send(f"Setup failed: {err}")

    @commands.is_owner()
    @commands.group(name="tidalplay")
    async def tidalplay(self, ctx: commands.Context) -> None:
        if ctx.invoked_subcommand is None:
            await ctx.send_help(ctx.command)

    @tidalplay.command(name="spotify")
    async def tidalplay_spotify(
        self, ctx: commands.Context, cid: str, csec: str
    ) -> None:
        await self.config.spotify_client_id.set(cid)
        await self.config.spotify_client_secret.set(csec)
        await self._initialize_spotify(await self.config.all())
        await ctx.send(Messages.SUCCESS_SPOTIFY_CONFIGURED)

    @tidalplay.command(name="youtube")
    async def tidalplay_youtube(self, ctx: commands.Context, key: str) -> None:
        await self.config.youtube_api_key.set(key)
        await self._initialize_youtube(await self.config.all())
        await ctx.send(Messages.SUCCESS_YOUTUBE_CONFIGURED)

    @tidalplay.command(name="cleartokens")
    async def tidalplay_cleartokens(self, ctx: commands.Context) -> None:
        await self.config.token_type.set(None)
        await self.config.access_token.set(None)
        await self.config.refresh_token.set(None)
        await self.config.expiry_time.set(None)
        await ctx.send(Messages.SUCCESS_TOKENS_CLEARED)


async def setup(bot: Red) -> None:
    await bot.add_cog(TidalPlayer(bot))
