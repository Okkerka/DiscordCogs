from redbot.core import commands, Config
from redbot.core.utils.menus import menu, DEFAULT_CONTROLS
import discord
import logging
import asyncio
import re
from typing import Dict, List
from datetime import datetime

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

# Constants
MAX_QUEUE_SIZE = 1000
BATCH_UPDATE_INTERVAL = 5
API_SEMAPHORE_LIMIT = 3
SEARCH_RETRY_ATTEMPTS = 2


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
        )
        self.config.register_guild(track_metadata=[], cancel_queue=False)
        self.session = tidalapi.Session() if TIDALAPI_AVAILABLE else None
        self.sp = None
        self.yt = None
        self.api_semaphore = asyncio.Semaphore(API_SEMAPHORE_LIMIT)
        bot.loop.create_task(self._initialize_apis())

    def cog_unload(self):
        """Clean up on unload."""
        log.info("TidalPlayer cog unloaded")

    async def _initialize_apis(self):
        """Initialize API clients on bot startup."""
        await self.bot.wait_until_ready()
        creds = await self.config.all()
        
        # Tidal session
        if TIDALAPI_AVAILABLE and all(creds.get(k) for k in ("token_type", "access_token", "refresh_token")):
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
                        log.info("Tidal session loaded")
            except Exception as e:
                log.error(f"Tidal load failed: {e}")
        
        # Spotify
        if SPOTIFY_AVAILABLE and creds.get("spotify_client_id") and creds.get("spotify_client_secret"):
            try:
                self.sp = spotipy.Spotify(
                    client_credentials_manager=SpotifyClientCredentials(
                        creds["spotify_client_id"],
                        creds["spotify_client_secret"]
                    )
                )
                await self.bot.loop.run_in_executor(None, lambda: self.sp.search("test", limit=1))
                log.info("Spotify initialized")
            except Exception as e:
                log.error(f"Spotify init failed: {e}")
                self.sp = None
        
        # YouTube
        if YOUTUBE_API_AVAILABLE and creds.get("youtube_api_key"):
            try:
                self.yt = build("youtube", "v3", developerKey=creds["youtube_api_key"])
                log.info("YouTube initialized")
            except Exception as e:
                log.error(f"YouTube init failed: {e}")
                self.yt = None

    def _get_quality_label(self, quality: str) -> str:
        """Convert quality code to readable format."""
        return {
            "HI_RES": "HI-RES (MQA)",
            "HI_RES_LOSSLESS": "HI-RES LOSSLESS",
            "LOSSLESS": "LOSSLESS (FLAC)",
            "HIGH": "HIGH (320kbps)",
            "LOW": "LOW (96kbps)"
        }.get(quality, "LOSSLESS (FLAC)")

    def _extract_meta(self, track) -> Dict:
        """Extract track metadata."""
        try:
            return {
                "title": getattr(track, "name", None) or "Unknown",
                "artist": getattr(track.artist, "name", "Unknown") if hasattr(track, "artist") and track.artist else "Unknown",
                "album": getattr(track.album, "name", None) if hasattr(track, "album") and track.album else None,
                "duration": int(getattr(track, "duration", 0) or 0),
                "quality": getattr(track, "audio_quality", "LOSSLESS")
            }
        except Exception as e:
            log.error(f"Metadata extraction error: {e}")
            return {"title": "Unknown", "artist": "Unknown", "album": None, "duration": 0, "quality": "LOSSLESS"}

    async def _add_meta(self, guild_id: int, meta: Dict) -> bool:
        """Add metadata to queue."""
        try:
            async with self.config.guild_from_id(guild_id).track_metadata() as q:
                if len(q) >= MAX_QUEUE_SIZE:
                    return False
                q.append(meta)
                return True
        except Exception as e:
            log.error(f"Add metadata error: {e}")
            return False

    async def _pop_meta(self, guild_id: int):
        """Remove first track metadata."""
        try:
            async with self.config.guild_from_id(guild_id).track_metadata() as q:
                if q:
                    q.pop(0)
        except:
            pass

    async def _clear_meta(self, guild_id: int):
        """Clear all metadata."""
        try:
            await self.config.guild_from_id(guild_id).track_metadata.set([])
        except:
            pass

    async def _should_cancel(self, guild_id: int) -> bool:
        """Check cancel flag."""
        try:
            return await self.config.guild_from_id(guild_id).cancel_queue()
        except:
            return False

    async def _set_cancel(self, guild_id: int, value: bool):
        """Set cancel flag."""
        try:
            await self.config.guild_from_id(guild_id).cancel_queue.set(value)
        except:
            pass

    def _format_time(self, seconds: int) -> str:
        """Format seconds to MM:SS."""
        m, s = divmod(seconds, 60)
        return f"{m:02d}:{s:02d}"

    async def _search_tidal(self, query: str, retry: int = 0) -> List:
        """Search Tidal with rate limiting and retry."""
        async with self.api_semaphore:
            try:
                res = await self.bot.loop.run_in_executor(None, self.session.search, query)
                return res.get("tracks", [])
            except Exception as e:
                if retry < SEARCH_RETRY_ATTEMPTS:
                    await asyncio.sleep(0.5)
                    return await self._search_tidal(query, retry + 1)
                log.error(f"Search failed: {query} - {e}")
                return []

    async def _play(self, ctx, track, show_embed: bool = True) -> bool:
        """Queue track via Audio cog."""
        try:
            meta = self._extract_meta(track)
            if not await self._add_meta(ctx.guild.id, meta):
                return False
            
            if show_embed:
                desc = f"**{meta['title']}** • {meta['artist']}"
                if meta["album"]:
                    desc += f"\n_{meta['album']}_"
                embed = discord.Embed(title="🎵 Playing from Tidal", description=desc, color=discord.Color.blue())
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
            return False
        except Exception as e:
            log.error(f"Play error: {e}")
            return False

    async def _check_ready(self, ctx) -> bool:
        """Verify session and Audio cog."""
        if not TIDALAPI_AVAILABLE:
            await ctx.send("❌ tidalapi not installed. Run: `[p]pipinstall tidalapi`")
            return False
        if not self.session or not self.session.check_login():
            await ctx.send("❌ Not authenticated. Run: `>tidalsetup`")
            return False
        if not self.bot.get_cog("Audio"):
            await ctx.send("❌ Audio cog not loaded. Run: `[p]load audio`")
            return False
        return True

    def _suppress_enqueued(self, ctx):
        """Suppress 'Track Enqueued' messages."""
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
        """Restore original send."""
        if hasattr(ctx, "_orig_send"):
            ctx.send = ctx._orig_send
            delattr(ctx, "_orig_send")

    @commands.command(name="tplay")
    async def tplay(self, ctx, *, query: str):
        """Search and play from Tidal, or queue Spotify/YouTube playlists."""
        if not await self._check_ready(ctx):
            return
        
        await self._set_cancel(ctx.guild.id, False)
        self._suppress_enqueued(ctx)
        
        try:
            if "open.spotify.com" in query or "spotify:" in query:
                if not SPOTIFY_AVAILABLE or not self.sp:
                    return await ctx.send("❌ Spotify not configured. Run: `>tidalplay spotify <id> <secret>`")
                await self._queue_spotify_playlist(ctx, query)
            elif "youtube.com" in query or "youtu.be" in query:
                if not YOUTUBE_API_AVAILABLE or not self.yt:
                    return await ctx.send("❌ YouTube not configured. Run: `>tidalplay youtube <api_key>`")
                await self._queue_youtube_playlist(ctx, query)
            elif "tidal.com" in query:
                await self._handle_tidal_url(ctx, query)
            else:
                await self._search_and_play(ctx, query)
        except Exception as e:
            log.error(f"tplay error: {e}")
            await ctx.send(f"❌ Error: {str(e)}")
        finally:
            self._restore_send(ctx)

    @commands.command(name="tstop")
    async def tstop(self, ctx):
        """Stop playlist queueing."""
        await self._set_cancel(ctx.guild.id, True)
        await ctx.send("⏹️ Stopping playlist queueing...")

    async def _search_and_play(self, ctx, query: str):
        """Search and play first result."""
        async with ctx.typing():
            tracks = await self._search_tidal(query)
        if not tracks:
            return await ctx.send("❌ No tracks found.")
        await self._play(ctx, tracks[0])

    async def _handle_tidal_url(self, ctx, url: str):
        """Queue Tidal playlist, album, or track."""
        kind = ("playlist" if "playlist/" in url else
                "album" if "album/" in url else
                "mix" if "mix/" in url else
                "track")
        
        match = re.search(rf"{kind}/([A-Za-z0-9\-]+)", url)
        if not match:
            return await ctx.send(f"❌ Invalid Tidal {kind} URL")
        
        loader = getattr(self.session, kind, None)
        if not loader:
            return await ctx.send(f"❌ Unsupported content type")
        
        try:
            obj = await self.bot.loop.run_in_executor(None, loader, match.group(1))
        except Exception as e:
            log.error(f"Load {kind} failed: {e}")
            return await ctx.send("❌ Content unavailable (private/region-locked)")
        
        items = await self.bot.loop.run_in_executor(
            None,
            lambda: getattr(obj, "tracks", lambda: getattr(obj, "items", lambda: [])())()
        )
        
        if not items:
            return await ctx.send(f"❌ No tracks in {kind}")
        
        name = getattr(obj, "name", getattr(obj, "title", "Unknown"))
        msg = await ctx.send(f"🎵 Queueing **{name}** ({len(items)} tracks)...")
        
        queued = 0
        for i, t in enumerate(items, 1):
            if await self._should_cancel(ctx.guild.id):
                await msg.edit(content=f"⏹️ Cancelled. Queued {queued}/{len(items)}.")
                await self._set_cancel(ctx.guild.id, False)
                return
            
            if await self._play(ctx, t, show_embed=False):
                queued += 1
            
            if i % BATCH_UPDATE_INTERVAL == 0:
                try:
                    await msg.edit(content=f"⏳ {queued}/{len(items)} queued...")
                except:
                    pass
        
        await msg.edit(content=f"✅ Queued {queued} tracks from **{name}**")

    async def _queue_spotify_playlist(self, ctx, url: str):
        """Queue Spotify playlist via Tidal search."""
        match = re.search(r"playlist/([A-Za-z0-9]+)", url)
        if not match:
            return await ctx.send("❌ Invalid Spotify playlist URL")
        
        msg = await ctx.send("🔍 Fetching Spotify playlist...")
        
        try:
            results = await self.bot.loop.run_in_executor(
                None,
                lambda: self.sp.playlist_items(match.group(1), fields="items.track(name,artists),next", limit=100)
            )
            tracks = []
            while results:
                tracks.extend(results["items"])
                results = await self.bot.loop.run_in_executor(None, self.sp.next, results) if results.get("next") else None
        except Exception as e:
            log.error(f"Spotify error: {e}")
            return await ctx.send("❌ Could not fetch playlist.")
        
        if not tracks:
            return await ctx.send("❌ No tracks in playlist")
        
        await msg.edit(content=f"🎵 Queueing {len(tracks)} tracks from Spotify...")
        
        queued, skipped = 0, 0
        for idx, item in enumerate(tracks, 1):
            if await self._should_cancel(ctx.guild.id):
                await msg.edit(content=f"⏹️ Cancelled. {queued} queued, {skipped} skipped.")
                await self._set_cancel(ctx.guild.id, False)
                return
            
            tr = item.get("track")
            if not tr:
                skipped += 1
                continue
            
            artist = tr["artists"][0]["name"] if tr.get("artists") else ""
            title = tr.get("name", "")
            
            tidal_tracks = await self._search_tidal(f"{artist} {title}")
            if tidal_tracks and await self._play(ctx, tidal_tracks[0], show_embed=False):
                queued += 1
            else:
                skipped += 1
            
            if idx % BATCH_UPDATE_INTERVAL == 0:
                try:
                    await msg.edit(content=f"⏳ {queued} queued, {skipped} skipped ({idx}/{len(tracks)})")
                except:
                    pass
        
        await msg.edit(content=f"✅ Queued {queued}/{len(tracks)} ({skipped} not found on Tidal)")

    async def _queue_youtube_playlist(self, ctx, url: str):
        """Queue YouTube playlist via Tidal search."""
        match = re.search(r"list=([A-Za-z0-9_-]+)", url)
        if not match:
            return await ctx.send("❌ Invalid YouTube playlist URL")
        
        msg = await ctx.send("🔍 Fetching YouTube playlist...")
        
        try:
            videos = []
            req = self.yt.playlistItems().list(part="snippet", playlistId=match.group(1), maxResults=50)
            while req:
                res = await self.bot.loop.run_in_executor(None, req.execute)
                videos += [item["snippet"]["title"] for item in res["items"]]
                req = self.yt.playlistItems().list_next(req, res)
        except Exception as e:
            log.error(f"YouTube error: {e}")
            return await ctx.send("❌ Could not fetch playlist.")
        
        if not videos:
            return await ctx.send("❌ No videos in playlist")
        
        await msg.edit(content=f"🎵 Queueing {len(videos)} videos from YouTube...")
        
        queued, skipped = 0, 0
        for idx, title in enumerate(videos, 1):
            if await self._should_cancel(ctx.guild.id):
                await msg.edit(content=f"⏹️ Cancelled. {queued} queued, {skipped} skipped.")
                await self._set_cancel(ctx.guild.id, False)
                return
            
            tidal_tracks = await self._search_tidal(title)
            if tidal_tracks and await self._play(ctx, tidal_tracks[0], show_embed=False):
                queued += 1
            else:
                skipped += 1
            
            if idx % BATCH_UPDATE_INTERVAL == 0:
                try:
                    await msg.edit(content=f"⏳ {queued} queued, {skipped} skipped ({idx}/{len(videos)})")
                except:
                    pass
        
        await msg.edit(content=f"✅ Queued {queued}/{len(videos)} ({skipped} not found on Tidal)")

    @commands.Cog.listener()
    async def on_red_audio_track_start(self, guild, track, requester):
        """Pop metadata on track start."""
        await self._pop_meta(guild.id)

    @commands.Cog.listener()
    async def on_red_audio_queue_end(self, guild, track_history, req_history):
        """Clear metadata on queue end."""
        await self._clear_meta(guild.id)

    @commands.command(name="tqueue")
    async def tqueue(self, ctx):
        """Display Tidal queue."""
        data = await self.config.guild(ctx.guild).track_metadata()
        if not data:
            return await ctx.send("📭 Queue is empty.")
        
        embeds = []
        for i in range(0, len(data), 10):
            chunk = data[i:i+10]
            desc = "\n".join(
                f"`{i+j+1}.` **{m['title']}** • {m['artist']} • `{self._format_time(m['duration'])}`"
                for j, m in enumerate(chunk)
            )
            embed = discord.Embed(title="🎵 Tidal Queue", description=desc, color=discord.Color.blue())
            embed.set_footer(text=f"Page {i//10 + 1} • Total: {len(data)} tracks")
            embeds.append(embed)
        
        await menu(ctx, embeds, DEFAULT_CONTROLS)

    @commands.command(name="tclear")
    async def tclear(self, ctx):
        """Clear Tidal queue."""
        await self._clear_meta(ctx.guild.id)
        await ctx.send("✅ Queue cleared.")

    @commands.command(name="tidalreset")
    @commands.is_owner()
    async def tidalreset(self, ctx):
        """Clear stored tokens."""
        await self.config.token_type.set(None)
        await self.config.access_token.set(None)
        await self.config.refresh_token.set(None)
        await self.config.expiry_time.set(None)
        self.session = tidalapi.Session() if TIDALAPI_AVAILABLE else None
        await ctx.send("✅ Tokens cleared. Run:\n1. `[p]pipinstall --force-reinstall tidalapi`\n2. Restart bot\n3. `>tidalsetup`")

    @commands.command(name="tidalsetup")
    @commands.is_owner()
    async def tidalsetup(self, ctx):
        """Setup Tidal OAuth (requires tidalapi 0.8.8+)."""
        if not TIDALAPI_AVAILABLE:
            return await ctx.send("❌ Install tidalapi: `[p]pipinstall tidalapi`")
        
        await ctx.send("🔐 Starting OAuth... **Check your console for the login link!**")
        
        def _run_oauth():
            try:
                self.session.login_oauth_simple()
                return True
            except Exception as e:
                log.error(f"OAuth failed: {e}")
                return e
        
        result = await self.bot.loop.run_in_executor(None, _run_oauth)
        
        if isinstance(result, Exception):
            return await ctx.send(f"❌ OAuth failed: {result}")
        
        await asyncio.sleep(3)
        
        if self.session.check_login():
            await self.config.token_type.set(self.session.token_type)
            await self.config.access_token.set(self.session.access_token)
            await self.config.refresh_token.set(self.session.refresh_token)
            if hasattr(self.session, "expiry_time"):
                await self.config.expiry_time.set(self.session.expiry_time.timestamp())
            await ctx.send("✅ Tidal setup complete!")
        else:
            await ctx.send("⏳ Complete login in console, then run `>tidalsetup` again.")

    @commands.group(name="tidalplay", invoke_without_command=True)
    @commands.is_owner()
    async def tidalplay(self, ctx):
        """Config for Spotify and YouTube."""
        await ctx.send_help()

    @tidalplay.command(name="spotify")
    @commands.is_owner()
    async def spotify_setup(self, ctx, client_id: str, client_secret: str):
        """Configure Spotify API."""
        if not SPOTIFY_AVAILABLE:
            return await ctx.send("❌ Install spotipy: `pip install spotipy`")
        
        await self.config.spotify_client_id.set(client_id)
        await self.config.spotify_client_secret.set(client_secret)
        try:
            self.sp = spotipy.Spotify(
                client_credentials_manager=SpotifyClientCredentials(client_id, client_secret)
            )
            await self.bot.loop.run_in_executor(None, lambda: self.sp.search("test", limit=1))
            await ctx.send("✅ Spotify configured.")
        except Exception as e:
            await ctx.send(f"❌ Failed: {e}")

    @tidalplay.command(name="youtube")
    @commands.is_owner()
    async def youtube_setup(self, ctx, api_key: str):
        """Configure YouTube API."""
        if not YOUTUBE_API_AVAILABLE:
            return await ctx.send("❌ Install: `pip install google-api-python-client`")
        
        await self.config.youtube_api_key.set(api_key)
        try:
            self.yt = build("youtube", "v3", developerKey=api_key)
            await ctx.send("✅ YouTube configured.")
        except Exception as e:
            await ctx.send(f"❌ Failed: {e}")


async def setup(bot):
    await bot.add_cog(TidalPlayer(bot))
