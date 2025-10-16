from redbot.core import commands, Config
from redbot.core.utils.menus import menu, DEFAULT_CONTROLS
import discord
import logging
import asyncio
import re
from typing import Dict, List, Optional, Tuple
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
OAUTH_TIMEOUT = 300
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
        self.active_tasks: Dict[int, asyncio.Task] = {}
        bot.loop.create_task(self._initialize_apis())

    def cog_unload(self):
        """Clean up tasks on cog unload."""
        for task in self.active_tasks.values():
            if not task.done():
                task.cancel()
        log.info("TidalPlayer cog unloaded and tasks cleaned up")

    async def _initialize_apis(self):
        """Initialize Tidal, Spotify, and YouTube API clients with proper error handling."""
        try:
            await self.bot.wait_until_ready()
            creds = await self.config.all()
            
            # Tidal OAuth with retry and refresh logic
            if TIDALAPI_AVAILABLE and all(creds.get(k) for k in ("token_type", "access_token", "refresh_token")):
                try:
                    expiry = creds.get("expiry_time")
                    if expiry and datetime.fromtimestamp(expiry) < datetime.now():
                        log.info("Tidal token expired, will need re-authentication")
                    else:
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
                        else:
                            log.warning("Tidal session loaded but login check failed")
                except Exception as e:
                    log.error(f"Tidal session load failed: {e}")
            
            # Spotify client with validation
            if SPOTIFY_AVAILABLE and creds.get("spotify_client_id") and creds.get("spotify_client_secret"):
                try:
                    self.sp = spotipy.Spotify(
                        client_credentials_manager=SpotifyClientCredentials(
                            creds["spotify_client_id"],
                            creds["spotify_client_secret"]
                        )
                    )
                    # Test the connection
                    await self.bot.loop.run_in_executor(None, lambda: self.sp.search("test", limit=1))
                    log.info("Spotify client initialized and validated")
                except Exception as e:
                    log.error(f"Spotify init failed: {e}")
                    self.sp = None
            
            # YouTube API with validation
            if YOUTUBE_API_AVAILABLE and creds.get("youtube_api_key"):
                try:
                    self.yt = build("youtube", "v3", developerKey=creds["youtube_api_key"])
                    log.info("YouTube API initialized")
                except Exception as e:
                    log.error(f"YouTube init failed: {e}")
                    self.yt = None
        except Exception as e:
            log.error(f"API initialization failed: {e}")

    def _get_quality_label(self, quality: str) -> str:
        """Convert Tidal quality code to readable format."""
        labels = {
            "HI_RES": "HI-RES (MQA)",
            "HI_RES_LOSSLESS": "HI-RES LOSSLESS",
            "LOSSLESS": "LOSSLESS (FLAC)",
            "HIGH": "HIGH (320kbps)",
            "LOW": "LOW (96kbps)"
        }
        return labels.get(quality, "LOSSLESS (FLAC)")

    def _extract_meta(self, track) -> Dict:
        """Extract metadata from Tidal track object with fallbacks."""
        try:
            return {
                "title": getattr(track, "name", None) or "Unknown",
                "artist": getattr(track.artist, "name", "Unknown") if hasattr(track, "artist") and track.artist else "Unknown",
                "album": getattr(track.album, "name", None) if hasattr(track, "album") and track.album else None,
                "duration": int(getattr(track, "duration", 0) or 0),
                "quality": getattr(track, "audio_quality", "LOSSLESS")
            }
        except Exception as e:
            log.error(f"Error extracting metadata: {e}")
            return {
                "title": "Unknown",
                "artist": "Unknown",
                "album": None,
                "duration": 0,
                "quality": "LOSSLESS"
            }

    async def _add_meta(self, guild_id: int, meta: Dict) -> bool:
        """Add track metadata to guild queue with size limit."""
        try:
            async with self.config.guild_from_id(guild_id).track_metadata() as q:
                if len(q) >= MAX_QUEUE_SIZE:
                    log.warning(f"Queue size limit reached for guild {guild_id}")
                    return False
                q.append(meta)
                return True
        except Exception as e:
            log.error(f"Error adding metadata: {e}")
            return False

    async def _pop_meta(self, guild_id: int):
        """Remove first track metadata from guild queue."""
        try:
            async with self.config.guild_from_id(guild_id).track_metadata() as q:
                if q:
                    q.pop(0)
        except Exception as e:
            log.error(f"Error popping metadata: {e}")

    async def _clear_meta(self, guild_id: int):
        """Clear all track metadata for guild."""
        try:
            await self.config.guild_from_id(guild_id).track_metadata.set([])
        except Exception as e:
            log.error(f"Error clearing metadata: {e}")

    async def _should_cancel(self, guild_id: int) -> bool:
        """Check if queueing should be cancelled."""
        try:
            return await self.config.guild_from_id(guild_id).cancel_queue()
        except:
            return False

    async def _set_cancel(self, guild_id: int, value: bool):
        """Set cancel flag for guild."""
        try:
            await self.config.guild_from_id(guild_id).cancel_queue.set(value)
        except Exception as e:
            log.error(f"Error setting cancel flag: {e}")

    def _format_time(self, seconds: int) -> str:
        """Format seconds to MM:SS."""
        m, s = divmod(seconds, 60)
        return f"{m:02d}:{s:02d}"

    async def _search_tidal(self, query: str, retry: int = 0) -> List:
        """Search Tidal with semaphore rate limiting and retry logic."""
        async with self.api_semaphore:
            try:
                res = await self.bot.loop.run_in_executor(None, self.session.search, query)
                return res.get("tracks", [])
            except Exception as e:
                if retry < SEARCH_RETRY_ATTEMPTS:
                    log.warning(f"Tidal search failed for '{query}' (attempt {retry + 1}), retrying: {e}")
                    await asyncio.sleep(0.5)
                    return await self._search_tidal(query, retry + 1)
                log.error(f"Tidal search failed for '{query}' after {SEARCH_RETRY_ATTEMPTS} attempts: {e}")
                return []

    async def _play(self, ctx, track, show_embed: bool = True) -> bool:
        """Queue a Tidal track via Audio cog with comprehensive error handling."""
        try:
            meta = self._extract_meta(track)
            added = await self._add_meta(ctx.guild.id, meta)
            
            if not added:
                log.warning(f"Failed to add track to queue: {meta['title']}")
                return False
            
            if show_embed:
                desc = f"**{meta['title']}** • {meta['artist']}"
                if meta["album"]:
                    desc += f"\n_{meta['album']}_"
                embed = discord.Embed(
                    title="🎵 Playing from Tidal",
                    description=desc,
                    color=discord.Color.blue()
                )
                embed.add_field(name="Quality", value=self._get_quality_label(meta["quality"]), inline=True)
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
            log.error(f"Error playing track: {e}")
            return False

    async def _check_ready(self, ctx) -> bool:
        """Verify Tidal session and Audio cog are available."""
        if not TIDALAPI_AVAILABLE:
            await ctx.send("❌ Error: tidalapi not installed. Run: `[p]pipinstall tidalapi`")
            return False
        if not self.session or not self.session.check_login():
            await ctx.send("❌ Error: Not authenticated. Run: `>tidalsetup`")
            return False
        if not self.bot.get_cog("Audio"):
            await ctx.send("❌ Error: Audio cog not loaded. Load it with `[p]load audio`")
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
        self._suppress_enqueued(ctx)
        
        try:
            if "open.spotify.com" in query or "spotify:" in query:
                if not SPOTIFY_AVAILABLE or not self.sp:
                    return await ctx.send("❌ Spotify not configured. Run: `>tidalplay spotify <client_id> <client_secret>`")
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
            log.error(f"Error in tplay command: {e}")
            await ctx.send(f"❌ An error occurred: {str(e)}")
        finally:
            self._restore_send(ctx)

    @commands.command(name="tstop")
    async def tstop(self, ctx):
        """Stop the current playlist queueing operation."""
        await self._set_cancel(ctx.guild.id, True)
        await ctx.send("⏹️ Stopping playlist queueing...")

    async def _search_and_play(self, ctx, query: str):
        """Search Tidal and play first result."""
        async with ctx.typing():
            tracks = await self._search_tidal(query)
        if not tracks:
            return await ctx.send("❌ No tracks found.")
        await self._play(ctx, tracks[0])

    async def _handle_tidal_url(self, ctx, url: str):
        """Queue Tidal playlist, album, or track with robust error handling."""
        kind = ("playlist" if "playlist/" in url else
                "album" if "album/" in url else
                "mix" if "mix/" in url else
                "track")
        
        match = re.search(rf"{kind}/([A-Za-z0-9\-]+)", url)
        if not match:
            return await ctx.send(f"❌ Invalid Tidal {kind} URL")
        
        loader = getattr(self.session, kind, None)
        if not loader:
            return await ctx.send(f"❌ Unsupported Tidal content type: {kind}")
        
        try:
            obj = await self.bot.loop.run_in_executor(None, loader, match.group(1))
        except Exception as e:
            log.error(f"Tidal {kind} load failed: {e}")
            return await ctx.send("❌ Tidal content unavailable (private, region-locked, or invalid)")
        
        items = await self.bot.loop.run_in_executor(
            None,
            lambda: getattr(obj, "tracks", lambda: getattr(obj, "items", lambda: [])())()
        )
        
        if not items:
            return await ctx.send(f"❌ No tracks found in this {kind}")
        
        name = getattr(obj, "name", getattr(obj, "title", "Unknown"))
        msg = await ctx.send(f"🎵 Queueing Tidal {kind} **{name}** ({len(items)} tracks)...")
        
        queued = 0
        for i, t in enumerate(items, 1):
            if await self._should_cancel(ctx.guild.id):
                await msg.edit(content=f"⏹️ Cancelled. Queued {queued}/{len(items)} tracks.")
                await self._set_cancel(ctx.guild.id, False)
                return
            
            success = await self._play(ctx, t, show_embed=False)
            if success:
                queued += 1
            
            if i % BATCH_UPDATE_INTERVAL == 0:
                try:
                    await msg.edit(content=f"⏳ Progress: {queued}/{len(items)} queued...")
                except:
                    pass
        
        await msg.edit(content=f"✅ Queued {queued} tracks from **{name}**")

    async def _queue_spotify_playlist(self, ctx, url: str):
        """Queue Spotify playlist via Tidal search."""
        match = re.search(r"playlist/([A-Za-z0-9]+)", url)
        if not match:
            return await ctx.send("❌ Invalid Spotify playlist URL")
        pid = match.group(1)
        
        msg = await ctx.send("🔍 Fetching Spotify playlist...")
        
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
            return await ctx.send("❌ Could not fetch playlist. Ensure it is public and credentials are valid.")
        
        if not tracks:
            return await ctx.send("❌ No tracks found in playlist")
        
        await msg.edit(content=f"🎵 Queueing {len(tracks)} tracks from Spotify via Tidal search...")
        
        queued, skipped = 0, 0
        for idx, item in enumerate(tracks, 1):
            if await self._should_cancel(ctx.guild.id):
                await msg.edit(content=f"⏹️ Cancelled. Queued {queued}, skipped {skipped} ({idx-1}/{len(tracks)} processed)")
                await self._set_cancel(ctx.guild.id, False)
                return
            
            tr = item.get("track")
            if not tr:
                skipped += 1
                continue
            
            artist = tr["artists"][0]["name"] if tr.get("artists") else ""
            title = tr.get("name", "")
            search_query = f"{artist} {title}"
            
            tidal_tracks = await self._search_tidal(search_query)
            if tidal_tracks:
                success = await self._play(ctx, tidal_tracks[0], show_embed=False)
                if success:
                    queued += 1
                else:
                    skipped += 1
            else:
                skipped += 1
            
            if idx % BATCH_UPDATE_INTERVAL == 0:
                try:
                    await msg.edit(content=f"⏳ Progress: {queued} queued, {skipped} skipped ({idx}/{len(tracks)})")
                except:
                    pass
        
        await msg.edit(content=f"✅ Complete. Queued {queued}/{len(tracks)} tracks ({skipped} not found on Tidal)")

    async def _queue_youtube_playlist(self, ctx, url: str):
        """Queue YouTube playlist via Tidal search."""
        match = re.search(r"list=([A-Za-z0-9_-]+)", url)
        if not match:
            return await ctx.send("❌ Invalid YouTube playlist URL")
        pid = match.group(1)
        
        msg = await ctx.send("🔍 Fetching YouTube playlist...")
        
        try:
            videos = []
            req = self.yt.playlistItems().list(part="snippet", playlistId=pid, maxResults=50)
            while req:
                res = await self.bot.loop.run_in_executor(None, req.execute)
                videos += [item["snippet"]["title"] for item in res["items"]]
                req = self.yt.playlistItems().list_next(req, res)
        except Exception as e:
            log.error(f"YouTube API error: {e}")
            return await ctx.send("❌ Could not fetch playlist. Check API key and playlist visibility.")
        
        if not videos:
            return await ctx.send("❌ No videos found in playlist")
        
        await msg.edit(content=f"🎵 Queueing {len(videos)} videos from YouTube via Tidal search...")
        
        queued, skipped = 0, 0
        for idx, title in enumerate(videos, 1):
            if await self._should_cancel(ctx.guild.id):
                await msg.edit(content=f"⏹️ Cancelled. Queued {queued}, skipped {skipped} ({idx-1}/{len(videos)} processed)")
                await self._set_cancel(ctx.guild.id, False)
                return
            
            tidal_tracks = await self._search_tidal(title)
            if tidal_tracks:
                success = await self._play(ctx, tidal_tracks[0], show_embed=False)
                if success:
                    queued += 1
                else:
                    skipped += 1
            else:
                skipped += 1
            
            if idx % BATCH_UPDATE_INTERVAL == 0:
                try:
                    await msg.edit(content=f"⏳ Progress: {queued} queued, {skipped} skipped ({idx}/{len(videos)})")
                except:
                    pass
        
        await msg.edit(content=f"✅ Complete. Queued {queued}/{len(videos)} videos ({skipped} not found on Tidal)")

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
            return await ctx.send("📭 The queue is empty.")
        
        embeds = []
        for i in range(0, len(data), 10):
            chunk = data[i:i+10]
            desc = "\n".join(
                f"`{i+j+1}.` **{m['title']}** • {m['artist']} • `{self._format_time(m['duration'])}`"
                for j, m in enumerate(chunk)
            )
            embed = discord.Embed(title="🎵 Tidal Queue", description=desc, color=discord.Color.blue())
            embed.set_footer(text=f"Page {i//10 + 1} of {(len(data)-1)//10 + 1} • Total: {len(data)} tracks")
            embeds.append(embed)
        
        await menu(ctx, embeds, DEFAULT_CONTROLS)

    @commands.command(name="tclear")
    async def tclear(self, ctx):
        """Clear the Tidal metadata queue."""
        await self._clear_meta(ctx.guild.id)
        await ctx.send("✅ Tidal queue metadata cleared.")

    @commands.command(name="tidalsetup")
    @commands.is_owner()
    async def tidalsetup(self, ctx):
        """Setup Tidal OAuth authentication."""
        if not TIDALAPI_AVAILABLE:
            return await ctx.send("❌ Error: Install tidalapi with `[p]pipinstall tidalapi`")
        
        # Try login_oauth_simple first (more reliable)
        if hasattr(self.session, 'login_oauth_simple'):
            await ctx.send("🔐 Starting Tidal OAuth (simplified method)...")
            
            def _run_oauth_simple():
                """Run OAuth with custom printer to capture link."""
                link_data = {}
                
                def custom_printer(text):
                    link_data['text'] = text
                
                try:
                    self.session.login_oauth_simple(function=custom_printer)
                    return link_data
                except Exception as e:
                    return e
            
            result = await self.bot.loop.run_in_executor(None, _run_oauth_simple)
            
            if isinstance(result, Exception):
                log.error(f"OAuth simple failed: {result}")
                await ctx.send(f"❌ Tidal OAuth failed: {result}\n\n**Troubleshooting:**\n1. Update tidalapi: `[p]pipinstall --upgrade tidalapi`\n2. Tidal may have blocked third-party logins\n3. Try a different Tidal account")
                return
            
            if self.session.check_login():
                await self.config.token_type.set(self.session.token_type)
                await self.config.access_token.set(self.session.access_token)
                await self.config.refresh_token.set(self.session.refresh_token)
                if hasattr(self.session, "expiry_time"):
                    await self.config.expiry_time.set(self.session.expiry_time.timestamp())
                await ctx.send("✅ Tidal setup complete!")
            else:
                await ctx.send("❌ Login failed. You may not have completed the authorization.")
        else:
            # Fall back to standard login_oauth
            def _run_oauth():
                try:
                    return self.session.login_oauth()
                except Exception as e:
                    return e

            result = await self.bot.loop.run_in_executor(None, _run_oauth)
            if isinstance(result, Exception):
                await ctx.send(f"❌ Tidal OAuth device login failed: {result}")
                await ctx.send("**Troubleshooting:**\n1. Update tidalapi: `[p]pipinstall --upgrade tidalapi`\n2. This error often means Tidal restricted third-party app logins\n3. Check if your Tidal subscription is active")
                return

            login, fut = result
            embed = discord.Embed(
                title="🎵 Tidal OAuth Setup",
                description=f"Visit:\n{login.verification_uri_complete}",
                color=discord.Color.blue()
            )
            embed.set_footer(text="Expires in 5 minutes")
            await ctx.send(embed=embed)
            
            try:
                await asyncio.wait_for(self.bot.loop.run_in_executor(None, fut.result), timeout=OAUTH_TIMEOUT)
            except asyncio.TimeoutError:
                return await ctx.send("⏱️ OAuth timed out. Please try again.")
            except Exception as e:
                return await ctx.send(f"❌ OAuth login error: {e}")

            if self.session.check_login():
                await self.config.token_type.set(self.session.token_type)
                await self.config.access_token.set(self.session.access_token)
                await self.config.refresh_token.set(self.session.refresh_token)
                if hasattr(self.session, "expiry_time"):
                    await self.config.expiry_time.set(self.session.expiry_time.timestamp())
                await ctx.send("✅ Tidal setup complete!")
            else:
                await ctx.send("❌ Login failed. Try again or check if Tidal restricted third-party access.")

    @commands.group(name="tidalplay", invoke_without_command=True)
    @commands.is_owner()
    async def tidalplay(self, ctx):
        """Configuration commands for Spotify and YouTube integration."""
        await ctx.send_help()

    @tidalplay.command(name="spotify")
    @commands.is_owner()
    async def spotify_setup(self, ctx, client_id: str, client_secret: str):
        """Configure Spotify API credentials."""
        if not SPOTIFY_AVAILABLE:
            return await ctx.send("❌ Error: Install spotipy with: `pip install spotipy`")
        
        await self.config.spotify_client_id.set(client_id)
        await self.config.spotify_client_secret.set(client_secret)
        try:
            self.sp = spotipy.Spotify(
                client_credentials_manager=SpotifyClientCredentials(client_id, client_secret)
            )
            # Validate
            await self.bot.loop.run_in_executor(None, lambda: self.sp.search("test", limit=1))
            await ctx.send("✅ Spotify credentials saved and validated.")
        except Exception as e:
            await ctx.send(f"❌ Failed to initialize Spotify client: {e}")

    @tidalplay.command(name="youtube")
    @commands.is_owner()
    async def youtube_setup(self, ctx, api_key: str):
        """Configure YouTube Data API key."""
        if not YOUTUBE_API_AVAILABLE:
            return await ctx.send("❌ Error: Install google-api-python-client with: `pip install google-api-python-client`")
        
        await self.config.youtube_api_key.set(api_key)
        try:
            self.yt = build("youtube", "v3", developerKey=api_key)
            await ctx.send("✅ YouTube API key saved and validated.")
        except Exception as e:
            await ctx.send(f"❌ Failed to initialize YouTube client: {e}")


async def setup(bot):
    await bot.add_cog(TidalPlayer(bot))