from redbot.core import commands, Config
import discord
import logging
import asyncio
import re

try:
    import tidalapi
    TIDALAPI_AVAILABLE = True
except ImportError:
    TIDALAPI_AVAILABLE = False

log = logging.getLogger("red.tidalplayer")

class TidalPlayer(commands.Cog):
    """Play music from Tidal in LOSSLESS quality"""
    
    def __init__(self, bot):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=1234567890, force_registration=True)
        default_global = {
            "token_type": None,
            "access_token": None,
            "refresh_token": None,
            "expiry_time": None,
            "quiet_mode": True
        }
        self.config.register_global(**default_global)
        self.session = tidalapi.Session() if TIDALAPI_AVAILABLE else None
        
        if TIDALAPI_AVAILABLE:
            bot.loop.create_task(self._load_session())
    
    async def _load_session(self):
        """Load saved Tidal session"""
        await self.bot.wait_until_ready()
        try:
            creds = await self.config.all()
            if all(creds.get(f) for f in ("token_type", "access_token", "refresh_token")):
                self.session.load_oauth_session(
                    token_type=creds["token_type"],
                    access_token=creds["access_token"],
                    refresh_token=creds["refresh_token"],
                    expiry_time=creds.get("expiry_time")
                )
                log.info("Tidal session loaded" if self.session.check_login() else "Tidal session expired")
        except Exception as e:
            log.error(f"Session load failed: {e}")
    
    def _patch_ctx_send(self, ctx):
        """Suppress 'Track Enqueued' messages"""
        if not hasattr(ctx, "_original_send"):
            ctx._original_send = ctx.send
            async def send_override(*args, **kwargs):
                embed = kwargs.get("embed") or (args[0] if args and isinstance(args[0], discord.Embed) else None)
                if embed and hasattr(embed, "title") and embed.title:
                    if "Track Enqueued" in embed.title or "Tracks Enqueued" in embed.title:
                        return
                return await ctx._original_send(*args, **kwargs)
            ctx.send = send_override
    
    def _restore_ctx_send(self, ctx):
        """Restore ctx.send"""
        if hasattr(ctx, "_original_send"):
            ctx.send = ctx._original_send
            delattr(ctx, "_original_send")
    
    async def _check_ready(self, ctx):
        """Check if ready"""
        if not TIDALAPI_AVAILABLE:
            await ctx.send("❌ Install: `[p]pipinstall tidalapi`")
            return False
        if not self.session or not self.session.check_login():
            await ctx.send("❌ Not authenticated. Run `>tidalsetup`")
            return False
        if not self.bot.get_cog("Audio"):
            await ctx.send("❌ Audio cog not loaded")
            return False
        return True
    
    def _get_quality(self, track) -> str:
        """Get quality label"""
        if hasattr(track, 'audio_quality'):
            return {
                'HI_RES': "HI_RES (MQA)",
                'LOSSLESS': "LOSSLESS (FLAC)",
                'HIGH': "HIGH (320kbps)",
                'LOW': "LOW (96kbps)"
            }.get(track.audio_quality, "LOSSLESS")
        return "LOSSLESS"
    
    def _create_embed(self, title_text, description, quality=None, color=discord.Color.blue()) -> discord.Embed:
        """Create embed"""
        if quality:
            if "HI_RES" in quality or "MQA" in quality:
                color = discord.Color.gold()
            elif "LOSSLESS" in quality:
                color = discord.Color.blue()
        
        embed = discord.Embed(title=title_text, description=description, color=color)
        if quality:
            embed.add_field(name="Quality", value=quality, inline=True)
        return embed
    
    async def _play_track(self, ctx, track):
        """Play single track"""
        try:
            quality = self._get_quality(track)
            stream_url = await self.bot.loop.run_in_executor(None, track.get_url)
            
            if not stream_url:
                return False
            
            audio = self.bot.get_cog("Audio")
            await audio.command_play(ctx, query=stream_url)
            return True
        except Exception as e:
            log.error(f"Play track error: {e}")
            return False
    
    @commands.command(name="tplay")
    async def tidal_play(self, ctx, *, query: str):
        """
        Play from Tidal - supports search, playlists, albums, tracks, mixes
        
        Examples:
        >tplay Lovejoy Baptism
        >tplay https://tidal.com/browse/playlist/xxxxx
        >tplay https://tidal.com/browse/album/xxxxx
        >tplay https://tidal.com/browse/track/xxxxx
        >tplay https://tidal.com/browse/mix/xxxxx
        """
        if not await self._check_ready(ctx):
            return
        
        quiet = await self.config.quiet_mode()
        if quiet:
            self._patch_ctx_send(ctx)
        
        try:
            # Check if it's a URL
            if "tidal.com" in query or "tidal.link" in query:
                await self._handle_url(ctx, query)
            else:
                # Search query
                await self._handle_search(ctx, query)
        finally:
            if quiet:
                self._restore_ctx_send(ctx)
    
    async def _handle_search(self, ctx, query):
        """Handle search query"""
        try:
            async with ctx.typing():
                results = await self.bot.loop.run_in_executor(None, self.session.search, query)
            
            if not results or not results.get('tracks'):
                await ctx.send("❌ No tracks found")
                return
            
            track = results['tracks'][0]
            quality = self._get_quality(track)
            
            title = getattr(track, 'name', "Unknown")
            artist = getattr(track.artist, 'name', "Unknown") if hasattr(track, 'artist') and track.artist else "Unknown"
            album = getattr(track.album, 'name', "") if hasattr(track, 'album') and track.album else ""
            
            desc = f"**{title}**\nby {artist}"
            if album:
                desc += f"\n*{album}*"
            
            await ctx.send(embed=self._create_embed("💎 Playing from Tidal", desc, quality))
            await self._play_track(ctx, track)
            
        except Exception as e:
            await ctx.send(f"❌ Error: {e}")
            log.error(f"Search error: {e}")
    
    async def _handle_url(self, ctx, url):
        """Handle Tidal URL"""
        if "mix/" in url:
            await self._queue_mix(ctx, url)
        elif "playlist/" in url:
            await self._queue_playlist(ctx, url)
        elif "album/" in url:
            await self._queue_album(ctx, url)
        elif "track/" in url:
            await self._queue_single_track(ctx, url)
        else:
            await ctx.send("❌ Invalid Tidal URL")
    
    async def _queue_playlist(self, ctx, url):
        """Queue entire playlist"""
        match = re.search(r"playlist/([A-Za-z0-9\-]+)", url)
        if not match:
            await ctx.send("❌ Invalid playlist URL")
            return
        
        try:
            loading = await ctx.send("⏳ Loading playlist...")
            playlist = await self.bot.loop.run_in_executor(None, self.session.playlist, match.group(1))
            tracks = await self.bot.loop.run_in_executor(None, playlist.tracks)
            
            total = len(tracks)
            await loading.edit(content=f"⏳ Queueing **{playlist.name}** ({total} tracks)...")
            
            queued = 0
            for track in tracks:
                if await self._play_track(ctx, track):
                    queued += 1
            
            await loading.edit(content=f"✅ Queued **{queued}/{total}** tracks from **{playlist.name}**")
        except Exception as e:
            await ctx.send(f"❌ Error: {e}")
            log.error(f"Playlist error: {e}")
    
    async def _queue_album(self, ctx, url):
        """Queue entire album"""
        match = re.search(r"album/([0-9]+)", url)
        if not match:
            await ctx.send("❌ Invalid album URL")
            return
        
        try:
            loading = await ctx.send("⏳ Loading album...")
            album = await self.bot.loop.run_in_executor(None, self.session.album, match.group(1))
            tracks = await self.bot.loop.run_in_executor(None, album.tracks)
            
            total = len(tracks)
            await loading.edit(content=f"⏳ Queueing **{album.name}** by {album.artist.name} ({total} tracks)...")
            
            queued = 0
            for track in tracks:
                if await self._play_track(ctx, track):
                    queued += 1
            
            await loading.edit(content=f"✅ Queued **{queued}/{total}** tracks from **{album.name}**")
        except Exception as e:
            await ctx.send(f"❌ Error: {e}")
            log.error(f"Album error: {e}")
    
    async def _queue_single_track(self, ctx, url):
        """Queue single track from URL"""
        match = re.search(r"track/([0-9]+)", url)
        if not match:
            await ctx.send("❌ Invalid track URL")
            return
        
        try:
            track = await self.bot.loop.run_in_executor(None, self.session.track, match.group(1))
            quality = self._get_quality(track)
            
            title = getattr(track, 'name', "Unknown")
            artist = getattr(track.artist, 'name', "Unknown") if hasattr(track, 'artist') and track.artist else "Unknown"
            
            await ctx.send(embed=self._create_embed("💎 Playing from Tidal", f"**{title}**\nby {artist}", quality))
            await self._play_track(ctx, track)
        except Exception as e:
            await ctx.send(f"❌ Error: {e}")
            log.error(f"Track error: {e}")
    
    async def _queue_mix(self, ctx, url):
        """Queue Tidal mix"""
        match = re.search(r"mix/([A-Za-z0-9]+)", url)
        if not match:
            await ctx.send("❌ Invalid mix URL")
            return
        
        try:
            loading = await ctx.send("⏳ Loading mix...")
            mix = await self.bot.loop.run_in_executor(None, self.session.mix, match.group(1))
            items = await self.bot.loop.run_in_executor(None, mix.items)
            
            total = len(items)
            await loading.edit(content=f"⏳ Queueing **{mix.title}** ({total} tracks)...")
            
            queued = 0
            for item in items:
                if await self._play_track(ctx, item):
                    queued += 1
            
            await loading.edit(content=f"✅ Queued **{queued}/{total}** tracks from **{mix.title}**")
        except Exception as e:
            await ctx.send(f"❌ Error: {e}")
            log.error(f"Mix error: {e}")
    
    @commands.is_owner()
    @commands.command()
    async def tidalsetup(self, ctx):
        """Setup Tidal OAuth"""
        if not TIDALAPI_AVAILABLE:
            return await ctx.send("❌ Install: `[p]pipinstall tidalapi`")
        
        await ctx.send("Starting OAuth...")
        try:
            login, future = self.session.login_oauth()
            embed = discord.Embed(
                title="Tidal OAuth",
                description=f"Visit:\n{login.verification_uri_complete}",
                color=discord.Color.blue()
            )
            embed.set_footer(text="5 minute timeout")
            await ctx.send(embed=embed)
            
            await asyncio.wait_for(
                self.bot.loop.run_in_executor(None, future.result),
                timeout=300
            )
            
            if self.session.check_login():
                await self.config.token_type.set(self.session.token_type)
                await self.config.access_token.set(self.session.access_token)
                await self.config.refresh_token.set(self.session.refresh_token)
                if hasattr(self.session, "expiry_time") and self.session.expiry_time:
                    await self.config.expiry_time.set(self.session.expiry_time.timestamp())
                await ctx.send("✅ Setup complete! Use `>tplay <song/url>`")
            else:
                await ctx.send("❌ Login failed")
        except asyncio.TimeoutError:
            await ctx.send("⏱️ Timeout")
        except Exception as e:
            await ctx.send(f"❌ Error: {e}")
    
    @commands.is_owner()
    @commands.command()
    async def tidalquiet(self, ctx, mode: str = None):
        """Toggle quiet mode (suppresses 'Track Enqueued')"""
        if mode is None:
            status = "enabled" if await self.config.quiet_mode() else "disabled"
            await ctx.send(f"Quiet mode: **{status}**\nUsage: `>tidalquiet on/off`")
            return
        
        if mode.lower() not in ("on", "off"):
            await ctx.send("Usage: `>tidalquiet on/off`")
            return
        
        await self.config.quiet_mode.set(mode.lower() == "on")
        await ctx.send(f"✅ Quiet mode **{mode.lower()}**")
    
    def cog_unload(self):
        log.info("TidalPlayer unloaded")


async def setup(bot):
    await bot.add_cog(TidalPlayer(bot))
