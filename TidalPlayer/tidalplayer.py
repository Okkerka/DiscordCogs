from redbot.core import commands, Config
import discord
import logging
import asyncio
import aiohttp
from typing import Optional, Dict, Any

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
            "enabled": True,
            "quality": "HIGH",  # LOW, HIGH, LOSSLESS, HI_RES
            "fallback_to_youtube": True,
            "download_mode": False  # Download tracks before playing
        }
        self.config.register_global(**default_global)
        self.session = tidalapi.Session() if TIDALAPI_AVAILABLE else None
        self.download_cache = {}
        
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
    
    @commands.Cog.listener()
    async def on_command(self, ctx):
        """Intercept play commands and use Tidal"""
        if ctx.command.qualified_name != "play":
            return
        
        enabled = await self.config.enabled()
        if not enabled or not TIDALAPI_AVAILABLE:
            return
        
        # Get the query from command arguments
        if not ctx.args or len(ctx.args) < 3:
            return
        
        query = ctx.kwargs.get('query') or (ctx.args[2] if len(ctx.args) > 2 else None)
        
        # Skip if already a URL or empty
        if not query or any(x in str(query).lower() for x in ["http", "www.", ".com"]):
            return
        
        if not self.session or not self.session.check_login():
            log.warning("Tidal session not authenticated")
            return
        
        # Search Tidal and get playable URL
        async with ctx.typing():
            result = await self.get_tidal_playback(query, ctx)
        
        if result:
            new_query = result["url"]
            track_info = result.get("info", {})
            
            # Update the context with Tidal URL
            if 'query' in ctx.kwargs:
                ctx.kwargs['query'] = new_query
            elif len(ctx.args) > 2:
                ctx.args = list(ctx.args)
                ctx.args[2] = new_query
            
            # Send info message
            title = track_info.get("title", "Unknown")
            artist = track_info.get("artist", "Unknown")
            quality = track_info.get("quality", "Unknown")
            
            embed = discord.Embed(
                title="🎵 Playing from Tidal",
                description=f"**{title}**\nby {artist}",
                color=discord.Color.blue()
            )
            embed.add_field(name="Quality", value=quality, inline=True)
            embed.add_field(name="Source", value="Tidal Direct", inline=True)
            
            await ctx.send(embed=embed)
    
    async def get_tidal_playback(self, query: str, ctx) -> Optional[Dict[str, Any]]:
        """Get playable URL from Tidal (stream or downloaded file)"""
        try:
            # Search Tidal
            results = await self.bot.loop.run_in_executor(
                None,
                self.session.search,
                query
            )
            
            if not results or not results.get('tracks'):
                log.warning(f"No Tidal results for: {query}")
                return await self._fallback_search(query)
            
            track = results['tracks'][0]
            
            # Get quality setting
            quality_str = await self.config.quality()
            quality = self._get_quality_enum(quality_str)
            
            # Try to get stream URL
            download_mode = await self.config.download_mode()
            
            if download_mode:
                # Download the track first
                url = await self._download_track(track, quality, ctx)
            else:
                # Try direct streaming
                url = await self._get_stream_url(track, quality)
            
            if url:
                return {
                    "url": url,
                    "info": {
                        "title": track.name,
                        "artist": track.artist.name if track.artist else "Unknown",
                        "album": track.album.name if track.album else "Unknown",
                        "quality": quality_str
                    }
                }
            
            # If we couldn't get URL, try fallback
            log.warning(f"Could not get Tidal URL for track: {track.name}")
            return await self._fallback_search(query)
            
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
            log.error("track.get_url() not available - tidalapi version may not support it")
        except Exception as e:
            log.error(f"Failed to get stream URL: {e}")
        
        return None
    
    async def _download_track(self, track, quality, ctx) -> Optional[str]:
        """Download track and return local file path"""
        try:
            import tempfile
            import os
            from pathlib import Path
            
            # Check cache
            cache_key = f"{track.id}_{quality}"
            if cache_key in self.download_cache:
                if os.path.exists(self.download_cache[cache_key]):
                    log.info(f"Using cached track: {track.name}")
                    return self.download_cache[cache_key]
            
            await ctx.send("⏬ Downloading from Tidal... (this may take a moment)")
            
            # Create temp directory
            temp_dir = Path(tempfile.gettempdir()) / "tidalplayer"
            temp_dir.mkdir(exist_ok=True)
            
            # Sanitize filename
            safe_name = "".join(c for c in f"{track.artist.name} - {track.name}" if c.isalnum() or c in (' ', '-', '_')).strip()
            file_path = temp_dir / f"{safe_name}_{track.id}.m4a"
            
            # Try to download using tidalapi
            # Note: This may not work depending on tidalapi version
            def download():
                try:
                    # Some versions of tidalapi support this
                    if hasattr(track, 'download'):
                        track.download(str(file_path), quality=quality)
                        return True
                except:
                    pass
                return False
            
            success = await self.bot.loop.run_in_executor(None, download)
            
            if success and file_path.exists():
                self.download_cache[cache_key] = str(file_path)
                log.info(f"Successfully downloaded: {track.name}")
                return str(file_path)
            else:
                log.error("Download method not available or failed")
                
        except Exception as e:
            log.error(f"Failed to download track: {e}", exc_info=True)
        
        return None
    
    async def _fallback_search(self, query: str) -> Optional[Dict[str, Any]]:
        """Fallback to YouTube search if Tidal fails"""
        fallback_enabled = await self.config.fallback_to_youtube()
        
        if fallback_enabled:
            log.info(f"Using YouTube fallback for: {query}")
            return {
                "url": query,  # Let Audio cog search YouTube
                "info": {
                    "title": query,
                    "artist": "Unknown",
                    "quality": "YouTube"
                }
            }
        
        return None
    
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
    
    @tidalplay.command(name="setup")
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
                
                await ctx.send("✅ **Setup complete!** Tidal integration is now active.\nUse `>play <song name>` to play from Tidal.")
                log.info("OAuth setup completed successfully")
            else:
                await ctx.send("❌ Login failed. Please try again.")
        except Exception as e:
            await ctx.send(f"❌ Error during setup: {e}")
            log.error(f"OAuth error: {e}", exc_info=True)
    
    @tidalplay.command(name="toggle")
    async def toggle_integration(self, ctx):
        """Enable/disable Tidal integration with play command"""
        current = await self.config.enabled()
        await self.config.enabled.set(not current)
        
        if not current:
            await ctx.send("✅ Tidal integration **enabled** - `>play` will use Tidal")
        else:
            await ctx.send("❌ Tidal integration **disabled** - `>play` will use default sources")
    
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
            await ctx.send("✅ **Download mode enabled** - Tracks will be downloaded before playing")
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
    
    @tidalplay.command(name="clearcache")
    async def clear_cache(self, ctx):
        """Clear downloaded track cache"""
        import os
        
        count = 0
        for cache_key, file_path in list(self.download_cache.items()):
            try:
                if os.path.exists(file_path):
                    os.remove(file_path)
                    count += 1
                del self.download_cache[cache_key]
            except Exception as e:
                log.error(f"Failed to delete cache file: {e}")
        
        await ctx.send(f"✅ Cleared {count} cached tracks")
    
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
        
        enabled = await self.config.enabled()
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
        
        # Integration status
        integration_status = "✅ Enabled" if enabled else "❌ Disabled"
        embed.add_field(name="Integration", value=integration_status, inline=True)
        
        # Settings
        embed.add_field(name="Quality", value=quality, inline=True)
        embed.add_field(name="Country", value=country, inline=True)
        embed.add_field(name="Download Mode", value="✅ On" if download_mode else "❌ Off", inline=True)
        embed.add_field(name="YouTube Fallback", value="✅ On" if fallback else "❌ Off", inline=True)
        embed.add_field(name="Cached Tracks", value=str(len(self.download_cache)), inline=True)
        
        if not self.session or not self.session.check_login():
            embed.set_footer(text="Run >tidalplay setup to authenticate")
        
        await ctx.send(embed=embed)
    
    def cog_unload(self):
        """Cleanup when cog is unloaded"""
        # Clean up cached files
        import os
        for file_path in self.download_cache.values():
            try:
                if os.path.exists(file_path):
                    os.remove(file_path)
            except:
                pass
