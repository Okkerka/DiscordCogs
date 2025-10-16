"""
TidalPlayer - A Red-DiscordBot cog for playing music from Tidal.

This cog allows users to play music from Tidal with lossless quality,
and supports importing playlists from Spotify and YouTube by searching
equivalent tracks on Tidal.
"""

from redbot.core import commands, Config
from redbot.core.utils.menus import menu, DEFAULT_CONTROLS
import discord
import logging
import asyncio
import re
from typing import Dict, List, Optional, Any, Callable
from datetime import datetime
from functools import wraps

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

# Configuration Constants
MAX_QUEUE_SIZE = 1000
BATCH_UPDATE_INTERVAL = 5
API_SEMAPHORE_LIMIT = 3
SEARCH_RETRY_ATTEMPTS = 2
COG_IDENTIFIER = 160819386


class Messages:
    """Centralized message templates for user feedback."""

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

    PROGRESS_QUEUEING = "Queueing {name} ({count} tracks)..."
    PROGRESS_FETCHING_SPOTIFY = "Fetching Spotify playlist..."
    PROGRESS_FETCHING_YOUTUBE = "Fetching YouTube playlist..."
    PROGRESS_QUEUEING_SPOTIFY = "Queueing {count} tracks from Spotify..."
    PROGRESS_QUEUEING_YOUTUBE = "Queueing {count} videos from YouTube..."
    PROGRESS_UPDATE = "{queued} queued, {skipped} skipped ({current}/{total})"
    PROGRESS_OAUTH = "Starting OAuth... Check your console for the login link!"
    PROGRESS_OAUTH_PENDING = "Complete login in console, then run `>tidalsetup` again."

    STATUS_STOPPING = "Stopping playlist queueing..."
    STATUS_CANCELLED = "Cancelled. Queued {queued}/{total}."
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


class TidalPlayerError(Exception):
    """Base exception for TidalPlayer errors."""
    pass


class AuthenticationError(TidalPlayerError):
    """Raised when authentication fails."""
    pass


class APIError(TidalPlayerError):
    """Raised when API calls fail."""
    pass


def async_retry(max_attempts: int = SEARCH_RETRY_ATTEMPTS, delay: float = 0.5):
    """
    Decorator for retrying async functions on failure.

    Parameters:
        max_attempts (int): Maximum number of retry attempts.
        delay (float): Delay between retries in seconds.

    Returns:
        Callable: Decorated function with retry logic.
    """
    def decorator(func: Callable) -> Callable:
        @wraps(func)
        async def wrapper(*args, **kwargs):
            for attempt in range(max_attempts):
                try:
                    return await func(*args, **kwargs)
                except Exception as e:
                    if attempt == max_attempts - 1:
                        raise
                    log.warning(f"Attempt {attempt + 1} failed for {func.__name__}: {e}")
                    await asyncio.sleep(delay)
            return None
        return wrapper
    return decorator


