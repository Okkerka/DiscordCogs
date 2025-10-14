from redbot.core import commands, Config
import discord
import logging
import asyncio
from typing import Optional, Dict
import os
from pathlib import Path
import tempfile

try:
    import tidalapi
    TIDALAPI_AVAILABLE = True
except ImportError:
    TIDALAPI_AVAILABLE = False

log = logging.getLogger("red.tidalplayer")

class TidalPlayer(commands.Cog):
    """Play music from Tidal in highest quality available"""
    
    def __init__(self, bot):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=1234567890)
        default_global = {
            "token_type": None,
            "access_token": None,
            "refresh_token": None,
            "expiry_time": None,
            "country_code": "US",
            "fallback_to_youtube": True,
            "download_mode": False,
            "debug_mode": False
        }
        self.config.register_global(**default_global)
        self.session = tidalapi.Session() if TIDALAPI_AVAILABLE else None
        self.last_file = None
        
        if TIDALAPI_AVAILABLE:
            bot.loop.create_task(self._load_session())
    
    async def _debug(self, message: str):
        """Log debug messages if debug mode is enabled"""
        if await self.config.debug_mode():
            log.info(f"[DEBUG] {message}")
    
    async def _load_session(self):
        """Load saved Tidal session"""
        try:
            await self._debug("Loading Tidal session...")
            token_type = await self.config.token_type()
            access_token = await self.config.access_token()
            refresh_token = await self.config.refresh_token()
            
            if all([token_type, access_token, refresh_token]):
                self.session.load_oauth_session(token_type, access_token, refresh_token)
                log.info("Tidal session loaded")
                await self._debug(f"Session loaded: token_type={token_type}")
            else:
                await self._debug("No saved session found")
        except Exception as e:
            log.error(f"Session load failed: {e}")
            await self._debug(f"Session load exception: {type(e).__name__}: {e}")
    
    def _cleanup_file(self, file_path: str):
        """Delete a file if it exists"""
        if file_path and os.path.exists(file_path):
            try:
                os.remove(file_path)
                log.info(f"Deleted: {file_path}")
            except Exception as e:
                log.error(f"Delete failed: {e}")
    
    def _get_quality(self, track) -> str:
        """Get quality label for track"""
        try:
            if hasattr(track, 'audio_quality'):
                quality_labels = {
                    'HI_RES': "HI_RES (MQA)",
                    'LOSSLESS': "LOSSLESS (FLAC)",
                    'HIGH': "HIGH (320kbps)",
                    'LOW': "LOW (96kbps)"
                }
                return quality_labels.get(track.audio_quality, "LOSSLESS (FLAC)")
        except Exception as e:
            log.warning(f"Quality detection error: {e}")
        
        return "LOSSLESS (FLAC)"
    
    def _extract_metadata(self, track) -> Dict:
        """Extract track metadata"""
        return {
            "title": getattr(track, 'name', "Unknown"),
            "artist": getattr(track.artist, 'name', "Unknown") if hasattr(track, 'artist') and track.artist else "Unknown",
            "album": getattr(track.album, 'name', "Unknown") if hasattr(track, 'album') and track.album else "Unknown",
            "track_id": getattr(track, 'id', None)
        }
    
    @commands.command(name="tplay")
    async def tidal_play(self, ctx, *, query: str):
        """Play a song from Tidal"""
        await self._debug(f"Command received: query='{query}', user={ctx.author}, guild={ctx.guild.name if ctx.guild else 'DM'}")
        
        if not TIDALAPI_AVAILABLE:
            await self._debug("tidalapi not available")
            return await ctx.send("❌ Install tidalapi: `[p]pipinstall tidalapi`")
        
        if not self.session or not self.session.check_login():
            await self._debug("Session not authenticated")
            return await ctx.send("❌ Not authenticated. Run `>tidalplay setup`")
        
        if not self.bot.get_cog("Audio"):
            await self._debug("Audio cog not loaded")
            return await ctx.send("❌ Audio cog not loaded")
        
        # Clean up previous file
        if self.last_file:
            await self._debug(f"Cleaning up previous file: {self.last_file}")
            self._cleanup_file(self.last_file)
            self.last_file = None
        
        # Get track
        await self._debug("Starting track search...")
        async with ctx.typing():
            result = await self._get_track(query, ctx)
        
        if not result:
            await self._debug("No result returned from track search")
            return await ctx.send("❌ Track not found")
        
        await self._debug(f"Track result: url={result['url'][:50]}..., is_fallback={result.get('is_fallback')}")
        
        # Send embed only if playing from Tidal
        if not result.get("is_fallback"):
            await self._debug("Sending Tidal embed")
            await ctx.send(embed=self._create_embed(result["info"]))
        else:
            await self._debug("Using YouTube fallback, no embed")
        
        # Save file for cleanup
        if result.get("file_path"):
            self.last_file = result["file_path"]
            await self._debug(f"Saved file path for cleanup: {self.last_file}")
        
        # Play via Audio cog
        try:
            await self._debug("Invoking Audio cog play command")
            audio = self.bot.get_cog("Audio")
            await audio.command_play(ctx, query=result["url"])
            await self._debug("Audio cog play command completed")
        except Exception as e:
            log.error(f"Play failed: {e}")
            await self._debug(f"Play exception: {type(e).__name__}: {e}")
            await ctx.send(f"❌ Playback failed")
    
    async def _get_track(self, query: str, ctx) -> Optional[Dict]:
        """Search and get track from Tidal"""
        try:
            await self._debug(f"Searching Tidal for: {query}")
            
            # Search
            results = await self.bot.loop.run_in_executor(None, self.session.search, query)
            await self._debug(f"Search completed, results type: {type(results)}")
            
            if not results or not results.get('tracks'):
                await self._debug("No tracks in search results")
                return await self._fallback(query)
            
            await self._debug(f"Found {len(results['tracks'])} tracks")
            track = results['tracks'][0]
            
            metadata = self._extract_metadata(track)
            await self._debug(f"Metadata: {metadata}")
            
            quality_str = self._get_quality(track)
            await self._debug(f"Quality: {quality_str}")
            
            # Try download mode first
            download_mode = await self.config.download_mode()
            await self._debug(f"Download mode: {download_mode}")
            
            if download_mode:
                await self._debug("Attempting download...")
                result = await self._download(track, ctx, metadata)
                if result:
                    await self._debug(f"Download successful: {result}")
                    return {
                        "url": result,
                        "file_path": result,
                        "info": {**metadata, "quality": quality_str},
                        "is_fallback": False
                    }
                await self._debug("Download failed")
            
            # Try streaming
            await self._debug("Attempting stream...")
            stream_url = await self._stream(track)
            if stream_url:
                await self._debug(f"Stream URL obtained: {stream_url[:50]}...")
                return {
                    "url": stream_url,
                    "file_path": None,
                    "info": {**metadata, "quality": quality_str},
                    "is_fallback": False
                }
            
            await self._debug("Stream failed, using fallback")
            return await self._fallback(query, metadata)
            
        except Exception as e:
            log.error(f"Track retrieval failed: {e}")
            await self._debug(f"Track retrieval exception: {type(e).__name__}: {e}")
            import traceback
            if await self.config.debug_mode():
                log.error(traceback.format_exc())
            return await self._fallback(query)
    
    async def _stream(self, track) -> Optional[str]:
        """Get stream URL"""
        try:
            await self._debug(f"Calling track.get_url() on track ID {getattr(track, 'id', 'unknown')}")
            url = await self.bot.loop.run_in_executor(None, track.get_url)
            if url:
                await self._debug(f"Stream URL type: {type(url)}, length: {len(str(url))}")
                return url
            await self._debug("track.get_url() returned None")
        except AttributeError as e:
            await self._debug(f"track.get_url() not available: {e}")
        except Exception as e:
            await self._debug(f"Stream exception: {type(e).__name__}: {e}")
        return None
    
    async def _download(self, track, ctx, metadata: Dict) -> Optional[str]:
        """Download track"""
        try:
            track_id = metadata.get("track_id")
            if not track_id:
                await self._debug("No track ID available")
                return None
            
            await self._debug(f"Creating download message...")
            msg = await ctx.send("⏬ Downloading...")
            
            temp_dir = Path(tempfile.gettempdir()) / "tidalplayer"
            temp_dir.mkdir(exist_ok=True)
            await self._debug(f"Temp dir: {temp_dir}")
            
            # Sanitize filename
            safe_name = "".join(c for c in f"{metadata['artist']}-{metadata['title']}" 
                              if c.isalnum() or c in (' ', '-', '_'))[:100]
            file_path = temp_dir / f"{safe_name}_{track_id}.m4a"
            await self._debug(f"Target file: {file_path}")
            
            # Download
            def download():
                if hasattr(track, 'download'):
                    await self._debug("track.download() method exists")
                    track.download(str(file_path))
                    return file_path.exists()
                await self._debug("track.download() method not available")
                return False
            
            await self._debug("Starting download executor...")
            success = await self.bot.loop.run_in_executor(None, download)
            await self._debug(f"Download result: {success}")
            
            try:
                await msg.delete()
            except:
                pass
            
            if success:
                file_size = os.path.getsize(file_path)
                await self._debug(f"Download successful, file size: {file_size} bytes")
                return str(file_path)
            
            await self._debug("Download unsuccessful")
        except Exception as e:
            await self._debug(f"Download exception: {type(e).__name__}: {e}")
            import traceback
            if await self.config.debug_mode():
                log.error(traceback.format_exc())
        
        return None
    
    async def _fallback(self, query: str, metadata: Dict = None) -> Optional[Dict]:
        """YouTube fallback"""
        fallback_enabled = await self.config.fallback_to_youtube()
        await self._debug(f"Fallback enabled: {fallback_enabled}")
        
        if not fallback_enabled:
            return None
        
        if metadata:
            search = f"{metadata['artist']} {metadata['title']}"
            await self._debug(f"YouTube fallback search: {search}")
            return {
                "url": search,
                "file_path": None,
                "info": {**metadata, "quality": "YouTube"},
                "is_fallback": True
            }
        
        await self._debug(f"YouTube fallback direct: {query}")
        return {
            "url": query,
            "file_path": None,
            "info": {"title": query, "artist": "Unknown", "album": "Unknown", "quality": "YouTube"},
            "is_fallback": True
        }
    
    def _create_embed(self, info: Dict) -> discord.Embed:
        """Create track info embed"""
        quality = info.get("quality", "Unknown")
        
        if "HI_RES" in quality or "MQA" in quality:
            color, emoji = discord.Color.gold(), "💎"
        elif "LOSSLESS" in quality:
            color, emoji = discord.Color.blue(), "🎵"
        else:
            color, emoji = discord.Color.green(), "🎶"
        
        embed = discord.Embed(
            title=f"{emoji} Playing from Tidal",
            description=f"**{info['title']}**\nby {info['artist']}",
            color=color
        )
        
        if info.get("album") and info["album"] != "Unknown":
            embed.add_field(name="Album", value=info["album"], inline=True)
        embed.add_field(name="Quality", value=quality, inline=True)
        
        return embed
    
    @commands.group()
    @commands.is_owner()
    async def tidalplay(self, ctx):
        """Tidal configuration"""
        pass
    
    @tidalplay.command(name="setup", hidden=True)
    async def setup(self, ctx):
        """Authenticate with Tidal"""
        if not TIDALAPI_AVAILABLE:
            return await ctx.send("❌ Install tidalapi first")
        
        try:
            login, future = self.session.login_oauth()
            
            embed = discord.Embed(
                title="Tidal Authentication",
                description=f"Visit:\n{login.verification_uri_complete}",
                color=discord.Color.blue()
            )
            embed.set_footer(text="5 minute timeout | Requires Tidal HiFi subscription")
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
                
                await ctx.send("✅ Authenticated! Use `>tplay <song>` to play")
            else:
                await ctx.send("❌ Authentication failed")
        except asyncio.TimeoutError:
            await ctx.send("⏱️ Timeout")
        except Exception as e:
            await ctx.send(f"❌ Error: {e}")
    
    @tidalplay.command(name="debug")
    async def debug(self, ctx):
        """Toggle debug mode for detailed console logging"""
        current = await self.config.debug_mode()
        await self.config.debug_mode.set(not current)
        
        status = "enabled" if not current else "disabled"
        await ctx.send(f"🐛 Debug mode **{status}**\nCheck your console for detailed logs")
    
    @tidalplay.command(name="downloadmode")
    async def downloadmode(self, ctx):
        """Toggle download mode"""
        current = await self.config.download_mode()
        await self.config.download_mode.set(not current)
        
        status = "enabled" if not current else "disabled"
        await ctx.send(f"✅ Download mode **{status}**")
    
    @tidalplay.command(name="fallback")
    async def fallback(self, ctx):
        """Toggle YouTube fallback"""
        current = await self.config.fallback_to_youtube()
        await self.config.fallback_to_youtube.set(not current)
        
        status = "enabled" if not current else "disabled"
        await ctx.send(f"✅ YouTube fallback **{status}**")
    
    @tidalplay.command(name="country")
    async def country(self, ctx, code: str):
        """Set country code (US, GB, DE, etc.)"""
        await self.config.country_code.set(code.upper())
        await ctx.send(f"✅ Country: **{code.upper()}**")
    
    @tidalplay.command(name="status")
    async def status(self, ctx):
        """Show configuration"""
        if not TIDALAPI_AVAILABLE:
            return await ctx.send("❌ tidalapi not installed")
        
        authenticated = self.session and self.session.check_login()
        download_mode = await self.config.download_mode()
        fallback = await self.config.fallback_to_youtube()
        country = await self.config.country_code()
        debug_mode = await self.config.debug_mode()
        
        embed = discord.Embed(
            title="Tidal Player Status",
            color=discord.Color.green() if authenticated else discord.Color.red()
        )
        
        embed.add_field(name="Authentication", value="✅" if authenticated else "❌", inline=True)
        embed.add_field(name="Download Mode", value="✅" if download_mode else "❌", inline=True)
        embed.add_field(name="YouTube Fallback", value="✅" if fallback else "❌", inline=True)
        embed.add_field(name="Country", value=country, inline=True)
        embed.add_field(name="Debug Mode", value="🐛" if debug_mode else "❌", inline=True)
        embed.add_field(name="Cached File", value="Yes" if self.last_file else "No", inline=True)
        
        if not authenticated:
            embed.set_footer(text="Run >tidalplay setup")
        else:
            embed.set_footer(text="Plays highest quality available automatically")
        
        await ctx.send(embed=embed)
    
    @tidalplay.command(name="clear")
    async def clear(self, ctx):
        """Delete cached file"""
        if self.last_file:
            self._cleanup_file(self.last_file)
            self.last_file = None
            await ctx.send("✅ Cache cleared")
        else:
            await ctx.send("ℹ️ No cached file")
    
    def cog_unload(self):
        """Cleanup on unload"""
        if self.last_file:
            self._cleanup_file(self.last_file)
        log.info("TidalPlayer cog unloaded")
