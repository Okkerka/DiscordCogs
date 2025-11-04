from redbot.core import commands, Config
from redbot.core.utils.menus import menu, DEFAULT_CONTROLS
import discord
import logging
import asyncio
import re
from typing import Dict, List, Optional, Any, Callable, Tuple
from datetime import datetime
from functools import wraps, lru_cache

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

MAX_QUEUE_SIZE = 1000
BATCH_UPDATE_INTERVAL = 5
API_SEMAPHORE_LIMIT = 3
SEARCH_RETRY_ATTEMPTS = 3
COG_IDENTIFIER = 160819386
INTERACTIVE_TIMEOUT = 30
RETRY_BASE_DELAY = 0.5
RETRY_MAX_DELAY = 5.0

# Reaction numbers for interactive selection
REACTION_NUMBERS = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣"]
CANCEL_EMOJI = "❌"

class Messages:
    ERROR_NO_TIDALAPI = "tidalapi not installed. Run: `[p]pipinstall tidalapi`"
    ERROR_NOT_AUTHENTICATED = "Not authenticated. Run: `>tidalsetup`"
    ERROR_NO_AUDIO_COG = "Audio cog not loaded. Run: `[p]load audio`"
    ERROR_NO_TRACKS_FOUND = "No tracks found."
    ERROR_INVALID_URL = "Invalid {platform} {content_type} URL"
    ERROR_CONTENT_UNAVAILABLE = "Content unavailable (private/region-locked)"
    ERROR_NO_TRACKS_IN_CONTENT = "No tracks in {content_type}"
    ERROR_FETCH_FAILED = "Could not fetch playlist."
    ERROR_NO_SPOTIFY = "Spotify not configured. Run: `>tidalplay spotify <client_id> <client_secret>`"
    ERROR_NO_YOUTUBE = "YouTube not configured. Run: `>tidalplay youtube <api_key>`"
    ERROR_INSTALL_SPOTIFY = "Install spotipy: `pip install spotipy`"
    ERROR_INSTALL_YOUTUBE = "Install: `pip install google-api-python-client`"

    SUCCESS_QUEUED = "Queued {count} tracks from {name}"
    SUCCESS_PARTIAL_QUEUE = "Queued {queued}/{total} ({skipped} not found on Tidal)"
    SUCCESS_QUEUE_CLEARED = "Queue cleared."
    SUCCESS_TOKENS_CLEARED = "Tokens cleared. Run:\n1. `[p]pipinstall --force-reinstall tidalapi`\n2. Restart bot\n3. `>tidalsetup`"
    SUCCESS_TIDAL_SETUP = "Tidal setup complete!"
    SUCCESS_SPOTIFY_CONFIGURED = "Spotify configured."
    SUCCESS_YOUTUBE_CONFIGURED = "YouTube configured."
    SUCCESS_FILTER_ENABLED = "Remix/TikTok filter enabled."
    SUCCESS_FILTER_DISABLED = "Remix/TikTok filter disabled."
    SUCCESS_INTERACTIVE_ENABLED = "Interactive search enabled. You'll choose from multiple results."
    SUCCESS_INTERACTIVE_DISABLED = "Interactive search disabled. First result will auto-play."

    ERROR_TIMEOUT = "Selection timed out."
    ERROR_INVALID_CHOICE = "Invalid choice. Please enter a number between 1 and {max}."

    STATUS_CHOOSE_TRACK = "React with a number to select a track, or {cancel} to cancel"

    PROGRESS_QUEUEING = "Queueing {name} ({count} tracks)..."
    PROGRESS_FETCHING_SPOTIFY = "Fetching Spotify playlist..."
    PROGRESS_FETCHING_YOUTUBE = "Fetching YouTube playlist..."
    PROGRESS_QUEUEING_SPOTIFY = "Queueing {count} tracks from Spotify..."
    PROGRESS_QUEUEING_YOUTUBE = "Queueing {count} videos from YouTube..."
    PROGRESS_UPDATE = "{queued} queued, {skipped} skipped ({current}/{total})"
    PROGRESS_OAUTH = "Starting OAuth..."

    STATUS_STOPPING = "Stopping playlist queueing..."
    STATUS_CANCELLED_WITH_SKIPPED = "Cancelled. {queued} queued, {skipped} skipped."
    STATUS_EMPTY_QUEUE = "Queue is empty."
    STATUS_PLAYING = "Playing from Tidal"

