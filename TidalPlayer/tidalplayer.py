"""
TidalPlayer - Tidal music integration for Red Discord Bot
Optimized for Performance and Reliability
"""

import asyncio
import logging
import re
from datetime import datetime
from functools import wraps
from typing import Any, Callable, Dict, List, Optional, TypedDict

import discord
from redbot.core import Config, commands
from redbot.core.bot import Red

# Optional Dependencies
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
# CONSTANTS & CONFIG
# =============================================================================

COG_IDENTIFIER = 160819386
MAX_CACHE_SIZE = 500
API_SEMAPHORE_LIMIT = 5
SEARCH_RETRY_ATTEMPTS = 3
API_TIMEOUT = 30.0
BATCH_UPDATE_INTERVAL = 10
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

# Pre-compiled Regex
TIDAL_TRACK_PATTERN = re.compile(r"tidal\.com/(?:browse/)?track/(\d+)")
TIDAL_ALBUM_PATTERN = re.compile(r"tidal\.com/(?:browse/)?album/(\d+)")
TIDAL_PLAYLIST_PATTERN = re.compile(r"tidal\.com/(?:browse/)?playlist/([a-f0-9-]+)")
TIDAL_MIX_PATTERN = re.compile(r"tidal\.com/(?:browse/)?mix/([a-f0-9]+)")

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
    ERROR_NO_SPOTIFY = "Spotify not configured. Run: `>tidalplay spotify <id> <secret>`"
    ERROR_NO_YOUTUBE = "YouTube not configured. Run: `>tidalplay youtube <key>`"
    ERROR_TIMEOUT = "Selection timed out."
    ERROR_API_TIMEOUT = "API request timed out. Please try again."

    SUCCESS_PARTIAL_QUEUE = "Queued {queued}/{total} ({skipped} not found on Tidal)"
    SUCCESS_TOKENS_CLEARED = "Tokens cleared."
    SUCCESS_TIDAL_SETUP = "Tidal setup complete!"
    SUCCESS_SPOTIFY_CONFIGURED = "Spotify configured."
    SUCCESS_YOUTUBE_CONFIGURED = "YouTube configured."
    SUCCESS_FILTER_ENABLED = "Remix/TikTok filter enabled."
    SUCCESS_FILTER_DISABLED = "Remix/TikTok filter disabled."
    SUCCESS_INTERACTIVE_ENABLED = "Interactive search enabled."
    SUCCESS_INTERACTIVE_DISABLED = "Interactive search disabled."

    STATUS_CHOOSE_TRACK = "React with a number to select a track, or {cancel} to cancel"
    PROGRESS_QUEUEING = "Queueing {name} ({count} tracks)..."
    PROGRESS_FETCHING = "Fetching playlist..."
    PROGRESS_UPDATE = "{queued} queued, {skipped} skipped ({current}/{total})"
    STATUS_STOPPING = "Stopping playlist queueing..."
    STATUS_PLAYING = "Playing from Tidal"


class TidalPlayerError(Exception):
    pass


class APIError(TidalPlayerError):
    pass


# =============================================================================
# UTILITIES
# =============================================================================


def async_retry(
    max_attempts: int = SEARCH_RETRY_ATTEMPTS,
    base_delay: float = RETRY_BASE_DELAY,
    exceptions: tuple = (Exception,),
):
    """Decorator for retrying async functions."""

    def decorator(func: Callable) -> Callable:
        @wraps(func)
        async def wrapper(*args, **kwargs):
            for attempt in range(max_attempts):
                try:
                    return await func(*args, **kwargs)
                except exceptions:
                    if attempt == max_attempts - 1:
                        raise
                    delay = min(base_delay * (2**attempt), RETRY_MAX_DELAY)
                    await asyncio.sleep(delay)
            return None

        return wrapper

    return decorator