class TidalPlayer(commands.Cog):
    """
    Play music from Tidal, Spotify, or YouTube via Tidal search (LOSSLESS).

    This cog integrates with Tidal's API to provide lossless music playback
    through Discord. It supports searching and playing individual tracks,
    as well as importing entire playlists from Tidal, Spotify, and YouTube.
    """

    def __init__(self, bot: commands.Bot):
        """
        Initialize the TidalPlayer cog.

        Parameters:
            bot (commands.Bot): The Red-DiscordBot instance.
        """
        self.bot = bot
        self.config = Config.get_conf(
            self, 
            identifier=COG_IDENTIFIER, 
            force_registration=True
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
            cancel_queue=False
        )

        self.session: Optional[tidalapi.Session] = (
            tidalapi.Session() if TIDALAPI_AVAILABLE else None
        )
        self.sp: Optional[spotipy.Spotify] = None
        self.yt: Optional[Any] = None
        self.api_semaphore = asyncio.Semaphore(API_SEMAPHORE_LIMIT)

        bot.loop.create_task(self._initialize_apis())

    def cog_unload(self) -> None:
        """Clean up resources when the cog is unloaded."""
        log.info("TidalPlayer cog unloaded")

    async def _initialize_apis(self) -> None:
        """
        Initialize API clients on bot startup.

        Loads stored credentials and attempts to establish sessions
        with Tidal, Spotify, and YouTube APIs.
        """
        await self.bot.wait_until_ready()
        creds = await self.config.all()

        await self._initialize_tidal(creds)
        await self._initialize_spotify(creds)
        await self._initialize_youtube(creds)

    async def _initialize_tidal(self, creds: Dict[str, Any]) -> None:
        """
        Initialize Tidal session with stored credentials.

        Parameters:
            creds (Dict[str, Any]): Dictionary containing stored credentials.
        """
        if not TIDALAPI_AVAILABLE:
            return

        required_keys = ("token_type", "access_token", "refresh_token")
        if not all(creds.get(k) for k in required_keys):
            return

        try:
            expiry = creds.get("expiry_time")
            if not expiry or datetime.fromtimestamp(expiry) > datetime.now():
                await self.bot.loop.run_in_executor(
                    None,
                    self.session.load_oauth_session,
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
        """
        Initialize Spotify client with stored credentials.

        Parameters:
            creds (Dict[str, Any]): Dictionary containing stored credentials.
        """
        if not SPOTIFY_AVAILABLE:
            return

        client_id = creds.get("spotify_client_id")
        client_secret = creds.get("spotify_client_secret")

        if not (client_id and client_secret):
            return

        try:
            self.sp = spotipy.Spotify(
                client_credentials_manager=SpotifyClientCredentials(
                    client_id,
                    client_secret
                )
            )
            await self.bot.loop.run_in_executor(
                None,
                lambda: self.sp.search("test", limit=1)
            )
            log.info("Spotify client initialized successfully")
        except Exception as e:
            log.error(f"Spotify initialization failed: {e}", exc_info=True)
            self.sp = None

    async def _initialize_youtube(self, creds: Dict[str, Any]) -> None:
        """
        Initialize YouTube client with stored credentials.

        Parameters:
            creds (Dict[str, Any]): Dictionary containing stored credentials.
        """
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

    def _get_quality_label(self, quality: str) -> str:
        """
        Convert quality code to human-readable format.

        Parameters:
            quality (str): Quality code from Tidal API.

        Returns:
            str: Human-readable quality label.
        """
        return QUALITY_LABELS.get(quality, "LOSSLESS (FLAC)")

    def _extract_meta(self, track: Any) -> Dict[str, Any]:
        """
        Extract metadata from a Tidal track object.

        Parameters:
            track (Any): Tidal track object.

        Returns:
            Dict[str, Any]: Dictionary containing track metadata.
        """
        try:
            return {
                "title": getattr(track, "name", None) or "Unknown",
                "artist": (
                    getattr(track.artist, "name", "Unknown")
                    if hasattr(track, "artist") and track.artist
                    else "Unknown"
                ),
                "album": (
                    getattr(track.album, "name", None)
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

    def _format_time(self, seconds: int) -> str:
        """
        Format seconds as MM:SS.

        Parameters:
            seconds (int): Duration in seconds.

        Returns:
            str: Formatted time string.
        """
        minutes, secs = divmod(seconds, 60)
        return f"{minutes:02d}:{secs:02d}"

    async def _add_meta(self, guild_id: int, meta: Dict[str, Any]) -> bool:
        """
        Add track metadata to the guild queue.

        Parameters:
            guild_id (int): Discord guild ID.
            meta (Dict[str, Any]): Track metadata dictionary.

        Returns:
            bool: True if metadata was added.
        """
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
        """
        Remove the first track metadata from the guild queue.

        Parameters:
            guild_id (int): Discord guild ID.
        """
        try:
            async with self.config.guild_from_id(guild_id).track_metadata() as queue:
                if queue:
                    queue.pop(0)
        except Exception as e:
            log.error(f"Pop metadata error for guild {guild_id}: {e}", exc_info=True)

    async def _clear_meta(self, guild_id: int) -> None:
        """
        Clear all track metadata from the guild queue.

        Parameters:
            guild_id (int): Discord guild ID.
        """
        try:
            await self.config.guild_from_id(guild_id).track_metadata.set([])
        except Exception as e:
            log.error(f"Clear metadata error for guild {guild_id}: {e}", exc_info=True)

    async def _should_cancel(self, guild_id: int) -> bool:
        """
        Check if playlist queueing should be cancelled.

        Parameters:
            guild_id (int): Discord guild ID.

        Returns:
            bool: True if queueing should be cancelled.
        """
        try:
            return await self.config.guild_from_id(guild_id).cancel_queue()
        except Exception as e:
            log.error(f"Check cancel error for guild {guild_id}: {e}", exc_info=True)
            return False

    async def _set_cancel(self, guild_id: int, value: bool) -> None:
        """
        Set the cancel flag for playlist queueing.

        Parameters:
            guild_id (int): Discord guild ID.
            value (bool): Cancel flag value.
        """
        try:
            await self.config.guild_from_id(guild_id).cancel_queue.set(value)
        except Exception as e:
            log.error(f"Set cancel error for guild {guild_id}: {e}", exc_info=True)

    @async_retry(max_attempts=SEARCH_RETRY_ATTEMPTS)
    async def _search_tidal(self, query: str) -> List[Any]:
        """
        Search Tidal with rate limiting and automatic retry.

        Parameters:
            query (str): Search query string.

        Returns:
            List[Any]: List of track objects from Tidal API.
        """
        async with self.api_semaphore:
            try:
                result = await self.bot.loop.run_in_executor(
                    None,
                    self.session.search,
                    query
                )
                return result.get("tracks", [])
            except Exception as e:
                log.error(f"Tidal search failed for query '{query}': {e}")
                raise APIError(f"Tidal search failed: {e}")

    async def _check_ready(self, ctx: commands.Context) -> bool:
        """
        Verify that all required components are ready for playback.

        Parameters:
            ctx (commands.Context): Discord context.

        Returns:
            bool: True if ready, False otherwise.
        """
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

    def _suppress_enqueued(self, ctx: commands.Context) -> None:
        """
        Suppress Track Enqueued messages from the Audio cog.

        Parameters:
            ctx (commands.Context): Discord context to modify.
        """
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
        """
        Restore the original send method to the context.

        Parameters:
            ctx (commands.Context): Discord context to restore.
        """
        if hasattr(ctx, "_orig_send"):
            ctx.send = ctx._orig_send
            delattr(ctx, "_orig_send")

    async def _play(
        self,
        ctx: commands.Context,
        track: Any,
        show_embed: bool = True
    ) -> bool:
        """
        Queue a track via the Audio cog.

        Parameters:
            ctx (commands.Context): Discord context.
            track (Any): Tidal track object.
            show_embed (bool): Whether to display track information embed.

        Returns:
            bool: True if track was queued successfully.
        """
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

            audio_cog = self.bot.get_cog("Audio")
            if audio_cog:
                await audio_cog.command_play(ctx, query=url)
                return True

            return False
        except Exception as e:
            log.error(f"Play error: {e}", exc_info=True)
            return False

    async def _search_and_queue(
        self,
        ctx: commands.Context,
        query: str,
        track_name: str
    ) -> bool:
        """
        Search Tidal for a track and queue the best match.

        Parameters:
            ctx (commands.Context): Discord context.
            query (str): Search query string.
            track_name (str): Original track name for logging.

        Returns:
            bool: True if track was found and queued successfully.
        """
        try:
            tracks = await self._search_tidal(query)
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
        """
        Queue multiple tracks from a playlist with progress updates.

        Parameters:
            ctx (commands.Context): Discord context.
            tracks (List[Any]): List of track objects.
            playlist_name (str): Name of the playlist for status messages.
            progress_msg (Optional[discord.Message]): Message to update with progress.

        Returns:
            Dict[str, int]: Dictionary with queued and skipped counts.
        """
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
                                title=Messages.PROGRESS_QUEUEING.format(
                                    name=playlist_name,
                                    count=total
                                ),
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

                    await asyncio.sleep(0.1)

                except Exception as e:
                    log.error(f"Error queueing track {i}/{total}: {e}")
                    skipped += 1

            return {"queued": queued, "skipped": skipped, "total": total}

        finally:
            self._restore_send(ctx)
            await self._set_cancel(ctx.guild.id, False)

    async def _queue_spotify_playlist(
        self,
        ctx: commands.Context,
        playlist_id: str
    ) -> None:
        """
        Queue tracks from a Spotify playlist by searching on Tidal.

        Parameters:
            ctx (commands.Context): Discord context.
            playlist_id (str): Spotify playlist ID.
        """
        if not SPOTIFY_AVAILABLE:
            await ctx.send(Messages.ERROR_INSTALL_SPOTIFY)
            return

        if not self.sp:
            await ctx.send(Messages.ERROR_NO_SPOTIFY)
            return

        progress_msg = await ctx.send(Messages.PROGRESS_FETCHING_SPOTIFY)

        try:
            playlist = await self.bot.loop.run_in_executor(
                None,
                self.sp.playlist,
                playlist_id
            )

            if not playlist or "tracks" not in playlist:
                await progress_msg.edit(content=Messages.ERROR_FETCH_FAILED)
                return

            tracks = playlist["tracks"]["items"]
            if not tracks:
                await progress_msg.edit(
                    content=Messages.ERROR_NO_TRACKS_IN_CONTENT.format(content_type="playlist")
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
                        description=Messages.STATUS_CANCELLED_WITH_SKIPPED.format(
                            queued=queued,
                            skipped=skipped
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

                    await asyncio.sleep(0.1)

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

    async def _queue_youtube_playlist(
        self,
        ctx: commands.Context,
        playlist_id: str
    ) -> None:
        """
        Queue tracks from a YouTube playlist by searching on Tidal.

        Parameters:
            ctx (commands.Context): Discord context.
            playlist_id (str): YouTube playlist ID.
        """
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
                await progress_msg.edit(
                    content=Messages.ERROR_NO_TRACKS_IN_CONTENT.format(content_type="playlist")
                )
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
                        description=Messages.STATUS_CANCELLED_WITH_SKIPPED.format(
                            queued=queued,
                            skipped=skipped
                        ),
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

                    await asyncio.sleep(0.1)

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
        """
        Handle Tidal URLs for tracks, albums, or playlists.

        Parameters:
            ctx (commands.Context): Discord context.
            url (str): Tidal URL to process.
        """
        track_match = re.search(r"tidal\.com/(?:browse/)?track/(\d+)", url)
        album_match = re.search(r"tidal\.com/(?:browse/)?album/(\d+)", url)
        playlist_match = re.search(r"tidal\.com/(?:browse/)?playlist/([a-f0-9-]+)", url)

        try:
            if track_match:
                track_id = track_match.group(1)
                track = await self.bot.loop.run_in_executor(
                    None,
                    self.session.track,
                    track_id
                )
                if track:
                    await self._play(ctx, track)
                else:
                    await ctx.send(Messages.ERROR_NO_TRACKS_FOUND)

            elif album_match:
                album_id = album_match.group(1)
                album = await self.bot.loop.run_in_executor(
                    None,
                    self.session.album,
                    album_id
                )

                if not album:
                    await ctx.send(Messages.ERROR_CONTENT_UNAVAILABLE)
                    return

                tracks = await self.bot.loop.run_in_executor(None, album.tracks)
                if not tracks:
                    await ctx.send(Messages.ERROR_NO_TRACKS_IN_CONTENT.format(content_type="album"))
                    return

                progress_msg = await ctx.send(
                    Messages.PROGRESS_QUEUEING.format(
                        name=album.name,
                        count=len(tracks)
                    )
                )

                result = await self._queue_playlist_batch(
                    ctx,
                    tracks,
                    album.name,
                    progress_msg
                )

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
                playlist = await self.bot.loop.run_in_executor(
                    None,
                    self.session.playlist,
                    playlist_id
                )

                if not playlist:
                    await ctx.send(Messages.ERROR_CONTENT_UNAVAILABLE)
                    return

                tracks = await self.bot.loop.run_in_executor(None, playlist.tracks)
                if not tracks:
                    await ctx.send(Messages.ERROR_NO_TRACKS_IN_CONTENT.format(content_type="playlist"))
                    return

                progress_msg = await ctx.send(
                    Messages.PROGRESS_QUEUEING.format(
                        name=playlist.name,
                        count=len(tracks)
                    )
                )

                result = await self._queue_playlist_batch(
                    ctx,
                    tracks,
                    playlist.name,
                    progress_msg
                )

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
        """
        Play a track or queue a playlist from Tidal, Spotify, or YouTube.

        Parameters:
            ctx (commands.Context): Discord context.
            query (str): Search query or URL.

        Usage:
            >tplay <search query>
            >tplay <Tidal URL>
            >tplay <Spotify playlist URL>
            >tplay <YouTube playlist URL>
        """
        if not await self._check_ready(ctx):
            return

        tidal_url = re.search(r"tidal\.com/(?:browse/)?(track|album|playlist)", query)
        spotify_playlist = re.search(
            r"open\.spotify\.com/playlist/([a-zA-Z0-9]+)",
            query
        )
        youtube_playlist = re.search(
            r"youtube\.com/.*[?&]list=([a-zA-Z0-9_-]+)",
            query
        )

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
                tracks = await self._search_tidal(query)

                if not tracks:
                    await ctx.send(Messages.ERROR_NO_TRACKS_FOUND)
                    return

                await self._play(ctx, tracks[0])

            except APIError as e:
                await ctx.send(f"{Messages.ERROR_NO_TRACKS_FOUND} ({str(e)})")
            except Exception as e:
                log.error(f"Search error: {e}", exc_info=True)
                await ctx.send(Messages.ERROR_NO_TRACKS_FOUND)

    @commands.command(name="tstop")
    async def tstop(self, ctx: commands.Context) -> None:
        """
        Stop queueing a playlist.

        Parameters:
            ctx (commands.Context): Discord context.

        Usage:
            >tstop
        """
        await self._set_cancel(ctx.guild.id, True)
        await ctx.send(Messages.STATUS_STOPPING)

    @commands.command(name="tqueue")
    async def tqueue(self, ctx: commands.Context) -> None:
        """
        Display the current queue with track metadata.

        Parameters:
            ctx (commands.Context): Discord context.

        Usage:
            >tqueue
        """
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
        """
        Clear the queue metadata.

        Parameters:
            ctx (commands.Context): Discord context.

        Usage:
            >tclear
        """
        await self._clear_meta(ctx.guild.id)
        await ctx.send(Messages.SUCCESS_QUEUE_CLEARED)

    @commands.command(name="tidalsetup")
    async def tidalsetup(self, ctx: commands.Context) -> None:
        """
        Set up Tidal OAuth authentication.

        Parameters:
            ctx (commands.Context): Discord context.

        Usage:
            >tidalsetup

        Notes:
            This command initiates the OAuth flow. Follow the console
            instructions to complete authentication.
        """
        if not TIDALAPI_AVAILABLE:
            await ctx.send(Messages.ERROR_NO_TIDALAPI)
            return

        try:
            login, future = await self.bot.loop.run_in_executor(
                None,
                self.session.login_oauth
            )
            await ctx.send(Messages.PROGRESS_OAUTH)
            print(f"\n[TIDAL] Visit: {login.verification_uri_complete}")
            print(f"[TIDAL] Or enter code: {login.user_code} at {login.verification_uri}\n")
            
            try:
                await asyncio.wait_for(future, timeout=300)
            except asyncio.TimeoutError:
                await ctx.send("OAuth timeout. Please try again.")
                return
            
            if self.session.check_login():
                await self.config.token_type.set(self.session.token_type)
                await self.config.access_token.set(self.session.access_token)
                await self.config.refresh_token.set(self.session.refresh_token)
                
                expiry = None
                if self.session.expiry_time:
                    expiry = int(self.session.expiry_time.timestamp())
                await self.config.expiry_time.set(expiry)
                
                await ctx.send(Messages.SUCCESS_TIDAL_SETUP)
            else:
                await ctx.send("Login failed. Please try again.")
        
        except Exception as e:
            log.error(f"OAuth setup error: {e}", exc_info=True)
            await ctx.send(f"Setup failed: {str(e)}")
            

    @commands.group(name="tidalplay")
    @commands.is_owner()
    async def tidalplay(self, ctx: commands.Context) -> None:
        """
        Configure Spotify and YouTube API credentials.

        Parameters:
            ctx (commands.Context): Discord context.

        Usage:
            >tidalplay spotify <client_id> <client_secret>
            >tidalplay youtube <api_key>
        """
        if ctx.invoked_subcommand is None:
            await ctx.send_help(ctx.command)

    @tidalplay.command(name="spotify")
    async def tidalplay_spotify(
        self,
        ctx: commands.Context,
        client_id: str,
        client_secret: str
    ) -> None:
        """
        Configure Spotify API credentials.

        Parameters:
            ctx (commands.Context): Discord context.
            client_id (str): Spotify client ID.
            client_secret (str): Spotify client secret.

        Usage:
            >tidalplay spotify <client_id> <client_secret>
        """
        if not SPOTIFY_AVAILABLE:
            await ctx.send(Messages.ERROR_INSTALL_SPOTIFY)
            return

        try:
            await self.config.spotify_client_id.set(client_id)
            await self.config.spotify_client_secret.set(client_secret)

            self.sp = spotipy.Spotify(
                client_credentials_manager=SpotifyClientCredentials(
                    client_id,
                    client_secret
                )
            )

            await self.bot.loop.run_in_executor(
                None,
                lambda: self.sp.search("test", limit=1)
            )

            await ctx.send(Messages.SUCCESS_SPOTIFY_CONFIGURED)

        except Exception as e:
            log.error(f"Spotify configuration error: {e}", exc_info=True)
            await ctx.send(f"Spotify setup failed: {str(e)}")
            self.sp = None

    @tidalplay.command(name="youtube")
    async def tidalplay_youtube(self, ctx: commands.Context, api_key: str) -> None:
        """
        Configure YouTube API credentials.

        Parameters:
            ctx (commands.Context): Discord context.
            api_key (str): YouTube Data API key.

        Usage:
            >tidalplay youtube <api_key>
        """
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
        """
        Clear stored Tidal OAuth tokens.

        Parameters:
            ctx (commands.Context): Discord context.

        Usage:
            >tidalplay cleartokens

        Notes:
            Use this if you encounter authentication issues.
        """
        await self.config.token_type.set(None)
        await self.config.access_token.set(None)
        await self.config.refresh_token.set(None)
        await self.config.expiry_time.set(None)
        await ctx.send(Messages.SUCCESS_TOKENS_CLEARED)

    @commands.Cog.listener()
    async def on_red_audio_track_start(
        self,
        guild: discord.Guild,
        track: Any,
        requester: discord.Member
    ) -> None:
        """
        Handle track start events from the Audio cog.

        Parameters:
            guild (discord.Guild): Guild where track started.
            track (Any): Track object from Audio cog.
            requester (discord.Member): Member who requested the track.
        """
        try:
            await self._pop_meta(guild.id)
        except Exception as e:
            log.error(f"Track start event error for guild {guild.id}: {e}", exc_info=True)

    @commands.Cog.listener()
    async def on_red_audio_queue_end(
        self,
        guild: discord.Guild,
        track_count: int,
        total_duration: int
    ) -> None:
        """
        Handle queue end events from the Audio cog.

        Parameters:
            guild (discord.Guild): Guild where queue ended.
            track_count (int): Number of tracks that were played.
            total_duration (int): Total duration of all tracks.
        """
        try:
            await self._clear_meta(guild.id)
        except Exception as e:
            log.error(f"Queue end event error for guild {guild.id}: {e}", exc_info=True)


async def setup(bot: commands.Bot) -> None:
    """
    Register the TidalPlayer cog with the bot.

    Parameters:
        bot (commands.Bot): The Red-DiscordBot instance.
    """
    await bot.add_cog(TidalPlayer(bot))