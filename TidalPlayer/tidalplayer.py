from redbot.core import commands, Config
import discord
import logging
import asyncio
from typing import Optional, Dict

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
            "debug_mode": False
        }
        self.config.register_global(**default_global)
        self.session = tidalapi.Session() if TIDALAPI_AVAILABLE else None
        self.debug_enabled = False
        
        if TIDALAPI_AVAILABLE:
            bot.loop.create_task(self._load_session())
    
    def _debug(self, message: str):
        """Log debug messages if debug mode is enabled"""
        if self.debug_enabled:
            log.info(f"[DEBUG] {message}")
    
    async def _load_session(self):
        """Load saved Tidal session"""
        try:
            self.debug_enabled = await self.config.debug_mode()
            self._debug("Loading Tidal session...")
            
            token_type = await self.config.token_type()
            access_token = await self.config.access_token()
            refresh_token = await self.config.refresh_token()
            
            if all([token_type, access_token, refresh_token]):
                self.session.load_oauth_session(token_type, access_token, refresh_token)
                log.info("Tidal session loaded")
                self._debug(f"Session loaded")
            else:
                self._debug("No saved session found")
        except Exception as e:
            log.error(f"Session load failed: {e}")
            self._debug(f"Session load exception: {e}")
    
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
            "duration": getattr(track, 'duration', 0) * 1000  # Convert to milliseconds
        }
    
    async def _inject_metadata(self, ctx, metadata: Dict):
        """Try to inject metadata into Lavalink player"""
        try:
            await asyncio.sleep(1.5)  # Wait for track to load
            
            audio_cog = self.bot.get_cog("Audio")
            if not audio_cog:
                self._debug("Audio cog not found")
                return
            
            # Try to access the lavalink player
            try:
                if hasattr(audio_cog, 'lavalink'):
                    player = audio_cog.lavalink.player_manager.get(ctx.guild.id)
                    if player and player.current:
                        self._debug(f"Current track title: {player.current.title}")
                        
                        # Inject metadata if title is unknown
                        if "Unknown" in player.current.title or not player.current.title:
                            player.current.title = metadata['title']
                            player.current.author = metadata['artist']
                            player.current.length = metadata['duration']
                            self._debug(f"✅ Injected metadata: {metadata['title']} by {metadata['artist']}")
                        else:
                            self._debug(f"Track already has metadata: {player.current.title}")
                else:
                    self._debug("lavalink not found in Audio cog")
                    
            except AttributeError as e:
                self._debug(f"Could not access player: {e}")
                
        except Exception as e:
            self._debug(f"Metadata injection failed: {e}")
    
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """Suppress Audio cog 'Track Enqueued' messages when using tplay"""
        if message.author == self.bot.user and message.embeds:
            for embed in message.embeds:
                if embed.title and ("Track Enqueued" in embed.title or "Tracks Enqueued" in embed.title):
                    async for msg in message.channel.history(limit=10, before=message):
                        if ">tplay" in msg.content or "!tplay" in msg.content:
                            self._debug("Suppressing Audio cog enqueue message")
                            try:
                                await message.delete()
                            except:
                                pass
                            return
                    break
    
    @commands.command(name="tplay")
    async def tidal_play(self, ctx, *, query: str):
        """Play a song from Tidal in highest quality"""
        self.debug_enabled = await self.config.debug_mode()
        self._debug(f"Command: >tplay {query}")
        
        if not TIDALAPI_AVAILABLE:
            return await ctx.send("❌ Install tidalapi: `[p]pipinstall tidalapi`")
        
        if not self.session or not self.session.check_login():
            return await ctx.send("❌ Not authenticated. Run `>tidalplay setup`")
        
        if not self.bot.get_cog("Audio"):
            return await ctx.send("❌ Audio cog not loaded")
        
        async with ctx.typing():
            result = await self._get_track(query)
        
        if not result:
            return await ctx.send("❌ Track not found")
        
        self._debug(f"Playing: {result['info']['title']} by {result['info']['artist']}")
        
        # Send embed only if from Tidal
        if not result.get("is_fallback"):
            await ctx.send(embed=self._create_embed(result["info"]))
        
        # Play via Audio cog
        try:
            audio = self.bot.get_cog("Audio")
            await audio.command_play(ctx, query=result["url"])
            
            # Inject metadata after playing
            if not result.get("is_fallback"):
                self._debug("Scheduling metadata injection...")
                self.bot.loop.create_task(self._inject_metadata(ctx, result["info"]))
                
        except Exception as e:
            log.error(f"Play failed: {e}")
            await ctx.send(f"❌ Playback failed")
    
    async def _get_track(self, query: str) -> Optional[Dict]:
        """Search and get track from Tidal"""
        try:
            self._debug(f"Searching Tidal: {query}")
            
            results = await self.bot.loop.run_in_executor(None, self.session.search, query)
            
            if not results or not results.get('tracks'):
                self._debug("No tracks found")
                return await self._fallback(query)
            
            self._debug(f"Found {len(results['tracks'])} tracks")
            track = results['tracks'][0]
            
            metadata = self._extract_metadata(track)
            quality_str = self._get_quality(track)
            
            self._debug(f"Track: {metadata['title']} by {metadata['artist']} ({quality_str})")
            
            # Get stream URL
            stream_url = await self._stream(track)
            if stream_url:
                return {
                    "url": stream_url,
                    "info": {**metadata, "quality": quality_str},
                    "is_fallback": False
                }
            
            self._debug("Stream failed, using fallback")
            return await self._fallback(query, metadata)
            
        except Exception as e:
            log.error(f"Track retrieval failed: {e}")
            self._debug(f"Exception: {e}")
            return await self._fallback(query)
    
    async def _stream(self, track) -> Optional[str]:
        """Get stream URL"""
        try:
            self._debug("Getting stream URL...")
            url = await self.bot.loop.run_in_executor(None, track.get_url)
            if url:
                self._debug(f"Stream URL obtained: {url[:50]}...")
                return url
            self._debug("track.get_url() returned None")
        except Exception as e:
            self._debug(f"Stream failed: {e}")
        return None
    
    async def _fallback(self, query: str, metadata: Dict = None) -> Optional[Dict]:
        """YouTube fallback"""
        if not await self.config.fallback_to_youtube():
            return None
        
        if metadata:
            search = f"{metadata['artist']} {metadata['title']}"
            self._debug(f"YouTube fallback: {search}")
            return {
                "url": search,
                "info": {**metadata, "quality": "YouTube"},
                "is_fallback": True
            }
        
        self._debug(f"YouTube fallback: {query}")
        return {
            "url": query,
            "info": {"title": query, "artist": "Unknown", "album": "Unknown", "quality": "YouTube", "duration": 0},
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
        """Toggle debug mode"""
        current = await self.config.debug_mode()
        await self.config.debug_mode.set(not current)
        self.debug_enabled = not current
        
        status = "enabled" if not current else "disabled"
        await ctx.send(f"🐛 Debug mode **{status}**")
    
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
        fallback = await self.config.fallback_to_youtube()
        country = await self.config.country_code()
        debug_mode = await self.config.debug_mode()
        
        embed = discord.Embed(
            title="Tidal Player Status",
            color=discord.Color.green() if authenticated else discord.Color.red()
        )
        
        embed.add_field(name="Authentication", value="✅" if authenticated else "❌", inline=True)
        embed.add_field(name="YouTube Fallback", value="✅" if fallback else "❌", inline=True)
        embed.add_field(name="Country", value=country, inline=True)
        embed.add_field(name="Debug Mode", value="🐛" if debug_mode else "❌", inline=True)
        
        if not authenticated:
            embed.set_footer(text="Run >tidalplay setup")
        else:
            embed.set_footer(text="Streams highest quality | Metadata injection enabled")
        
        await ctx.send(embed=embed)
    
    def cog_unload(self):
        """Cleanup on unload"""
        log.info("TidalPlayer cog unloaded")