QUALITY_LABELS = {
    "HI_RES": "HI-RES (MQA)",
    "HI_RES_LOSSLESS": "HI-RES LOSSLESS",
    "LOSSLESS": "LOSSLESS (FLAC)",
    "HIGH": "HIGH (320kbps)",
    "LOW": "LOW (96kbps)"
}

# Removed 'nightcore' from filter
FILTER_KEYWORDS = [
    'sped up', 'slowed', 'tiktok',
    'reverb', '8d audio', 'bass boosted'
]

class TidalPlayerError(Exception):
    pass

class AuthenticationError(TidalPlayerError):
    pass

class APIError(TidalPlayerError):
    pass

def async_retry(max_attempts: int = SEARCH_RETRY_ATTEMPTS, base_delay: float = RETRY_BASE_DELAY):
    def decorator(func: Callable) -> Callable:
        @wraps(func)
        async def wrapper(*args, **kwargs):
            last_exception = None
            for attempt in range(max_attempts):
                try:
                    return await func(*args, **kwargs)
                except Exception as e:
                    last_exception = e
                    if attempt < max_attempts - 1:
                        delay = min(base_delay * (2 ** attempt), RETRY_MAX_DELAY)
                        log.warning(f"Attempt {attempt + 1}/{max_attempts} failed for {func.__name__}: {e}")
                        await asyncio.sleep(delay)
            if last_exception:
                raise last_exception
        return wrapper
    return decorator

