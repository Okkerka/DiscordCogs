from redbot.core import commands, Config
import discord
import logging
import asyncio
from typing import Optional, Dict, Any
import subprocess
import tempfile
import json
import os
from pathlib import Path
import shutil

try:
    import tidalapi
    TIDALAPI_AVAILABLE = True
except ImportError:
    TIDALAPI_AVAILABLE = False

log = logging.getLogger("red.tidalplayer")

class TidalPlayer(commands.Cog):
    """Play Lossless/Hi-Res music from Tidal using tidal-dl downloader"""
    
    def __init__(self, bot):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=1234567890)
        default_global = {
            "token_type": None,
            "access_token": None,
            "refresh_token": None,
            "expiry_time": None,
            "country_code": "US",
            "quality": "LOSSLESS",  # LOW, HIGH, LOSSLESS, HI_RES, MASTER
            "fallback_to_youtube": True,
            "tidal_dl_path": None,  # Path to tidal-dl executable
            "temp_download_dir": None
        }
        self.config.register_global(**default_global)
        self.session = tidalapi.Session() if TIDALAPI_AVAILABLE else None
        self.last_played_file = None
        
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
    
    async def _setup_tidal_dl(self):
        """Setup tidal-dl if not already configured"""
        tidal_dl_path = await self.config.tidal_dl_path()
        
        if not tidal_dl_path or not shutil.which(tidal_dl_path):
            # Try to find tidal-dl in PATH
            tidal_dl_path = shutil.which("tidal-dl")
            if tidal_dl_path:
                await self.config.tidal_dl_path.set(tidal_dl_path)
                return tidal_dl_path
            else:
                return None
        
        return tidal_dl_path
    
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
    
    @commands.command(name="tplay", aliases=["tidalplay"])
    async def tidal_play(self, ctx, *, query: str):
        """Play a song from Tidal in Lossless/Hi-Res quality"""
        if not TIDALAPI_AVAILABLE:
            return await ctx.send("❌ tidalapi is not installed. Install with: `[p]pipinstall tidalapi`")
        
        if not self.session or not self.session.check_login():
            return await ctx.send("❌ Not authenticated with Tidal. Run `>tidalplay setup` first.")
        
        # Check for tidal-dl
        tidal_dl_path = await self._setup_tidal_dl()
        if not tidal_dl_path:
            return await ctx.send("❌ tidal-dl not found. Install with: `pip install tidal-dl` then run `>tidalplay configure`")
        
        # Get Audio cog
        audio = self.bot.get_cog("Audio")
        if not audio:
            return await ctx.send("❌ Audio cog is not loaded. Load it with: `[p]load audio`")
        
        # Delete previous file before downloading new one
        await self._delete_previous_file()
        
        # Search and download from Tidal
        async with ctx.typing():
            result = await self.download_from_tidal(query, ctx, tidal_dl_path)
        
        if not result:
            return await ctx.send("❌ Could not find or download that track from Tidal.")
        
        file_path = result["file_path"]
        track_info = result.get("info", {})
        is_fallback = result.get("is_fallback", False)
        
        # Save file path for deletion on next play
        self.last_played_file = file_path
        
        # Only send embed if actually playing from Tidal
        if not is_fallback:
            title = track_info.get("title", "Unknown")
            artist = track_info.get("artist", "Unknown")
            album = track_info.get("album", "Unknown")
            quality = track_info.get("quality", "Unknown")
            
            embed = discord.Embed(
                title="🎵 Playing Lossless from Tidal",
                description=f"**{title}**\nby {artist}",
                color=discord.Color.gold()  # Gold for lossless
            )
            
            if album and album != "Unknown":
                embed.add_field(name="Album", value=album, inline=True)
            embed.add_field(name="Quality", value=quality, inline=True)
            embed.add_field(name="Source", value="Tidal Lossless", inline=True)
            
            await ctx.send(embed=embed)
        
        # Use Audio cog to play the local file
        try:
            await audio.command_play(ctx, query=file_path)
        except Exception as e:
            log.error(f"Failed to play via Audio cog: {e}")
            await ctx.send(f"❌ Failed to play track: {e}")
    
    async def download_from_tidal(self, query: str, ctx, tidal_dl_path: str) -> Optional[Dict[str, Any]]:
        """Download track from Tidal using tidal-dl"""
        try:
            # Search Tidal first to get track info
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
            
            # Extract metadata
            metadata = {
                "title": track.name if hasattr(track, 'name') else "Unknown",
                "artist": "Unknown",
                "album": "Unknown",
                "track_id": getattr(track, 'id', None)
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
            
            if not metadata["track_id"]:
                log.error("Could not get track ID from Tidal")
                return await self._fallback_search(query, metadata)
            
            log.info(f"Found track: {metadata['title']} by {metadata['artist']} (ID: {metadata['track_id']})")
            
            # Download using tidal-dl
            download_msg = await ctx.send("⏬ Downloading Lossless from Tidal...")
            
            temp_dir = Path(tempfile.gettempdir()) / "tidalplayer"
            temp_dir.mkdir(exist_ok=True)
            
            quality = await self.config.quality()
            
            # Prepare tidal-dl command
            cmd = [
                tidal_dl_path,
                "-t", str(metadata["track_id"]),  # Track ID
                "-q", quality,  # Quality
                "-o", str(temp_dir),  # Output directory
                "--no-playlist"  # Don't create playlist files
            ]
            
            # Run tidal-dl
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            
            stdout, stderr = await process.communicate()
            
            try:
                await download_msg.delete()
            except:
                pass
            
            if process.returncode == 0:
                # Find the downloaded file
                downloaded_file = await self._find_downloaded_file(temp_dir, metadata)
                
                if downloaded_file:
                    log.info(f"Successfully downloaded: {metadata['title']}")
                    return {
                        "file_path": downloaded_file,
                        "info": {
                            **metadata,
                            "quality": f"{quality} Lossless",
                            "source": "Tidal Direct"
                        },
                        "is_fallback": False
                    }
                else:
                    log.error("Downloaded file not found")
            else:
                log.error(f"tidal-dl failed: {stderr.decode()}")
            
            # If download failed, try fallback
            return await self._fallback_search(query, metadata)
            
        except Exception as e:
            log.error(f"Error downloading from Tidal: {e}", exc_info=True)
            return await self._fallback_search(query)
    
    async def _find_downloaded_file(self, temp_dir: Path, metadata: dict) -> Optional[str]:
        """Find the downloaded file in the temp directory"""
        try:
            # Common audio extensions
            extensions = ['.flac', '.m4a', '.mp3', '.wav']
            
            # Look for files with the track name
            artist = metadata.get("artist", "")
            title = metadata.get("title", "")
            
            for file_path in temp_dir.rglob("*"):
                if file_path.is_file() and file_path.suffix.lower() in extensions:
                    # Check if filename contains artist and title
                    filename_lower = file_path.name.lower()
                    if (artist.lower() in filename_lower or title.lower() in filename_lower):
                        return str(file_path)
            
            # If no match found, return the newest audio file
            audio_files = []
            for ext in extensions:
                audio_files.extend(temp_dir.rglob(f"*{ext}"))
            
            if audio_files:
                # Return the most recently created file
                newest = max(audio_files, key=os.path.getctime)
                return str(newest)
            
        except Exception as e:
            log.error(f"Error finding downloaded file: {e}")
        
        return None
    
    async def _fallback_search(self, query: str, metadata: dict = None) -> Optional[Dict[str, Any]]:
        """Fallback to YouTube search if Tidal fails"""
        fallback_enabled = await self.config.fallback_to_youtube()
        
        if not fallback_enabled:
            return None
        
        if metadata:
            search_query = f"{metadata['artist']} {metadata['title']}"
            log.info(f"Using YouTube fallback with Tidal metadata: {search_query}")
            return {
                "file_path": search_query,  # Let Audio cog search YouTube
                "info": {
                    **metadata,
                    "quality": "YouTube",
                    "source": "YouTube"
                },
                "is_fallback": True
            }
        else:
            log.info(f"Using YouTube fallback for: {query}")
            return {
                "file_path": query,
                "info": {
                    "title": query,
                    "artist": "Unknown",
                    "album": "Unknown",
                    "quality": "YouTube",
                    "source": "YouTube"
                },
                "is_fallback": True
            }
    
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
            embed.set_footer(text="Make sure you have a Tidal HiFi Plus subscription for Lossless downloads")
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
                
                await ctx.send("✅ **Setup complete!** Now install tidal-dl with: `pip install tidal-dl`\nThen run `>tidalplay configure`")
                log.info("OAuth setup completed successfully")
            else:
                await ctx.send("❌ Login failed. Please try again.")
        except Exception as e:
            await ctx.send(f"❌ Error during setup: {e}")
            log.error(f"OAuth error: {e}", exc_info=True)
    
    @tidalplay.command(name="configure")
    async def configure_tidal_dl(self, ctx):
        """Configure tidal-dl for Lossless downloads"""
        # Check if tidal-dl is installed
        tidal_dl_path = shutil.which("tidal-dl")
        if not tidal_dl_path:
            return await ctx.send("❌ tidal-dl not found. Install with: `pip install tidal-dl`")
        
        await self.config.tidal_dl_path.set(tidal_dl_path)
        
        # Get session tokens for tidal-dl
        access_token = await self.config.access_token()
        refresh_token = await self.config.refresh_token()
        
        if not access_token:
            return await ctx.send("❌ No Tidal tokens found. Run `>tidalplay setup` first.")
        
        embed = discord.Embed(
            title="Tidal-DL Configuration",
            description="Setting up tidal-dl for Lossless downloads...",
            color=discord.Color.blue()
        )
        embed.add_field(name="Path", value=tidal_dl_path, inline=False)
        embed.add_field(name="Status", value="✅ Ready for Lossless downloads", inline=False)
        
        await ctx.send(embed=embed)
    
    @tidalplay.command(name="quality")
    async def set_quality(self, ctx, quality: str):
        """Set download quality: LOW, HIGH, LOSSLESS, HI_RES, MASTER"""
        quality = quality.upper()
        valid_qualities = ["LOW", "HIGH", "LOSSLESS", "HI_RES", "MASTER"]
        
        if quality not in valid_qualities:
            return await ctx.send(f"❌ Invalid quality. Choose from: {', '.join(valid_qualities)}")
        
        await self.config.quality.set(quality)
        
        quality_info = {
            "LOW": "96kbps AAC",
            "HIGH": "320kbps AAC", 
            "LOSSLESS": "1411kbps FLAC",
            "HI_RES": "Up to 9216kbps FLAC",
            "MASTER": "MQA Master Quality"
        }
        
        await ctx.send(f"✅ Download quality set to: **{quality}** ({quality_info[quality]})")
    
    @tidalplay.command(name="fallback")
    async def toggle_fallback(self, ctx):
        """Toggle YouTube fallback if Tidal download fails"""
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
    
    @tidalplay.command(name="status")
    async def check_status(self, ctx):
        """Check Tidal configuration and tidal-dl status"""
        if not TIDALAPI_AVAILABLE:
            return await ctx.send("❌ tidalapi is not installed. Install with: `[p]pipinstall tidalapi`")
        
        quality = await self.config.quality()
        fallback = await self.config.fallback_to_youtube()
        tidal_dl_path = await self.config.tidal_dl_path()
        
        # Check if tidal-dl exists
        tidal_dl_status = "✅ Found" if tidal_dl_path and os.path.exists(tidal_dl_path) else "❌ Not found"
        
        embed = discord.Embed(
            title="Tidal Player Status",
            color=discord.Color.green() if self.session.check_login() else discord.Color.red()
        )
        
        # Authentication status
        auth_status = "✅ Authenticated" if self.session and self.session.check_login() else "❌ Not authenticated"
        embed.add_field(name="Tidal Authentication", value=auth_status, inline=False)
        
        # tidal-dl status
        embed.add_field(name="tidal-dl", value=tidal_dl_status, inline=True)
        embed.add_field(name="Quality", value=quality, inline=True)
        embed.add_field(name="YouTube Fallback", value="✅ On" if fallback else "❌ Off", inline=True)
        embed.add_field(name="Cached File", value="Yes" if self.last_played_file and os.path.exists(self.last_played_file) else "No", inline=True)
        
        if not self.session or not self.session.check_login():
            embed.set_footer(text="Run >tidalplay setup to authenticate")
        elif tidal_dl_status == "❌ Not found":
            embed.set_footer(text="Install tidal-dl: pip install tidal-dl")
        else:
            embed.set_footer(text="Use >tplay <song name> for Lossless Tidal playback")
        
        await ctx.send(embed=embed)
    
    def cog_unload(self):
        """Cleanup when cog is unloaded"""
        if self.last_played_file and os.path.exists(self.last_played_file):
            try:
                os.remove(self.last_played_file)
                log.info(f"Cleaned up last played file on unload: {self.last_played_file}")
            except:
                pass
        
        log.info("TidalPlayer cog unloaded")
