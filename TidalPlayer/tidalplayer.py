from redbot.core import commands, Config
from redbot.core.utils.menus import menu, DEFAULT_CONTROLS
import discord
import logging
import asyncio
import re
from typing import Dict, List, Optional

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


class TidalPlayer(commands.Cog):
    """Play music from Tidal, Spotify, or YouTube via Tidal search (LOSSLESS)."""

    def __init__(self, bot):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=1234567890, force_registration=True)
        self.config.register_global(
            token_type=None,
            access_token=None,
            refresh_token=None,
            expiry_time=None,
            spotify_client_id=None,
            spotify_client_secret=None,
            youtube_api_key=None,
            quiet_mode=True,
        )
        self.config.register_guild(track_metadata=[], cancel_queue=False)
        self.session = tidalapi.Session() if TIDALAPI_AVAILABLE else None
        self.sp = None
        self.yt = None
        bot.loop.create_task(self._initialize_apis())

    async def _initialize_apis(self):
        """Initialize Tidal, Spotify, and YouTube API clients."""
        await self.bot.wait_until_ready()
        creds = await self.config.all()
        
        # Tidal OAuth
        if TIDALAPI_AVAILABLE and all(creds.get(k) for k in ("token_type", "access_token", "refresh_token")):
            try:
                self.session.load_oauth_session(
                    creds["token_type"], 
                    creds["access_token"],
                    creds["refresh_token"], 
                    creds.get("expiry_time")
                )
            except Exception as e:
                log.error(f"Tidal session load failed: {e}")
        
        # Spotify client
        if SPOTIFY_AVAILABLE and creds.get("spotify_client_id") and creds.get("spotify_client_secret"):
            try:
                self.sp = spotipy.Spotify(
                    client_credentials_manager=SpotifyClientCredentials(
                        creds["spotify_client_id"], 
                        creds["spotify_client_secret"]
                    )
                )
            except Exception as e:
                log.error(f"Spotify init failed: {e}")
        
        # YouTube API
        if YOUTUBE_API_AVAILABLE and creds.get("youtube_api_key"):
            try:
                self.yt = build("youtube", "v3", developerKey=creds["youtube_api_key"])
            except Exception as e:
                log.error(f"YouTube init failed: {e}")

    def _get_quality_label(self, quality: str) -> str:
        """Convert Tidal quality code to readable format."""
        labels = {
            "HI_RES": "HI-RES (MQA)",
            "LOSSLESS": "LOSSLESS (FLAC)",
            "HIGH": "HIGH (320kbps)",
            "LOW": "LOW (96kbps)"
        }
        return labels.get(quality, "LOSSLESS (FLAC)")

    def _extract_meta(self, track) -> Dict:
        """Extract metadata from Tidal track object."""
        return {
            "title": track.name or "Unknown",
            "artist": track.artist.name if getattr(track, "artist", None) else "Unknown",
            "album": track.album.name if getattr(track, "album", None) else None,
            "duration": int(getattr(track, "duration", 0) or 0),
            "quality": getattr(track, "audio_quality", "LOSSLESS")
        }

    async def _add_meta(self, guild_id: int, meta: Dict):
        """Add track metadata to guild queue."""
        async with self.config.guild_from_id(guild_id).track_metadata() as q:
            q.append(meta)

    async def _pop_meta(self, guild_id: int):
        """Remove first track metadata from guild queue."""
        async with self.config.guild_from_id(guild_id).track_metadata() as q:
            if q:
                q.pop(0)

    async def _clear_meta(self, guild_id: int):
        """Clear all track metadata for guild."""
        async with self.config.guild_from_id(guild_id).track_metadata() as q:
            q.clear()

    async def _should_cancel(self, guild_id: int) -> bool:
        """Check if queueing should be cancelled."""
        return await self.config.guild_from_id(guild_id).cancel_queue()

    async def _set_cancel(self, guild_id: int, value: bool):
        """Set cancel flag for guild."""
        await self.config.guild_from_id(guild_id).cancel_queue.set(value)

    def _format_time(self, seconds: int) -> str:
        """Format seconds to MM:SS."""
        m, s = divmod(seconds, 60)
        return f"{m:02d}:{s:02d}"

    async def _play(self, ctx, track, show_embed: bool = True) -> bool:
        """Queue a Tidal track via Audio cog."""
        meta = self._extract_meta(track)
        await self._add_meta(ctx.guild.id, meta)
        
        if show_embed:
            desc = f"**{meta['title']}** • {meta['artist']}"
            if meta["album"]:
                desc += f"\n_{meta['album']}_"
            embed = discord.Embed(
                title="Playing from Tidal",
                description=desc,
                color=discord.Color.blue()
            )
            embed.add_field(name="Quality", value=self._get_quality_label(meta["quality"]), inline=True)
            embed.set_footer(text=f"Duration: {self._format_time(meta['duration'])}")
            await ctx.send(embed=embed)
        
        url = await self.bot.loop.run_in_executor(None, track.get_url)
        if not url:
            return False
        
        audio_cog = self.bot.get_cog("Audio")
        if audio_cog:
            await audio_cog.command_play(ctx, query=url)
        return True

    async def _check_ready(self, ctx) -> bool:
        """Verify Tidal session and Audio cog are available."""
        if not TIDALAPI_AVAILABLE:
            await ctx.send("Error: tidalapi not installed. Run: [p]pipinstall tidalapi")
            return False
        if not self.session or not self.session.check_login():
            await ctx.send("Error: Not authenticated. Run: >tidalsetup")
            return False
        if not self.bot.get_cog("Audio"):
            await ctx.send("Error: Audio cog not loaded")
            return False
        return True

    def _suppress_enqueued(self, ctx):
        """Suppress 'Track Enqueued' messages from Audio cog."""
        if hasattr(ctx, "_orig_send"):
            return
        ctx._orig_send = ctx.send

        async def send_override(*a, **k):
            e = k.get("embed") or (a[0] if a and isinstance(a[0], discord.Embed) else None)
            if e and "Track Enqueued" in getattr(e, "title", ""):
                return
            return await ctx._orig_send(*a, **k)

        ctx.send = send_override

    def _restore_send(self, ctx):
        """Restore original send method."""
        if hasattr(ctx, "_orig_send"):
            ctx.send = ctx._orig_send
            delattr(ctx, "_orig_send")

    @commands.command(name="tplay")
    async def tplay(self, ctx, *, query: str):
        """Search and play from Tidal, or queue Spotify/YouTube playlists via Tidal search."""
        if not await self._check_ready(ctx):
            return
        
        await self._set_cancel(ctx.guild.id, False)
        
        quiet = await self.config.quiet_mode()
        if quiet:
            self._suppress_enqueued(ctx)
        
        try:
            if "open.spotify.com" in query or "spotify:" in query:
                if not SPOTIFY_AVAILABLE or not self.sp:
                    return await ctx.send("Error: Spotify not configured. Run: >tidalplay spotify <client_id> <client_secret>")
                await self._queue_spotify_playlist(ctx, query)
            elif "youtube.com" in query or "youtu.be" in query:
                if not YOUTUBE_API_AVAILABLE or not self.yt:
                    return await ctx.send("Error: YouTube not configured. Run: >tidalplay youtube <api_key>")
                await self._queue_youtube_playlist(ctx, query)
            elif "tidal.com" in query:
                await self._handle_tidal_url(ctx, query)
            else:
                await self._search_and_play(ctx, query)
        finally:
            if quiet:
                self._restore_send(ctx)

    @commands.command(name="tstop")
    async def tstop(self, ctx):
        """Stop the current playlist queueing operation."""
        await self._set_cancel(ctx.guild.id, True)
        await ctx.send("Stopping playlist queueing...")

    async def _search_and_play(self, ctx, query: str):
        """Search Tidal and play first result."""
        async with ctx.typing():
            res = await self.bot.loop.run_in_executor(None, self.session.search, query)
        tracks = res.get("tracks", [])
        if not tracks:
            return await ctx.send("No tracks found.")
        await self._play(ctx, tracks[0])

    async def _handle_tidal_url(self, ctx, url: str):
        """Queue Tidal playlist, album, or track."""
        kind = ("playlist" if "playlist/" in url else
                "album" if "album/" in url else
                "mix" if "mix/" in url else
                "track")
        
        match = re.search(rf"{kind}/([A-Za-z0-9\-]+)", url)
        if not match:
            return await ctx.send(f"Invalid Tidal {kind} URL")
        
        loader = getattr(self.session, kind)
        try:
            obj = await self.bot.loop.run_in_executor(None, loader, match.group(1))
        except Exception:
            return await ctx.send("Tidal content unavailable (private or region-locked)")
        
        items = await self.bot.loop.run_in_executor(
            None, 
            lambda: getattr(obj, "tracks", lambda: getattr(obj, "items", lambda: [])())()
        )
        name = getattr(obj, "name", getattr(obj, "title", ""))
        msg = await ctx.send(f"Queueing Tidal {kind} '{name}' ({len(items)} tracks)...")
        
        queued = 0
        for t in items:
            if await self._should_cancel(ctx.guild.id):
                await msg.edit(content=f"Cancelled. Queued {queued}/{len(items)} tracks.")
                await self._set_cancel(ctx.guild.id, False)
                return
            await self._play(ctx, t, show_embed=False)
            queued += 1
        
        await msg.edit(content=f"Queued {len(items)} tracks from {name}")

    async def _queue_spotify_playlist(self, ctx, url: str):
        """Queue Spotify playlist via Tidal search."""
        match = re.search(r"playlist/([A-Za-z0-9]+)", url)
        if not match:
            return await ctx.send("Invalid Spotify playlist URL")
        pid = match.group(1)
        
        await ctx.send("Fetching Spotify playlist...")
        
        try:
            results = await self.bot.loop.run_in_executor(
                None, 
                lambda: self.sp.playlist_items(pid, fields="items.track(name,artists),next", limit=100)
            )
            tracks = []
            while results:
                tracks.extend(results["items"])
                results = await self.bot.loop.run_in_executor(None, self.sp.next, results) if results.get("next") else None
        except Exception as e:
            log.error(f"Spotify API error: {e}")
            return await ctx.send("Could not fetch playlist. Ensure it is public and credentials are valid.")
        
        if not tracks:
            return await ctx.send("No tracks found in playlist")
        
        msg = await ctx.send(f"Queueing {len(tracks)} tracks from Spotify via Tidal search...")
        
        queued, skipped = 0, 0
        for idx, item in enumerate(tracks, 1):
            if await self._should_cancel(ctx.guild.id):
                await msg.edit(content=f"Cancelled. Queued {queued}, skipped {skipped} ({idx-1}/{len(tracks)} processed)")
                await self._set_cancel(ctx.guild.id, False)
                return
            
            tr = item.get("track")
            if not tr:
                skipped += 1
                continue
            
            artist = tr["artists"][0]["name"] if tr.get("artists") else ""
            title = tr.get("name", "")
            search_query = f"{artist} {title}"
            
            try:
                res = await self.bot.loop.run_in_executor(None, self.session.search, search_query)
                tidal_tracks = res.get("tracks", [])
                if tidal_tracks:
                    await self._play(ctx, tidal_tracks[0], show_embed=False)
                    queued += 1
                else:
                    skipped += 1
            except Exception as e:
                log.warning(f"Skipped {search_query}: {e}")
                skipped += 1
            
            if idx % 5 == 0:
                try:
                    await msg.edit(content=f"Progress: {queued} queued, {skipped} skipped ({idx}/{len(tracks)})")
                except:
                    pass
        
        await msg.edit(content=f"Complete. Queued {queued}/{len(tracks)} tracks ({skipped} not found on Tidal)")

    async def _queue_youtube_playlist(self, ctx, url: str):
        """Queue YouTube playlist via Tidal search."""
        match = re.search(r"list=([A-Za-z0-9_-]+)", url)
        if not match:
            return await ctx.send("Invalid YouTube playlist URL")
        pid = match.group(1)
        
        await ctx.send("Fetching YouTube playlist...")
        
        videos = []
        req = self.yt.playlistItems().list(part="snippet", playlistId=pid, maxResults=50)
        while req:
            res = req.execute()
            videos += [item["snippet"]["title"] for item in res["items"]]
            req = self.yt.playlistItems().list_next(req, res)
        
        if not videos:
            return await ctx.send("No videos found in playlist")
        
        msg = await ctx.send(f"Queueing {len(videos)} videos from YouTube via Tidal search...")
        
        queued, skipped = 0, 0
        for idx, title in enumerate(videos, 1):
            if await self._should_cancel(ctx.guild.id):
                await msg.edit(content=f"Cancelled. Queued {queued}, skipped {skipped} ({idx-1}/{len(videos)} processed)")
                await self._set_cancel(ctx.guild.id, False)
                return
            
            try:
                res = await self.bot.loop.run_in_executor(None, self.session.search, title)
                tidal_tracks = res.get("tracks", [])
                if tidal_tracks:
                    await self._play(ctx, tidal_tracks[0], show_embed=False)
                    queued += 1
                else:
                    skipped += 1
            except Exception as e:
                log.warning(f"Skipped {title}: {e}")
                skipped += 1
            
            if idx % 5 == 0:
                try:
                    await msg.edit(content=f"Progress: {queued} queued, {skipped} skipped ({idx}/{len(videos)})")
                except:
                    pass
        
        await msg.edit(content=f"Complete. Queued {queued}/{len(videos)} videos ({skipped} not found on Tidal)")

    @commands.Cog.listener()
    async def on_red_audio_track_start(self, guild, track, requester):
        """Pop metadata when a track starts."""
        await self._pop_meta(guild.id)

    @commands.Cog.listener()
    async def on_red_audio_queue_end(self, guild, track_history, req_history):
        """Clear all metadata when queue ends."""
        await self._clear_meta(guild.id)

    @commands.command(name="tqueue")
    async def tqueue(self, ctx):
        """Display the Tidal queue with metadata."""
        data: List[Dict] = await self.config.guild(ctx.guild).track_metadata()
        if not data:
            return await ctx.send("The queue is empty.")
        
        embeds = []
        for i in range(0, len(data), 10):
            chunk = data[i:i+10]
            desc = "\n".join(
                f"`{i+j+1}.` **{m['title']}** • {m['artist']} • `{self._format_time(m['duration'])}`"
                for j, m in enumerate(chunk)
            )
            embeds.append(discord.Embed(title="Tidal Queue", description=desc, color=discord.Color.blue()))
        
        await menu(ctx, embeds, DEFAULT_CONTROLS)

    @commands.command(name="tclear")
    async def tclear(self, ctx):
        """Clear the Tidal metadata queue."""
        await self._clear_meta(ctx.guild.id)
        await ctx.send("Tidal queue metadata cleared.")

    @commands.command(name="tidalsetup")
    @commands.is_owner()
    async def tidalsetup(self, ctx):
        """Setup Tidal OAuth authentication."""
        if not TIDALAPI_AVAILABLE:
            return await ctx.send("Error: Install tidalapi with [p]pipinstall tidalapi")
        
        login, fut = self.session.login_oauth()
        embed = discord.Embed(
            title="Tidal OAuth Setup",
            description=f"Visit:\n{login.verification_uri_complete}",
            color=discord.Color.blue()
        )
        embed.set_footer(text="Expires in 5 minutes")
        await ctx.send(embed=embed)
        
        try:
            await asyncio.wait_for(self.bot.loop.run_in_executor(None, fut.result), timeout=300)
        except asyncio.TimeoutError:
            return await ctx.send("OAuth timed out.")
        
        if self.session.check_login():
            await self.config.token_type.set(self.session.token_type)
            await self.config.access_token.set(self.session.access_token)
            await self.config.refresh_token.set(self.session.refresh_token)
            if hasattr(self.session, "expiry_time"):
                await self.config.expiry_time.set(self.session.expiry_time.timestamp())
            await ctx.send("Tidal setup complete.")
        else:
            await ctx.send("Login failed.")

    @commands.group(name="tidalplay", invoke_without_command=True)
    @commands.is_owner()
    async def tidalplay(self, ctx):
        """Configuration commands."""
        await ctx.send_help()

    @tidalplay.command(name="spotify")
    @commands.is_owner()
    async def spotify_setup(self, ctx, client_id: str, client_secret: str):
        """Configure Spotify API credentials."""
        if not SPOTIFY_AVAILABLE:
            return await ctx.send("Error: Install spotipy with: pip install spotipy")
        
        await self.config.spotify_client_id.set(client_id)
        await self.config.spotify_client_secret.set(client_secret)
        self.sp = spotipy.Spotify(
            client_credentials_manager=SpotifyClientCredentials(client_id, client_secret)
        )
        await ctx.send("Spotify credentials saved.")

    @tidalplay.command(name="youtube")
    @commands.is_owner()
    async def youtube_setup(self, ctx, api_key: str):
        """Configure YouTube Data API key."""
        if not YOUTUBE_API_AVAILABLE:
            return await ctx.send("Error: Install google-api-python-client")
        
        await self.config.youtube_api_key.set(api_key)
        self.yt = build("youtube", "v3", developerKey=api_key)
        await ctx.send("YouTube API key saved.")

    @commands.command(name="tidalquiet")
    @commands.is_owner()
    async def tidalquiet(self, ctx, mode: Optional[str] = None):
        """Toggle quiet mode (suppresses Audio cog queue messages)."""
        if mode not in (None, "on", "off"):
            return await ctx.send("Usage: >tidalquiet [on|off]")
        
        if mode is None:
            status = "enabled" if await self.config.quiet_mode() else "disabled"
            return await ctx.send(f"Quiet mode is {status}.")
        
        await self.config.quiet_mode.set(mode == "on")
        await ctx.send(f"Quiet mode {mode}.")


def setup(bot):
    bot.add_cog(TidalPlayer(bot))