def truncate(text: str, limit: int) -> str:
    """Safe string truncation for Discord limits."""
    return text[: limit - 3] + "..." if len(text) > limit else text


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
            cancel_queue=False, filter_remixes=True, interactive_search=False
        )

        self.session: Optional["tidalapi.Session"] = (
            tidalapi.Session() if TIDALAPI_AVAILABLE else None
        )

        self.sp: Optional["spotipy.Spotify"] = None
        self.yt: Optional[Any] = None
        self.api_semaphore = asyncio.Semaphore(API_SEMAPHORE_LIMIT)

        self._track_meta_cache: Dict[str, TrackMeta] = {}

        self.bot.loop.create_task(self._initialize_apis())

    def cog_unload(self) -> None:
        self._track_meta_cache.clear()
        log.info("TidalPlayer cog unloaded")

    async def _initialize_apis(self) -> None:
        await self.bot.wait_until_ready()
        try:
            creds = await self.config.all()
            await self._initialize_tidal(creds)
            await self._initialize_spotify(creds)
            await self._initialize_youtube(creds)
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

            self.session.load_oauth_session(
                creds["token_type"],
                creds["access_token"],
                creds["refresh_token"],
                expiry_dt,
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
            log.info("Spotify configured")
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
            log.info("YouTube configured")
        except Exception:
            self.yt = None

    # ... HELPERS ...

    def _extract_meta(self, track: Any) -> TrackMeta:
        return TrackMeta(
            title=getattr(track, "name", "Unknown") or "Unknown",
            artist=getattr(track.artist, "name", "Unknown")
            if hasattr(track, "artist")
            else "Unknown",
            album=getattr(track.album, "name", None)
            if hasattr(track, "album")
            else None,
            duration=int(getattr(track, "duration", 0) or 0),
            quality=getattr(track, "audio_quality", "LOSSLESS") or "LOSSLESS",
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

        filtered = []
        for t in tracks:
            name_lower = (getattr(t, "name", "") or "").lower()
            if not any(kw in name_lower for kw in FILTER_KEYWORDS):
                filtered.append(t)

        return filtered if filtered else tracks

    # =========================================================================
    # CORE PLAYBACK LOGIC
    # =========================================================================

    async def _get_player(self, ctx: commands.Context) -> Optional[Any]:
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
                result = await self.bot.loop.run_in_executor(
                    None, self.session.search, query
                )
                tracks = result.get("tracks", [])

                if (
                    await self.config.guild_from_id(guild_id).filter_remixes()
                    and tracks
                ):
                    tracks = self._filter_tracks(tracks)

                return tracks
            except Exception as e:
                raise APIError(f"Search failed: {e}")

    async def _get_track_url(self, track: Any) -> Optional[str]:
        try:
            return await self.bot.loop.run_in_executor(None, track.get_url)
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
            await ctx.send(Messages.ERROR_NO_PLAYER)
            return False

        return True

    async def _load_and_queue_track(
        self, ctx: commands.Context, tidal_track: Any, show_embed: bool = True
    ) -> bool:
        """Loads track, MUTATES it with Tidal metadata, and queues it."""
        try:
            meta = self._extract_meta(tidal_track)
            url = await self._get_track_url(tidal_track)

            if not url:
                return False

            player = await self._get_player(ctx)
            if not player:
                await ctx.send(Messages.ERROR_NO_PLAYER)
                return False

            try:
                results = await player.load_tracks(url)
            except Exception:
                return False

            if not results or not results.tracks:
                return False

            track = results.tracks[0]

            # === METADATA MUTATION ===
            track.title = truncate(meta["title"], 100)
            if meta.get("album"):
                track.author = f"{meta['artist']} - {meta['album']}"
            else:
                track.author = meta["artist"]
            # =========================

            player.add(ctx.author, track)

            if not player.current:
                await player.play()

            if show_embed:
                await self._send_now_playing(ctx, meta)

            if len(self._track_meta_cache) > MAX_CACHE_SIZE:
                self._track_meta_cache.clear()
            self._track_meta_cache[url] = meta

            return True

        except Exception as e:
            log.error(f"Queue error: {e}", exc_info=True)
            return False

    async def _send_now_playing(self, ctx: commands.Context, meta: TrackMeta) -> None:
        desc = f"**{meta['title']}**\n{meta['artist']}"
        if meta.get("album"):
            desc += f"\n_{meta['album']}_"

        embed = discord.Embed(
            title=Messages.STATUS_PLAYING, description=desc, color=discord.Color.blue()
        )

        quality_label = QUALITY_LABELS.get(meta["quality"], "LOSSLESS (FLAC)")
        embed.add_field(name="Quality", value=quality_label, inline=True)
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

    # UNIFIED LIST PROCESSOR (FIXED: Instant Stop)
    async def _process_track_list(
        self,
        ctx: commands.Context,
        items: List[Any],
        name: str,
        item_processor: Callable[[Any], str],  # Returns query or None if skipped
        color: discord.Color = discord.Color.blue(),
    ) -> None:
        """Generic processor for iterating lists and queueing tracks."""
        total = len(items)
        if not total:
            await ctx.send(
                Messages.ERROR_NO_TRACKS_IN_CONTENT.format(content_type="list")
            )
            return

        pmsg = await ctx.send(
            Messages.PROGRESS_QUEUEING.format(name=truncate(name, 50), count=total)
        )
        queued, skipped, last_up = 0, 0, 0

        try:
            for i, item in enumerate(items, 1):
                # FIX: Instant cancellation check + Player check
                if await self.config.guild(ctx.guild).cancel_queue():
                    break

                # FIX: Stop if bot lost connection (allows >leave to stop queue)
                player = await self._get_player(ctx)
                if not player or not player.is_connected:
                    break

                query = item_processor(item)

                success = False
                if query:
                    if hasattr(query, "get_url"):
                        success = await self._play(ctx, query, show_embed=False)
                    else:
                        success = await self._search_and_queue(ctx, query, query)

                if success:
                    queued += 1
                else:
                    skipped += 1

                if i - last_up >= BATCH_UPDATE_INTERVAL or i == total:
                    try:
                        embed = discord.Embed(
                            title=Messages.PROGRESS_QUEUEING.format(
                                name=truncate(name, 50), count=total
                            ),
                            description=Messages.PROGRESS_UPDATE.format(
                                queued=queued, skipped=skipped, current=i, total=total
                            ),
                            color=color,
                        )
                        await pmsg.edit(embed=embed)
                        last_up = i
                    except Exception:
                        pass
                    await asyncio.sleep(0)

            await pmsg.edit(
                embed=discord.Embed(
                    title=Messages.SUCCESS_PARTIAL_QUEUE.format(
                        queued=queued, total=total, skipped=skipped
                    ),
                    description=f"Source: {truncate(name, 100)}",
                    color=color,
                )
            )
        except Exception:
            await pmsg.edit(content=Messages.ERROR_FETCH_FAILED)
        finally:
            await self.config.guild(ctx.guild).cancel_queue.set(False)

    # ... HANDLERS ...

    async def _handle_tidal_url(self, ctx: commands.Context, url: str) -> None:
        tm = TIDAL_TRACK_PATTERN.search(url)
        am = TIDAL_ALBUM_PATTERN.search(url)
        pm = TIDAL_PLAYLIST_PATTERN.search(url)
        mm = TIDAL_MIX_PATTERN.search(url)

        try:
            if tm:
                await self._handle_tidal_track(ctx, tm.group(1))
            elif am:
                await self._handle_tidal_album(ctx, am.group(1))
            elif pm:
                await self._handle_tidal_playlist(ctx, pm.group(1))
            elif mm:
                await self._handle_tidal_mix(ctx, mm.group(1))
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
            t = await self.bot.loop.run_in_executor(None, self.session.track, tid)
            if t:
                await self._play(ctx, t)
            else:
                await ctx.send(Messages.ERROR_NO_TRACKS_FOUND)
        except Exception:
            await ctx.send(Messages.ERROR_CONTENT_UNAVAILABLE)

    async def _handle_tidal_album(self, ctx: commands.Context, aid: str) -> None:
        try:
            alb = await self.bot.loop.run_in_executor(None, self.session.album, aid)
            if not alb:
                await ctx.send(Messages.ERROR_CONTENT_UNAVAILABLE)
                return
            tracks = await self.bot.loop.run_in_executor(None, alb.tracks)
            await self._process_track_list(
                ctx, tracks, alb.name, lambda t: t, discord.Color.blue()
            )
        except Exception:
            await ctx.send(Messages.ERROR_API_TIMEOUT)

    async def _handle_tidal_playlist(self, ctx: commands.Context, pid: str) -> None:
        try:
            pl = await self.bot.loop.run_in_executor(None, self.session.playlist, pid)
            if not pl:
                await ctx.send(Messages.ERROR_CONTENT_UNAVAILABLE)
                return
            tracks = await self.bot.loop.run_in_executor(None, pl.tracks)
            await self._process_track_list(
                ctx, tracks, pl.name, lambda t: t, discord.Color.blue()
            )
        except Exception:
            await ctx.send(Messages.ERROR_API_TIMEOUT)

    async def _handle_tidal_mix(self, ctx: commands.Context, mid: str) -> None:
        try:
            if hasattr(self.session, "mix"):
                mix = await self.bot.loop.run_in_executor(None, self.session.mix, mid)
            else:
                await ctx.send(
                    "Your `tidalapi` version might be too old to support Mixes. Try `pip install -U tidalapi`."
                )
                return

            if not mix:
                await ctx.send(Messages.ERROR_CONTENT_UNAVAILABLE)
                return

            tracks = await self.bot.loop.run_in_executor(None, mix.items)
            await self._process_track_list(
                ctx, tracks, f"Mix: {mid}", lambda t: t, discord.Color.purple()
            )

        except Exception as e:
            log.error(f"Mix fetch error: {e}")
            await ctx.send(Messages.ERROR_API_TIMEOUT)

    async def _queue_spotify_playlist(self, ctx: commands.Context, pl_id: str) -> None:
        if not SPOTIFY_AVAILABLE or not self.sp:
            await ctx.send(Messages.ERROR_NO_SPOTIFY)
            return

        try:
            pl = await self.bot.loop.run_in_executor(None, self.sp.playlist, pl_id)
            tracks = pl["tracks"]["items"] if pl else []

            def sp_processor(item):
                t = item.get("track")
                return f"{t['name']} {t['artists'][0]['name']}" if t else None

            await self._process_track_list(
                ctx, tracks, "Spotify Playlist", sp_processor, discord.Color.green()
            )
        except Exception:
            await ctx.send(Messages.ERROR_FETCH_FAILED)

    async def _queue_youtube_playlist(self, ctx: commands.Context, pl_id: str) -> None:
        if not YOUTUBE_API_AVAILABLE or not self.yt:
            await ctx.send(Messages.ERROR_NO_YOUTUBE)
            return

        try:
            req = self.yt.playlistItems().list(
                part="snippet", playlistId=pl_id, maxResults=50
            )
            resp = await self.bot.loop.run_in_executor(None, req.execute)
            items = resp.get("items", [])

            def yt_processor(item):
                return item["snippet"]["title"]

            await self._process_track_list(
                ctx, items, "YouTube Playlist", yt_processor, discord.Color.red()
            )
        except Exception:
            await ctx.send(Messages.ERROR_FETCH_FAILED)

    # ... COMMANDS ...

    @commands.command(name="tplay")
    async def tplay(self, ctx: commands.Context, *, query: str) -> None:
        if not await self._check_ready(ctx):
            return

        if "tidal.com" in query:
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

                if await self.config.guild(ctx.guild).interactive_search():
                    sel = await self._interactive_select(ctx, tracks)
                    if sel:
                        await self._play(ctx, sel)
                else:
                    await self._play(ctx, tracks[0])
            except Exception:
                await ctx.send(Messages.ERROR_NO_TRACKS_FOUND)

    @commands.command(name="tstop")
    async def tstop(self, ctx: commands.Context) -> None:
        await self.config.guild(ctx.guild).cancel_queue.set(True)
        await ctx.send(Messages.STATUS_STOPPING)

    @commands.command(name="tfilter")
    async def tfilter(self, ctx: commands.Context) -> None:
        curr = await self.config.guild(ctx.guild).filter_remixes()
        await self.config.guild(ctx.guild).filter_remixes.set(not curr)
        await ctx.send(
            Messages.SUCCESS_FILTER_ENABLED
            if not curr
            else Messages.SUCCESS_FILTER_DISABLED
        )

    @commands.command(name="tinteractive")
    async def tinteractive(self, ctx: commands.Context) -> None:
        curr = await self.config.guild(ctx.guild).interactive_search()
        await self.config.guild(ctx.guild).interactive_search.set(not curr)
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
