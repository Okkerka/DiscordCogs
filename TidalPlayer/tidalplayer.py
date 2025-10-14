from redbot.core import commands, Config
import discord
import logging
import asyncio
from typing import Optional, Dict, Any
import time
import os
from pathlib import Path

try:
    import tidalapi
    from tidalapi.media import Quality
    TIDALAPI_AVAILABLE = True
except ImportError:
    TIDALAPI_AVAILABLE = False

log = logging.getLogger("red.tidalplayer")

class TidalPlayer(commands.Cog):
    """Play music directly from Tidal with Red's Audio cog"""
    
    def __init__(self, bot):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=1234567890)
        default_global = {
            "token_type": None,
            "access_token": None,
            "refresh_token": None,
            "expiry_time": None,
            "country_code": "US",
            "quality": "HIGH",  # LOW, HIGH, LOSSLESS, HI_RES
            "fallback_to_youtube": True,
            "download_mode": False
        }
        self.config.register_global(**default_global)
        self.session = tidalapi.Session() if TIDALAPI_AVAILABLE else None
        self.last_played_file = None  # Track the last played file to delete on next play
        
        if TIDALAPI_AVAILABLE:
            bot.loop.create_task(self._load_session())
    
    async def _load_session(self):
        """Load saved session on startup"""
        token_type = await self.config.token_type()
        access_token = await self.config.access_token()
        refresh_token = await self.config.refresh_token()
        
        if all([token_type, access_token, refresh_token]):
            try:
                self.session.load_oauth_session(token_type, access_token, refresh_token)
                log.info("Tidal session loaded from config")
            except Exception as e:
                log.error(f"Failed to load session: {e}")
    
    async def _delete_previous_file(self):
        """Delete the previously played file"""
        if self.last_played_file and os.path.exists(self.last_played_file):
            try:
                os.remove(self.last_played_file)
                log.info(f"Deleted previous file: {self.last_played_file}")
            except Exception as e:
                log.error(f"Failed to delete previous file {self.last_played_file}: {e}")
            finally:
                self.last_played_file = None
    
    @commands.command(name="tplay", aliases=["tidalplayer"])
    async def tidal_play(self, ctx, *, query: str):
        """Play a song from Tidal"""
        if not TIDALAPI_AVAILABLE:
            return await ctx.send("❌ tidalapi is not installed. Install with: `[p]pipinstall tidalapi`")
        
        if not self.session or not self.session.check_login():
            return await ctx.send("❌ Not authenticated with Tidal. Run `>tidalplay setup` first.")
        
        # Get Audio cog
        audio = self.bot.get_cog("Audio")
        if not audio:
            return await ctx.send("❌ Audio cog is not loaded. Load it with: `[p]load audio`")
        
        # Delete previous file before downloading/playing new one
        await self._delete_previous_file()
        
        # Search Tidal and get playable URL
        async with ctx.typing():
            result = await self.get_tidal_playback(query, ctx)
        
        if not result:
            return await ctx.send("❌ Could not find that track on Tidal or YouTube.")
        
        new_query = result["url"]
        track_info = result.get("info", {})
        is_fallback = result.get("is_fallback", False)
        downloaded_file = result.get("file_path", None)
        
        # Save the downloaded file path for deletion on next play
        if downloaded_file:
            self.last_played_file = downloaded_file
        
        # Only send embed if actually playing from Tidal (not YouTube fallback)
        if not is_fallback:
            title = track_info.get("title", "Unknown")
            artist = track_info.get("artist", "Unknown")
            album = track_info.get("album", "Unknown")
            quality = track_info.get("quality", "Unknown")
            
            embed = discord.Embed(
                title="🎵 Playing from Tidal",
                description=f"**{title}**\nby {artist}",
                color=discord.Color.blue()
            )
            
            if album and album != "Unknown":
                embed.add_field(name="Album", value=album, inline=True)
            embed.add_field(name="Quality", value=quality, inline=True)
            embed.add_field(name="Source", value="Tidal Direct", inline=True)
            
            await ctx.send(embed=embed)
        
        # Use Audio cog to play
        try:
            await audio.command_play(ctx, query=new_query)
        except Exception as e:
            log.error(f"Failed to play via Audio cog: {e}")
            await ctx.send(f"❌ Failed to play track: {e}")
    
    async def get_tidal_playback(self, query: str, ctx) -> Optional[Dict[str, Any]]:
        """Get playable URL from Tidal (stream or downloaded file)"""
        try:
            # Search Tidal
            log.info(f"Searching Tidal for: {query}")
            results = await self.bot.loop.run_in_executor(
                None,
                self.session.search,
                query
            )
            
            if not results or not results.get('tracks') or len(results['tracks']) == 0:
                log.warning(f"No Tidal results for: {query}")
                return await self._fallback_search(query)
            
            track = results['tracks'][0]
            
            # Extract full metadata properly
            metadata = {
                "title": track.name if hasattr(track, 'name') else "Unknown",
                "artist": "Unknown",
                "album": "Unknown",
                "duration": getattr(track, 'duration', 0)
            }
            
            # Get artist name
            if hasattr(track, 'artist') and track.artist:
                if hasattr(track.artist, 'name'):
                    metadata["artist"] = track.artist.name
                elif isinstance(track.artist, str):
                    metadata["artist"] = track.artist
            
            # Get album name
            if hasattr(track, 'album') and track.album:
                if hasattr(track.album, 'name'):
                    metadata["album"] = track.album.name
                elif isinstance(track.album, str):
                    metadata["album"] = track.album
            
            log.info(f"Found track: {metadata['title']} by {metadata['artist']} from album {metadata['album']}")
            
            # Get quality setting
            quality_str = await self.config.quality()
            quality = self._get_quality_enum(quality_str)
            
            # Try to get stream URL
            download_mode = await self.config.download_mode()
            url = None
            file_path = None
            
            if download_mode:
                # Download the track first
                log.info("Download mode enabled, attempting download...")
                download_result = await self._download_track(track, quality, ctx, metadata)
                if download_result:
                    url, file_path = download_result
                    log.info(f"Successfully downloaded track")
            else:
                # Try direct streaming
                log.info("Attempting direct stream...")
                url = await self._get_stream_url(track, quality)
                if url:
                    log.info(f"Got direct stream URL")
            
            if url:
                return {
                    "url": url,
                    "info": {
                        **metadata,
                        "quality": quality_str,
                        "source": "Tidal Direct"
                    },
                    "is_fallback": False,
                    "file_path": file_path  # Include file path for deletion later
                }
            
            # If we couldn't get URL, try fallback
            log.warning(f"Could not get Tidal stream/download for: {track.name}, using YouTube fallback")
            return await self._fallback_search(query, metadata)
            
        except Exception as e:
            log.error(f"Error getting Tidal playback: {e}", exc_info=True)
            return await self._fallback_search(query)
    
    async def _get_stream_url(self, track, quality) -> Optional[str]:
        """Attempt to get direct stream URL from Tidal"""
        try:
            # Try to get stream URL
            stream_url = await self.bot.loop.run_in_executor(
                None,
                track.get_url
            )
            
            if stream_url:
                log.info(f"Got Tidal stream URL: {stream_url[:50]}...")
                return stream_url
            
        except AttributeError:
            log.warning("track.get_url() not available - tidalapi version may not support streaming")
        except Exception as e:
            log.warning(f"Failed to get stream URL: {e}")
        
        return None
    
    async def _download_track(self, track, quality, ctx, metadata: dict) -> Optional[tuple]:
        """Download track and return (file_path, file_path) tuple"""
        try:
            import tempfile
            
            track_id = getattr(track, 'id', None)
            
            msg = await ctx.send("⏬ Downloading from Tidal...")
            
            # Create temp directory
            temp_dir = Path(tempfile.gettempdir()) / "tidalplayer"
            temp_dir.mkdir(exist_ok=True)
            
            # Sanitize filename
            artist_name = metadata.get("artist", "Unknown")
            track_name = metadata.get("title", "Unknown")
            safe_name = "".join(c for c in f"{artist_name} - {track_name}" if c.isalnum() or c in (' ', '-', '_')).strip()
            
            # Add track ID to make it unique
            file_path = temp_dir / f"{safe_name}_{track_id}.m4a"
            
            # Try to download using tidalapi
            def download():
                try:
                    # Some versions of tidalapi support this
                    if hasattr(track, 'download'):
                        track.download(str(file_path), quality=quality)
                        return True
                except Exception as e:
                    log.error(f"Download failed: {e}")
                return False
            
            success = await self.bot.loop.run_in_executor(None, download)
            
            try:
                await msg.delete()
            except:
                pass
            
            if success and file_path.exists():
                log.info(f"Successfully downloaded: {metadata['title']}")
                return (str(file_path), str(file_path))
            else:
                log.warning("Download method not available or failed")
                
        except Exception as e:
            log.error(f"Failed to download track: {e}", exc_info=True)
        
        return None
    
    async def _fallback_search(self, query: str, metadata: dict = None) -> Optional[Dict[str, Any]]:
        """Fallback to YouTube search if Tidal fails"""
        fallback_enabled = await self.config.fallback_to_youtube()
        
        if not fallback_enabled:
            return None
        
        if metadata:
            # Use Tidal metadata for better YouTube search
            search_query = f"{metadata['artist']} {metadata['title']}"
            log.info(f"Using YouTube fallback with Tidal metadata: {search_query}")
            return {
                "url": search_query,
                "info": {
                    **metadata,
                    "quality": "YouTube",
                    "source": "YouTube"
                },
                "is_fallback": True,
                "file_path": None
            }
        else:
            # Direct search
            log.info(f"Using YouTube fallback for: {query}")
            return {
                "url": query,
                "info": {
                    "title": query,
                    "artist": "Unknown",
                    "album": "Unknown",
                    "quality": "YouTube",
                    "source": "YouTube"
                },
                "is_fallback": True,
                "file_path": None
            }
    
    def _get_quality_enum(self, quality_str: str):
        """Convert quality string to tidalapi Quality enum"""
        quality_map = {
            "LOW": Quality.low_96k,
            "HIGH": Quality.high_320k,
            "LOSSLESS": Quality.lossless,
            "HI_RES": Quality.hi_res
        }
        return quality_map.get(quality_str.upper(), Quality.high_320k)
    
    @commands.group()
    @commands.is_owner()
    async def tidalplay(self, ctx):
        """TidalPlayer commands"""
        if ctx.invoked_subcommand is None:
            await ctx.send_help()
    
    @tidalplay.command(name="setup", hidden=True)
    async def tidalsetup(self, ctx):
        """Set up Tidal OAuth authentication"""
        if not TIDALAPI_AVAILABLE:
            return await ctx.send("❌ tidalapi is not installed. Install with: `[p]pipinstall tidalapi`")
        
        await ctx.send("Starting OAuth setup...")
        
        try:
            login, future = self.session.login_oauth()
            
            embed = discord.Embed(
                title="Tidal OAuth Setup",
                description=f"Visit this URL and log in:\n{login.verification_uri_complete}",
                color=discord.Color.blue()
            )
            embed.add_field(name="⏱️ Timeout", value="5 minutes", inline=False)
            embed.set_footer(text="Make sure you have a Tidal HiFi subscription for best results")
            await ctx.send(embed=embed)
            
            try:
                await asyncio.wait_for(
                    self.bot.loop.run_in_executor(None, future.result),
                    timeout=300
                )
            except asyncio.TimeoutError:
                return await ctx.send("⏱️ OAuth timed out. Please try again.")
            
            if self.session.check_login():
                await self.config.token_type.set(self.session.token_type)
                await self.config.access_token.set(self.session.access_token)
                await self.config.refresh_token.set(self.session.refresh_token)
                
                if hasattr(self.session, "expiry_time") and self.session.expiry_time:
                    await self.config.expiry_time.set(self.session.expiry_time.timestamp())
                
                await ctx.send("✅ **Setup complete!** Tidal integration is now active.\nUse `>tplay <song name>` to play from Tidal.")
                log.info("OAuth setup completed successfully")
            else:
                await ctx.send("❌ Login failed. Please try again.")
        except Exception as e:
            await ctx.send(f"❌ Error during setup: {e}")
            log.error(f"OAuth error: {e}", exc_info=True)
    
    @tidalplay.command(name="quality")
    async def set_quality(self, ctx, quality: str):
        """Set playback quality: LOW, HIGH, LOSSLESS, HI_RES"""
        quality = quality.upper()
        valid_qualities = ["LOW", "HIGH", "LOSSLESS", "HI_RES"]
        
        if quality not in valid_qualities:
            return await ctx.send(f"❌ Invalid quality. Choose from: {', '.join(valid_qualities)}")
        
        await self.config.quality.set(quality)
        await ctx.send(f"✅ Playback quality set to: **{quality}**")
    
    @tidalplay.command(name="downloadmode")
    async def toggle_download_mode(self, ctx):
        """Toggle download mode (downloads tracks before playing)"""
        current = await self.config.download_mode()
        await self.config.download_mode.set(not current)
        
        if not current:
            await ctx.send("✅ **Download mode enabled** - Tracks will be downloaded before playing\n💡 Files are auto-deleted when you play the next song")
        else:
            await ctx.send("❌ **Download mode disabled** - Will attempt direct streaming")
    
    @tidalplay.command(name="fallback")
    async def toggle_fallback(self, ctx):
        """Toggle YouTube fallback if Tidal fails"""
        current = await self.config.fallback_to_youtube()
        await self.config.fallback_to_youtube.set(not current)
        
        if not current:
            await ctx.send("✅ **YouTube fallback enabled**")
        else:
            await ctx.send("❌ **YouTube fallback disabled**")
    
    @tidalplay.command(name="clearcurrent")
    async def clear_current(self, ctx):
        """Delete the currently cached file"""
        if self.last_played_file and os.path.exists(self.last_played_file):
            try:
                os.remove(self.last_played_file)
                await ctx.send(f"✅ Deleted current cached file")
                self.last_played_file = None
            except Exception as e:
                await ctx.send(f"❌ Failed to delete file: {e}")
        else:
            await ctx.send("ℹ️ No file currently cached")
    
    @tidalplay.command(name="country")
    async def set_country(self, ctx, country_code: str):
        """Set Tidal country code (e.g., US, GB, DE)"""
        await self.config.country_code.set(country_code.upper())
        await ctx.send(f"✅ Tidal country code set to: **{country_code.upper()}**")
    
    @tidalplay.command(name="status")
    async def check_status(self, ctx):
        """Check Tidal configuration and authentication status"""
        if not TIDALAPI_AVAILABLE:
            return await ctx.send("❌ tidalapi is not installed. Install with: `[p]pipinstall tidalapi`")
        
        quality = await self.config.quality()
        country = await self.config.country_code()
        download_mode = await self.config.download_mode()
        fallback = await self.config.fallback_to_youtube()
        
        embed = discord.Embed(
            title="Tidal Player Status",
            color=discord.Color.green() if self.session.check_login() else discord.Color.red()
        )
        
        # Authentication status
        auth_status = "✅ Authenticated" if self.session and self.session.check_login() else "❌ Not authenticated"
        embed.add_field(name="Authentication", value=auth_status, inline=False)
        
        # Settings
        embed.add_field(name="Quality", value=quality, inline=True)
        embed.add_field(name="Country", value=country, inline=True)
        embed.add_field(name="Download Mode", value="✅ On" if download_mode else "❌ Off", inline=True)
        embed.add_field(name="YouTube Fallback", value="✅ On" if fallback else "❌ Off", inline=True)
        embed.add_field(name="Cached File", value="Yes" if self.last_played_file and os.path.exists(self.last_played_file) else "No", inline=True)
        
        if not self.session or not self.session.check_login():
            embed.set_footer(text="Run >tidalplay setup to authenticate")
        else:
            embed.set_footer(text="Use >tplay <song name> to play from Tidal • Files auto-delete on next play")
        
        await ctx.send(embed=embed)
    
    def cog_unload(self):
        """Cleanup when cog is unloaded"""
        # Clean up the last played file
        if self.last_played_file and os.path.exists(self.last_played_file):
            try:
                os.remove(self.last_played_file)
                log.info(f"Cleaned up last played file on unload: {self.last_played_file}")
            except:
                pass
        
        log.info("TidalPlayer cog unloaded")