class TidalPlayer(commands.Cog):
    def __init__(self, bot: commands.Bot):
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
        )

        self.config.register_guild(
            track_metadata=[],
            cancel_queue=False,
            filter_remixes=True,
            interactive_search=False
        )

        self.session: Optional[tidalapi.Session] = (
            tidalapi.Session() if TIDALAPI_AVAILABLE else None
        )
        self.sp: Optional[spotipy.Spotify] = None
        self.yt: Optional[Any] = None
        self.api_semaphore = asyncio.Semaphore(API_SEMAPHORE_LIMIT)
        self._guild_settings_cache: Dict[int, Dict[str, Any]] = {}

        bot.loop.create_task(self._initialize_apis())

    def cog_unload(self) -> None:
        self._guild_settings_cache.clear()
        log.info("TidalPlayer cog unloaded")

    async def _get_guild_settings(self, guild_id: int, force_refresh: bool = False) -> Dict[str, Any]:
        if not force_refresh and guild_id in self._guild_settings_cache:
            return self._guild_settings_cache[guild_id]
        
        settings = await self.config.guild_from_id(guild_id).all()
        self._guild_settings_cache[guild_id] = settings
        return settings

    async def _initialize_apis(self) -> None:
        await self.bot.wait_until_ready()
        creds = await self.config.all()

        await self._initialize_tidal(creds)
        await self._initialize_spotify(creds)
        await self._initialize_youtube(creds)

    async def _initialize_tidal(self, creds: Dict[str, Any]) -> None:
        if not TIDALAPI_AVAILABLE or not self.session:
            return

        required_keys = ("token_type", "access_token", "refresh_token")
        if not all(creds.get(k) for k in required_keys):
            return

        try:
            expiry = creds.get("expiry_time")
            if not expiry or datetime.fromtimestamp(expiry) > datetime.now():
                self.session.load_oauth_session(
                    creds["token_type"],
                    creds["access_token"],
                    creds["refresh_token"],
                    expiry
                )

                if self.session.check_login():
                    log.info("Tidal session loaded successfully")
        except Exception as e:
            log.error(f"Tidal session load failed: {e}", exc_info=True)

    async def _initialize_spotify(self, creds: Dict[str, Any]) -> None:
        if not SPOTIFY_AVAILABLE:
            return

        client_id = creds.get("spotify_client_id")
        client_secret = creds.get("spotify_client_secret")

        if not (client_id and client_secret):
            return

        try:
            self.sp = spotipy.Spotify(
                client_credentials_manager=SpotifyClientCredentials(client_id, client_secret)
            )
            await self.bot.loop.run_in_executor(None, lambda: self.sp.search("test", limit=1))
            log.info("Spotify client initialized successfully")
        except Exception as e:
            log.error(f"Spotify initialization failed: {e}", exc_info=True)
            self.sp = None

    async def _initialize_youtube(self, creds: Dict[str, Any]) -> None:
        if not YOUTUBE_API_AVAILABLE:
            return

        api_key = creds.get("youtube_api_key")
        if not api_key:
            return

        try:
            self.yt = build("youtube", "v3", developerKey=api_key)
            log.info("YouTube client initialized successfully")
        except Exception as e:
            log.error(f"YouTube initialization failed: {e}", exc_info=True)
            self.yt = None

    @lru_cache(maxsize=128)
    def _get_quality_label(self, quality: str) -> str:
        return QUALITY_LABELS.get(quality, "LOSSLESS (FLAC)")

    def _extract_meta(self, track: Any) -> Dict[str, Any]:
        try:
            return {
                "title": getattr(track, "name", "Unknown"),
                "artist": (
                    track.artist.name
                    if hasattr(track, "artist") and track.artist
                    else "Unknown"
                ),
                "album": (
                    track.album.name
                    if hasattr(track, "album") and track.album
                    else None
                ),
                "duration": int(getattr(track, "duration", 0) or 0),
                "quality": getattr(track, "audio_quality", "LOSSLESS")
            }
        except Exception as e:
            log.error(f"Metadata extraction error: {e}", exc_info=True)
            return {
                "title": "Unknown",
                "artist": "Unknown",
                "album": None,
                "duration": 0,
                "quality": "LOSSLESS"
            }

    @staticmethod
    def _format_time(seconds: int) -> str:
        minutes, secs = divmod(seconds, 60)
        return f"{minutes:02d}:{secs:02d}"

    def _filter_tracks(self, tracks: List[Any]) -> List[Any]:
        filtered = [
            track for track in tracks
            if not any(kw in getattr(track, "name", "").lower() for kw in FILTER_KEYWORDS)
        ]
        return filtered if filtered else tracks

    async def _add_meta(self, guild_id: int, meta: Dict[str, Any]) -> bool:
        try:
            async with self.config.guild_from_id(guild_id).track_metadata() as queue:
                if len(queue) >= MAX_QUEUE_SIZE:
                    log.warning(f"Queue full for guild {guild_id}")
                    return False
                queue.append(meta)
                return True
        except Exception as e:
            log.error(f"Add metadata error for guild {guild_id}: {e}", exc_info=True)
            return False

    async def _pop_meta(self, guild_id: int) -> None:
        try:
            async with self.config.guild_from_id(guild_id).track_metadata() as queue:
                if queue:
                    queue.pop(0)
        except Exception as e:
            log.error(f"Pop metadata error for guild {guild_id}: {e}", exc_info=True)

    async def _clear_meta(self, guild_id: int) -> None:
        try:
            await self.config.guild_from_id(guild_id).track_metadata.set([])
        except Exception as e:
            log.error(f"Clear metadata error for guild {guild_id}: {e}", exc_info=True)

    async def _should_cancel(self, guild_id: int) -> bool:
        try:
            settings = await self._get_guild_settings(guild_id)
            return settings.get("cancel_queue", False)
        except Exception as e:
            log.error(f"Check cancel error for guild {guild_id}: {e}", exc_info=True)
            return False

    async def _set_cancel(self, guild_id: int, value: bool) -> None:
        try:
            await self.config.guild_from_id(guild_id).cancel_queue.set(value)
            if guild_id in self._guild_settings_cache:
                self._guild_settings_cache[guild_id]["cancel_queue"] = value
        except Exception as e:
            log.error(f"Set cancel error for guild {guild_id}: {e}", exc_info=True)

    @async_retry()
    async def _search_tidal(self, query: str, guild_id: int) -> List[Any]:
        async with self.api_semaphore:
            try:
                result = await self.bot.loop.run_in_executor(None, self.session.search, query)
                tracks = result.get("tracks", [])

                settings = await self._get_guild_settings(guild_id)
                if settings.get("filter_remixes", True) and tracks:
                    tracks = self._filter_tracks(tracks)

                return tracks
            except Exception as e:
                log.error(f"Tidal search failed for query '{query}': {e}")
                raise APIError(f"Tidal search failed: {e}")

    async def _check_ready(self, ctx: commands.Context) -> bool:
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

    async def _interactive_select(self, ctx: commands.Context, tracks: List[Any]) -> Optional[Any]:
        """Reaction-based track selection."""
        if not tracks:
            return None

        results_to_show = min(5, len(tracks))
        description = ""

        for i, track in enumerate(tracks[:results_to_show], 1):
            meta = self._extract_meta(track)
            duration = self._format_time(meta["duration"])
            description += f"{REACTION_NUMBERS[i-1]} **{meta['title']}** - {meta['artist']} ({duration})\n"

        embed = discord.Embed(
            title="Search Results",
            description=description,
            color=discord.Color.blue()
        )
        embed.set_footer(text=Messages.STATUS_CHOOSE_TRACK.format(cancel=CANCEL_EMOJI))

        msg = await ctx.send(embed=embed)

        # Add reactions
        for i in range(results_to_show):
            await msg.add_reaction(REACTION_NUMBERS[i])
        await msg.add_reaction(CANCEL_EMOJI)

        def check(reaction, user):
            return (
                user == ctx.author
                and reaction.message.id == msg.id
                and (str(reaction.emoji) in REACTION_NUMBERS[:results_to_show] or str(reaction.emoji) == CANCEL_EMOJI)
            )

        try:
            reaction, user = await self.bot.wait_for('reaction_add', check=check, timeout=INTERACTIVE_TIMEOUT)

            if str(reaction.emoji) == CANCEL_EMOJI:
                await msg.delete()
                await ctx.send("Cancelled.")
                return None

            choice = REACTION_NUMBERS.index(str(reaction.emoji))
            await msg.delete()
            return tracks[choice]

        except asyncio.TimeoutError:
            await msg.delete()
            await ctx.send(Messages.ERROR_TIMEOUT)
            return None

    def _suppress_enqueued(self, ctx: commands.Context) -> None:
        if hasattr(ctx, "_orig_send"):
            return

        ctx._orig_send = ctx.send

        async def send_override(*args, **kwargs):
            embed = kwargs.get("embed") or (
                args[0] if args and isinstance(args[0], discord.Embed) else None
            )
            if embed and "Track Enqueued" in getattr(embed, "title", ""):
                return
            return await ctx._orig_send(*args, **kwargs)

        ctx.send = send_override

    def _restore_send(self, ctx: commands.Context) -> None:
        if hasattr(ctx, "_orig_send"):
            ctx.send = ctx._orig_send
            delattr(ctx, "_orig_send")

    async def _play(self, ctx: commands.Context, track: Any, show_embed: bool = True) -> bool:
        try:
            meta = self._extract_meta(track)

            if not await self._add_meta(ctx.guild.id, meta):
                return False

            if show_embed:
                description = f"**{meta['title']}** • {meta['artist']}"
                if meta["album"]:
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
                embed.set_footer(text=f"Duration: {self._format_time(meta['duration'])}")
                await ctx.send(embed=embed)

            url = await self.bot.loop.run_in_executor(None, track.get_url)
            if not url:
                log.error(f"Failed to get URL for track: {meta['title']}")
                return False

            self._suppress_enqueued(ctx)

            try:
                audio_cog = self.bot.get_cog("Audio")
                if audio_cog:
                    await audio_cog.command_play(ctx, query=url)
                    return True
                return False
            finally:
                self._restore_send(ctx)
        except Exception as e:
            log.error(f"Play error: {e}", exc_info=True)
            return False

    async def _search_and_queue(self, ctx: commands.Context, query: str, track_name: str) -> bool:
        try:
            tracks = await self._search_tidal(query, ctx.guild.id)
            if not tracks:
                log.info(f"No Tidal match for: {track_name}")
                return False

            return await self._play(ctx, tracks[0], show_embed=False)

        except Exception as e:
            log.error(f"Search and queue failed for '{track_name}': {e}")
            return False

    async def _queue_playlist_batch(
        self,
        ctx: commands.Context,
        tracks: List[Any],
        playlist_name: str,
        progress_msg: Optional[discord.Message] = None
    ) -> Dict[str, int]:
        self._suppress_enqueued(ctx)
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

                    if progress_msg and (i - last_update >= BATCH_UPDATE_INTERVAL or i == total):
                        try:
                            embed = discord.Embed(
                                title=Messages.PROGRESS_QUEUEING.format(name=playlist_name, count=total),
                                description=Messages.PROGRESS_UPDATE.format(
                                    queued=queued,
                                    skipped=skipped,
                                    current=i,
                                    total=total
                                ),
                                color=discord.Color.blue()
                            )
                            await progress_msg.edit(embed=embed)
                            last_update = i
                        except discord.HTTPException:
                            pass

                    await asyncio.sleep(0.05)

                except Exception as e:
                    log.error(f"Error queueing track {i}/{total}: {e}")
                    skipped += 1

            return {"queued": queued, "skipped": skipped, "total": total}

        finally:
            self._restore_send(ctx)
            await self._set_cancel(ctx.guild.id, False)

    async def _queue_spotify_playlist(self, ctx: commands.Context, playlist_id: str) -> None:
        if not SPOTIFY_AVAILABLE:
            await ctx.send(Messages.ERROR_INSTALL_SPOTIFY)
            return

        if not self.sp:
            await ctx.send(Messages.ERROR_NO_SPOTIFY)
            return

        progress_msg = await ctx.send(Messages.PROGRESS_FETCHING_SPOTIFY)

        try:
            playlist = await self.bot.loop.run_in_executor(None, self.sp.playlist, playlist_id)

            if not playlist or "tracks" not in playlist:
                await progress_msg.edit(content=Messages.ERROR_FETCH_FAILED)
                return

            tracks = playlist["tracks"]["items"]
            if not tracks:
                await progress_msg.edit(content=Messages.ERROR_NO_TRACKS_IN_CONTENT.format(content_type="playlist"))
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
                        description=Messages.STATUS_CANCELLED_WITH_SKIPPED.format(queued=queued, skipped=skipped),
                        color=discord.Color.orange()
                    )
                    await progress_msg.edit(embed=embed)
                    return

                track = item.get("track")
                if not track:
                    skipped += 1
                    continue

                try:
                    track_name = track["name"]
                    artist_name = track["artists"][0]["name"] if track.get("artists") else ""
                    query = f"{track_name} {artist_name}"

                    if await self._search_and_queue(ctx, query, track_name):
                        queued += 1
                    else:
                        skipped += 1

                    if i - last_update >= BATCH_UPDATE_INTERVAL or i == len(tracks):
                        embed = discord.Embed(
                            title=Messages.PROGRESS_QUEUEING_SPOTIFY.format(count=len(tracks)),
                            description=Messages.PROGRESS_UPDATE.format(
                                queued=queued,
                                skipped=skipped,
                                current=i,
                                total=len(tracks)
                            ),
                            color=discord.Color.green()
                        )
                        await progress_msg.edit(embed=embed)
                        last_update = i

                    await asyncio.sleep(0.05)

                except Exception as e:
                    log.error(f"Error processing Spotify track {i}: {e}")
                    skipped += 1

            embed = discord.Embed(
                title=Messages.SUCCESS_PARTIAL_QUEUE.format(
                    queued=queued,
                    total=len(tracks),
                    skipped=skipped
                ),
                description=f"Playlist: {playlist_name}",
                color=discord.Color.green()
            )
            await progress_msg.edit(embed=embed)

        except Exception as e:
            log.error(f"Spotify playlist queue error: {e}", exc_info=True)
            await progress_msg.edit(content=Messages.ERROR_FETCH_FAILED)

        finally:
            await self._set_cancel(ctx.guild.id, False)

    async def _queue_youtube_playlist(self, ctx: commands.Context, playlist_id: str) -> None:
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

            response = await self.bot.loop.run_in_executor(None, request.execute)

            if not response or "items" not in response:
                await progress_msg.edit(content=Messages.ERROR_FETCH_FAILED)
                return

            items = response["items"]
            if not items:
                await progress_msg.edit(content=Messages.ERROR_NO_TRACKS_IN_CONTENT.format(content_type="playlist"))
                return

            playlist_title = items[0]["snippet"].get("playlistTitle", "YouTube Playlist")

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
                        description=Messages.STATUS_CANCELLED_WITH_SKIPPED.format(queued=queued, skipped=skipped),
                        color=discord.Color.orange()
                    )
                    await progress_msg.edit(embed=embed)
                    return

                try:
                    video_title = item["snippet"]["title"]

                    if await self._search_and_queue(ctx, video_title, video_title):
                        queued += 1
                    else:
                        skipped += 1

                    if i - last_update >= BATCH_UPDATE_INTERVAL or i == len(items):
                        embed = discord.Embed(
                            title=Messages.PROGRESS_QUEUEING_YOUTUBE.format(count=len(items)),
                            description=Messages.PROGRESS_UPDATE.format(
                                queued=queued,
                                skipped=skipped,
                                current=i,
                                total=len(items)
                            ),
                            color=discord.Color.red()
                        )
                        await progress_msg.edit(embed=embed)
                        last_update = i

                    await asyncio.sleep(0.05)

                except Exception as e:
                    log.error(f"Error processing YouTube video {i}: {e}")
                    skipped += 1

            embed = discord.Embed(
                title=Messages.SUCCESS_PARTIAL_QUEUE.format(
                    queued=queued,
                    total=len(items),
                    skipped=skipped
                ),
                description=f"Playlist: {playlist_title}",
                color=discord.Color.red()
            )
            await progress_msg.edit(embed=embed)

        except Exception as e:
            log.error(f"YouTube playlist queue error: {e}", exc_info=True)
            await progress_msg.edit(content=Messages.ERROR_FETCH_FAILED)

        finally:
            await self._set_cancel(ctx.guild.id, False)

    async def _handle_tidal_url(self, ctx: commands.Context, url: str) -> None:
        track_match = re.search(r"tidal\.com/(?:browse/)?track/(\d+)", url)
        album_match = re.search(r"tidal\.com/(?:browse/)?album/(\d+)", url)
        playlist_match = re.search(r"tidal\.com/(?:browse/)?playlist/([a-f0-9-]+)", url)

        try:
            if track_match:
                track_id = track_match.group(1)
                track = await self.bot.loop.run_in_executor(None, self.session.track, track_id)
                if track:
                    await self._play(ctx, track)
                else:
                    await ctx.send(Messages.ERROR_NO_TRACKS_FOUND)

            elif album_match:
                album_id = album_match.group(1)
                album = await self.bot.loop.run_in_executor(None, self.session.album, album_id)

                if not album:
                    await ctx.send(Messages.ERROR_CONTENT_UNAVAILABLE)
                    return

                tracks = await self.bot.loop.run_in_executor(None, album.tracks)
                if not tracks:
                    await ctx.send(Messages.ERROR_NO_TRACKS_IN_CONTENT.format(content_type="album"))
                    return

                progress_msg = await ctx.send(
                    Messages.PROGRESS_QUEUEING.format(name=album.name, count=len(tracks))
                )

                result = await self._queue_playlist_batch(ctx, tracks, album.name, progress_msg)

                embed = discord.Embed(
                    title=Messages.SUCCESS_PARTIAL_QUEUE.format(
                        queued=result["queued"],
                        total=result["total"],
                        skipped=result["skipped"]
                    ),
                    description=f"Album: {album.name}",
                    color=discord.Color.blue()
                )
                await progress_msg.edit(embed=embed)

            elif playlist_match:
                playlist_id = playlist_match.group(1)
                playlist = await self.bot.loop.run_in_executor(None, self.session.playlist, playlist_id)

                if not playlist:
                    await ctx.send(Messages.ERROR_CONTENT_UNAVAILABLE)
                    return

                tracks = await self.bot.loop.run_in_executor(None, playlist.tracks)
                if not tracks:
                    await ctx.send(Messages.ERROR_NO_TRACKS_IN_CONTENT.format(content_type="playlist"))
                    return

                progress_msg = await ctx.send(
                    Messages.PROGRESS_QUEUEING.format(name=playlist.name, count=len(tracks))
                )

                result = await self._queue_playlist_batch(ctx, tracks, playlist.name, progress_msg)

                embed = discord.Embed(
                    title=Messages.SUCCESS_PARTIAL_QUEUE.format(
                        queued=result["queued"],
                        total=result["total"],
                        skipped=result["skipped"]
                    ),
                    description=f"Playlist: {playlist.name}",
                    color=discord.Color.blue()
                )
                await progress_msg.edit(embed=embed)

            else:
                await ctx.send(Messages.ERROR_INVALID_URL.format(
                    platform="Tidal",
                    content_type="track/album/playlist"
                ))

        except Exception as e:
            log.error(f"Tidal URL handling error: {e}", exc_info=True)
            await ctx.send(Messages.ERROR_CONTENT_UNAVAILABLE)

    @commands.command(name="tplay")
    async def tplay(self, ctx: commands.Context, *, query: str) -> None:
        """Play from Tidal, Spotify, or YouTube playlist."""
        if not await self._check_ready(ctx):
            return

        tidal_url = re.search(r"tidal\.com/(?:browse/)?(track|album|playlist)", query)
        spotify_playlist = re.search(r"open\.spotify\.com/playlist/([a-zA-Z0-9]+)", query)
        youtube_playlist = re.search(r"youtube\.com/.*[?&]list=([a-zA-Z0-9_-]+)", query)

        if tidal_url:
            await self._handle_tidal_url(ctx, query)
        elif spotify_playlist:
            playlist_id = spotify_playlist.group(1)
            await self._queue_spotify_playlist(ctx, playlist_id)
        elif youtube_playlist:
            playlist_id = youtube_playlist.group(1)
            await self._queue_youtube_playlist(ctx, playlist_id)
        else:
            try:
                tracks = await self._search_tidal(query, ctx.guild.id)

                if not tracks:
                    await ctx.send(Messages.ERROR_NO_TRACKS_FOUND)
                    return

                settings = await self._get_guild_settings(ctx.guild.id)
                
                if settings.get("interactive_search", False):
                    selected_track = await self._interactive_select(ctx, tracks)
                    if selected_track:
                        await self._play(ctx, selected_track)
                else:
                    await self._play(ctx, tracks[0])

            except APIError as e:
                await ctx.send(f"{Messages.ERROR_NO_TRACKS_FOUND} ({str(e)})")
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
        """Display the current queue."""
        try:
            queue = await self.config.guild(ctx.guild).track_metadata()

            if not queue:
                await ctx.send(Messages.STATUS_EMPTY_QUEUE)
                return

            pages = []
            items_per_page = 10

            for i in range(0, len(queue), items_per_page):
                chunk = queue[i:i + items_per_page]
                description = ""

                for j, meta in enumerate(chunk, start=i + 1):
                    duration_str = self._format_time(meta["duration"])
                    quality_str = self._get_quality_label(meta["quality"])

                    description += (
                        f"**{j}.** {meta['title']}\n"
                        f"    {meta['artist']} • {duration_str} • {quality_str}\n"
                    )

                embed = discord.Embed(
                    title=f"Queue ({len(queue)} tracks)",
                    description=description,
                    color=discord.Color.blue()
                )
                embed.set_footer(
                    text=f"Page {len(pages) + 1}/{(len(queue) - 1) // items_per_page + 1}"
                )
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
        """Clear the queue."""
        await self._clear_meta(ctx.guild.id)
        if ctx.guild.id in self._guild_settings_cache:
            del self._guild_settings_cache[ctx.guild.id]
        await ctx.send(Messages.SUCCESS_QUEUE_CLEARED)

    @commands.command(name="tfilter")
    async def tfilter(self, ctx: commands.Context) -> None:
        """Toggle remix/TikTok filtering on or off."""
        current = await self.config.guild(ctx.guild).filter_remixes()
        new_value = not current
        await self.config.guild(ctx.guild).filter_remixes.set(new_value)
        
        if ctx.guild.id in self._guild_settings_cache:
            self._guild_settings_cache[ctx.guild.id]["filter_remixes"] = new_value
        
        if new_value:
            await ctx.send(Messages.SUCCESS_FILTER_ENABLED)
        else:
            await ctx.send(Messages.SUCCESS_FILTER_DISABLED)

    @commands.command(name="tinteractive")
    async def tinteractive(self, ctx: commands.Context) -> None:
        """Toggle interactive search mode on or off."""
        current = await self.config.guild(ctx.guild).interactive_search()
        new_value = not current
        await self.config.guild(ctx.guild).interactive_search.set(new_value)
        
        if ctx.guild.id in self._guild_settings_cache:
            self._guild_settings_cache[ctx.guild.id]["interactive_search"] = new_value
        
        if new_value:
            await ctx.send(Messages.SUCCESS_INTERACTIVE_ENABLED)
        else:
            await ctx.send(Messages.SUCCESS_INTERACTIVE_DISABLED)

    @commands.is_owner()
    @commands.command(name="tidalsetup")
    async def tidalsetup(self, ctx: commands.Context) -> None:
        """Set up Tidal OAuth authentication."""
        if not TIDALAPI_AVAILABLE:
            await ctx.send(Messages.ERROR_NO_TIDALAPI)
            return

        try:
            login, future = self.session.login_oauth()

            oauth_embed = discord.Embed(
                title="Tidal OAuth Setup",
                description="Click the link below to authenticate your Tidal account:",
                color=discord.Color.blue()
            )
            oauth_embed.add_field(
                name="Login URL",
                value=f"[Click here to login]({login.verification_uri_complete})",
                inline=False
            )
            oauth_embed.add_field(
                name="Or enter code manually",
                value=f"Code: `{login.user_code}`\nURL: {login.verification_uri}",
                inline=False
            )
            oauth_embed.set_footer(text="You have 5 minutes to complete login")

            try:
                await ctx.author.send(embed=oauth_embed)
                await ctx.send("OAuth link sent to your DMs.")
            except Exception:
                await ctx.send(embed=oauth_embed)
                await ctx.send("Couldn't DM you; sent the OAuth link in this channel instead.")

            log.info(f"[TIDAL OAuth] Visit: {login.verification_uri_complete}")
            log.info(f"[TIDAL OAuth] Code: {login.user_code} at {login.verification_uri}")

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
                log.info("Tidal OAuth authentication successful")
            else:
                await ctx.send("Login failed. Please try again.")
                log.warning("Tidal OAuth authentication failed")

        except Exception as e:
            log.error(f"OAuth setup error: {e}", exc_info=True)
            await ctx.send(f"Setup failed: {str(e)}")

    @commands.is_owner()
    @commands.group(name="tidalplay")
    async def tidalplay(self, ctx: commands.Context) -> None:
        """Tidal configuration commands."""
        if ctx.invoked_subcommand is None:
            await ctx.send_help(ctx.command)

    @tidalplay.command(name="spotify")
    async def tidalplay_spotify(self, ctx: commands.Context, client_id: str, client_secret: str) -> None:
        """Configure Spotify integration."""
        if not SPOTIFY_AVAILABLE:
            await ctx.send(Messages.ERROR_INSTALL_SPOTIFY)
            return

        try:
            await self.config.spotify_client_id.set(client_id)
            await self.config.spotify_client_secret.set(client_secret)

            self.sp = spotipy.Spotify(
                client_credentials_manager=SpotifyClientCredentials(client_id, client_secret)
            )

            await self.bot.loop.run_in_executor(None, lambda: self.sp.search("test", limit=1))

            await ctx.send(Messages.SUCCESS_SPOTIFY_CONFIGURED)

        except Exception as e:
            log.error(f"Spotify configuration error: {e}", exc_info=True)
            await ctx.send(f"Spotify setup failed: {str(e)}")
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
            log.error(f"YouTube configuration error: {e}", exc_info=True)
            await ctx.send(f"YouTube setup failed: {str(e)}")
            self.yt = None

    @tidalplay.command(name="cleartokens")
    async def tidalplay_cleartokens(self, ctx: commands.Context) -> None:
        """Clear stored Tidal tokens."""
        await self.config.token_type.set(None)
        await self.config.access_token.set(None)
        await self.config.refresh_token.set(None)
        await self.config.expiry_time.set(None)
        await ctx.send(Messages.SUCCESS_TOKENS_CLEARED)

    @commands.Cog.listener()
    async def on_red_audio_track_start(self, guild: discord.Guild, track: Any, requester: discord.Member) -> None:
        try:
            await self._pop_meta(guild.id)
        except Exception as e:
            log.error(f"Track start event error for guild {guild.id}: {e}", exc_info=True)

    @commands.Cog.listener()
    async def on_red_audio_queue_end(self, guild: discord.Guild, track_count: int, total_duration: int) -> None:
        try:
            await self._clear_meta(guild.id)
            if guild.id in self._guild_settings_cache:
                del self._guild_settings_cache[guild.id]
        except Exception as e:
            log.error(f"Queue end event error for guild {guild.id}: {e}", exc_info=True)

async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(TidalPlayer(bot))
