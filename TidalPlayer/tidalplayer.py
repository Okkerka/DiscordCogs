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
    from tidalapi.media import Quality
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
            "download_mode": False
        }
        self.config.register_global(**default_global)
        self.session = tidalapi.Session() if TIDALAPI_AVAILABLE else None
        self.last_file = None
        
        if TIDALAPI_AVAILABLE:
            bot.loop.create_task(self._load_session())
    
    async def _load_session(self):
        """Load saved Tidal session"""
        try:
            token_type = await self.config.token_type()
            access_token = await self.config.access_token()
            refresh_token = await self.config.refresh_token()
            
            if all([token_type, access_token, refresh_token]):
                self.session.load_oauth_session(token_type, access_token, refresh_token)
                log.info("Tidal session loaded")
        except Exception as e:
            log.error(f"Session load failed: {e}")
    
    def _cleanup_file(self, file_path: str):
        """Delete a file if it exists"""
        if file_path and os.path.exists(file_path):
            try:
                os.remove(file_path)
                log.info(f"Deleted: {file_path}")
            except Exception as e:
                log.error(f"Delete failed: {e}")
    
    def _get_quality(self, track) -> tuple:
        """Get highest quality available"""
        # Available Quality values in tidalapi:
        # Quality.hi_res_lossless (for HI_RES/MQA)
        # Quality.lossless (for LOSSLESS)
        # Quality.high (for HIGH)
        # Quality.low (for LOW)
        
        try:
            if hasattr(track, 'audio_quality'):
                quality_map = {
                    'HI_RES': (Quality.hi_res_lossless, "HI_RES (MQA)"),
                    'LOSSLESS': (Quality.lossless, "LOSSLESS (FLAC)"),
                    'HIGH': (Quality.high, "HIGH (320kbps)"),
                    'LOW': (Quality.low, "LOW (96kbps)")
                }
                result = quality_map.get(track.audio_quality, (Quality.lossless, "LOSSLESS (FLAC)"))
                log.info(f"Track quality: {track.audio_quality} -> {result[1]}")
                return result
        except Exception as e:
            log.warning(f"Quality detection failed: {e}")
        
        # Default to lossless
        return Quality.lossless, "LOSSLESS (FLAC)"
    
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
        if not TIDALAPI_AVAILABLE:
            return await ctx.send("❌ Install tidalapi: `[p]pipinstall tidalapi`")
        
        if not self.session or not self.session.check_login():
            return await ctx.send("❌ Not authenticated. Run `>tidalplay setup`")
        
        if not self.bot.get_cog("Audio"):
            return await ctx.send("❌ Audio cog not loaded")
        
        # Clean up previous file
        if self.last_file:
            self._cleanup_file(self.last_file)
            self.last_file = None
        
        # Get track
        async with ctx.typing():
            result = await self._get_track(query, ctx)
        
        if not result:
            return await ctx.send("❌ Track not found")
        
        # Send embed only if playing from Tidal
        if not result.get("is_fallback"):
            await ctx.send(embed=self._create_embed(result["info"]))
        
        # Save file for cleanup
        if result.get("file_path"):
            self.last_file = result["file_path"]
        
        # Play via Audio cog
        try:
            audio = self.bot.get_cog("Audio")
            await audio.command_play(ctx, query=result["url"])
        except Exception as e:
            log.error(f"Play failed: {e}")
            await ctx.send(f"❌ Playback failed")
    
    async def _get_track(self, query: str, ctx) -> Optional[Dict]:
        """Search and get track from Tidal"""
        try:
            # Search
            results = await self.bot.loop.run_in_executor(None, self.session.search, query)
            
            if not results or not results.get('tracks'):
                log.warning("No tracks found")
                return await self._fallback(query)
            
            track = results['tracks'][0]
            metadata = self._extract_metadata(track)
            quality, quality_str = self._get_quality(track)
            
            log.info(f"Found: {metadata['title']} by {metadata['artist']} ({quality_str})")
            
            # Try download mode first, then streaming
            if await self.config.download_mode():
                result = await self._download(track, quality, ctx, metadata)
                if result:
                    return {
                        "url": result,
                        "file_path": result,
                        "info": {**metadata, "quality": quality_str},
                        "is_fallback": False
                    }
            
            # Try streaming
            stream_url = await self._stream(track, quality)
            if stream_url:
                return {
                    "url": stream_url,
                    "file_path": None,
                    "info": {**metadata, "quality": quality_str},
                    "is_fallback": False
                }
            
            # Fallback to YouTube
            log.warning("Stream/download failed, using fallback")
            return await self._fallback(query, metadata)
            
        except Exception as e:
            log.error(f"Track retrieval failed: {e}")
            return await self._fallback(query)
    
    async def _stream(self, track, quality) -> Optional[str]:
        """Get stream URL"""
        try:
            url = await self.bot.loop.run_in_executor(None, track.get_url)
            if url:
                log.info("Got stream URL")
                return url
        except AttributeError:
            log.warning("track.get_url() not available")
        except Exception as e:
            log.warning(f"Streaming failed: {e}")
        return None
    
    async def _download(self, track, quality, ctx, metadata: Dict) -> Optional[str]:
        """Download track"""
        try:
            track_id = metadata.get("track_id")
            if not track_id:
                log.warning("No track ID for download")
                return None
            
            msg = await ctx.send("⏬ Downloading...")
            
            temp_dir = Path(tempfile.gettempdir()) / "tidalplayer"
            temp_dir.mkdir(exist_ok=True)
            
            # Sanitize filename
            safe_name = "".join(c for c in f"{metadata['artist']}-{metadata['title']}" 
                              if c.isalnum() or c in (' ', '-', '_'))[:100]
            file_path = temp_dir / f"{safe_name}_{track_id}.m4a"
            
            # Download
            def download():
                if hasattr(track, 'download'):
                    track.download(str(file_path), quality=quality)
                    return file_path.exists()
                return False
            
            success = await self.bot.loop.run_in_executor(None, download)
            
            try:
                await msg.delete()
            except:
                pass
            
            if success:
                log.info(f"Downloaded: {metadata['title']}")
                return str(file_path)
            
            log.warning("Download method unavailable")
        except Exception as e:
            log.error(f"Download failed: {e}")
        
        return None
    
    async def _fallback(self, query: str, metadata: Dict = None) -> Optional[Dict]:
        """YouTube fallback"""
        if not await self.config.fallback_to_youtube():
            return None
        
        if metadata:
            search = f"{metadata['artist']} {metadata['title']}"
            log.info(f"YouTube fallback: {search}")
            return {
                "url": search,
                "file_path": None,
                "info": {**metadata, "quality": "YouTube"},
                "is_fallback": True
            }
        
        return {
            "url": query,
            "file_path": None,
            "info": {"title": query, "artist": "Unknown", "album": "Unknown", "quality": "YouTube"},
            "is_fallback": True
        }
    
    def _create_embed(self, info: Dict) -> discord.Embed:
        """Create track info embed"""
        quality = info.get("quality", "Unknown")
        
        # Color based on quality
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
        
        embed = discord.Embed(
            title="Tidal Player Status",
            color=discord.Color.green() if authenticated else discord.Color.red()
        )
        
        embed.add_field(name="Authentication", value="✅" if authenticated else "❌", inline=True)
        embed.add_field(name="Download Mode", value="✅" if download_mode else "❌", inline=True)
        embed.add_field(name="YouTube Fallback", value="✅" if fallback else "❌", inline=True)
        embed.add_field(name="Country", value=country, inline=True)
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
