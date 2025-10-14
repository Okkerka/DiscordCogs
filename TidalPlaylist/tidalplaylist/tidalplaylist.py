import discord
from redbot.core import commands, Config
import asyncio
import re
import logging

log = logging.getLogger("red.tidalplaylist")

try:
    import tidalapi
    TIDALAPI_AVAILABLE = True
except ImportError:
    TIDALAPI_AVAILABLE = False
    log.error("tidalapi not installed")

class TidalPlaylist(commands.Cog):
    """Play Tidal links via YouTube search (uses Lavalink). Suppresses spam when quiet mode is enabled."""

    def __init__(self, bot):
        self.bot = bot
        self.config = Config.get_conf(
            self,
            identifier=1234567890,
            force_registration=True
        )
        self.config.register_global(
            token_type=None,
            access_token=None,
            refresh_token=None,
            expiry_time=None,
            quiet_mode=True
        )

        if TIDALAPI_AVAILABLE:
            self.session = tidalapi.Session()
            bot.loop.create_task(self.load_session())
        else:
            self.session = None

    async def load_session(self):
        """Load saved Tidal session from config."""
        await self.bot.wait_until_ready()
        try:
            creds = await self.config.all()
            if all(creds.get(f) for f in ("token_type", "access_token", "refresh_token")):
                self.session.load_oauth_session(
                    token_type=creds["token_type"],
                    access_token=creds["access_token"],
                    refresh_token=creds["refresh_token"],
                    expiry_time=creds["expiry_time"]
                )
                log.info("Tidal session loaded" if self.session.check_login() else "Tidal session expired")
            else:
                log.info("No Tidal credentials found")
        except Exception as e:
            log.error(f"Error loading session: {e}")

    def _patch_ctx_send(self, ctx):
        """Suppress Track Enqueued in ctx.send for quiet mode."""
        if not hasattr(ctx, "_original_send"):
            ctx._original_send = ctx.send
            async def send_override(*args, **kwargs):
                e = kwargs.get("embed") or (args[0] if args and isinstance(args[0], discord.Embed) else None)
                if e and getattr(e, "title", "").lower().find("track enqueued") >= 0:
                    return
                return await ctx._original_send(*args, **kwargs)
            ctx.send = send_override

    def _restore_ctx_send(self, ctx):
        """Restore ctx.send after suppression patch."""
        if hasattr(ctx, "_original_send"):
            ctx.send = ctx._original_send
            delattr(ctx, "_original_send")

    async def _check_ready(self, ctx):
        """Ensure cog and user are ready to queue."""
        if not TIDALAPI_AVAILABLE:
            await ctx.send("❌ tidalapi not installed. Run: `[p]pipinstall tidalapi`")
            return False
        if not self.session or not self.session.check_login():
            await ctx.send("❌ Not authenticated. Owner needs to run `[p]tidalsetup`")
            return False
        if not self.bot.get_cog("Audio"):
            await ctx.send("❌ Audio cog not loaded")
            return False
        if not ctx.author.voice:
            await ctx.send("❌ Join a voice channel first")
            return False
        return True

    @commands.is_owner()
    @commands.command()
    async def tidalsetup(self, ctx):
        """Set up Tidal OAuth authentication."""
        if not TIDALAPI_AVAILABLE:
            return await ctx.send("❌ tidalapi is not installed. Install with: `[p]pipinstall tidalapi`")
        await ctx.send("Starting OAuth setup...")
        try:
            login, future = self.session.login_oauth()
            embed = discord.Embed(
                title="Tidal OAuth Setup",
                description=f"Visit this URL:\n{login.verification_uri_complete}",
                color=discord.Color.blue()
            )
            embed.add_field(name="Waiting", value="Timeout in 5 minutes", inline=False)
            await ctx.send(embed=embed)
            try:
                await asyncio.wait_for(
                    self.bot.loop.run_in_executor(None, future.result),
                    timeout=300
                )
            except asyncio.TimeoutError:
                return await ctx.send("⏱️ OAuth timed out")
            if self.session.check_login():
                await self.config.token_type.set(self.session.token_type)
                await self.config.access_token.set(self.session.access_token)
                await self.config.refresh_token.set(self.session.refresh_token)
                if hasattr(self.session, "expiry_time") and self.session.expiry_time:
                    await self.config.expiry_time.set(self.session.expiry_time.timestamp())
                await ctx.send("✅ Setup complete!")
                log.info("OAuth setup completed")
            else:
                await ctx.send("❌ Login failed")
        except Exception as e:
            await ctx.send(f"❌ Error: {e}")
            log.error(f"OAuth error: {e}")

    @commands.is_owner()
    @commands.command()
    async def tidalquiet(self, ctx, mode: str = None):
        """
        Enable or disable 'quiet mode' (suppresses spam and disables Audio's 'Track Enqueued' messages).
        Usage: [p]tidalquiet on/off | [p]tidalquiet (show status)
        """
        if mode is None:
            status = "enabled" if (await self.config.quiet_mode()) else "disabled"
            await ctx.send(f"Quiet mode is **{status}**.\nUsage: `[p]tidalquiet on/off`")
            return
        mode = mode.lower()
        if mode not in ("on", "off"):
            await ctx.send("Usage: `[p]tidalquiet on` or `[p]tidalquiet off`")
            return
        await self.config.quiet_mode.set(mode == "on")
        status = "enabled" if mode == "on" else "disabled"
        await ctx.send(f"Quiet mode **{status}**.")

    @commands.command()
    async def tidal(self, ctx, url: str):
        """
        Queue Tidal playlist, album, track, or mix. Example:
        [p]tidal https://tidal.com/browse/playlist/xxxxx
        [p]tidal https://tidal.com/browse/album/xxxxx
        [p]tidal https://tidal.com/browse/track/xxxxx
        [p]tidal https://tidal.com/browse/mix/xxxxx
        """
        if not await self._check_ready(ctx):
            return
        quiet_enabled = await self.config.quiet_mode()
        if quiet_enabled:
            self._patch_ctx_send(ctx)
        try:
            if "mix/" in url:
                await self.queue_mix(ctx, url)
            elif "playlist/" in url:
                await self.queue_playlist(ctx, url)
            elif "album/" in url:
                await self.queue_album(ctx, url)
            elif "track/" in url:
                await self.queue_track(ctx, url)
            else:
                await ctx.send("❌ Invalid Tidal URL (supports: playlist, album, track, mix)")
        finally:
            if quiet_enabled:
                self._restore_ctx_send(ctx)

    async def queue_playlist(self, ctx, url):
        """Queue a Tidal playlist via YouTube search."""
        match = re.search(r"playlist/([A-Za-z0-9\-]+)", url)
        if not match:
            await ctx.send("❌ Invalid playlist URL")
            return
        playlist_id = match.group(1)
        quiet = await self.config.quiet_mode()
        try:
            loading_msg = await ctx.send("⏳ Loading Tidal playlist...")
            playlist = await self.bot.loop.run_in_executor(None, self.session.playlist, playlist_id)
            tracks = await self.bot.loop.run_in_executor(None, playlist.tracks)
            total = len(tracks)
            if not quiet:
                await loading_msg.edit(content=f"⏳ Queueing **{playlist.name}** ({total} tracks)...")
            queued, failed = 0, 0
            for i, track in enumerate(tracks, 1):
                try:
                    if await self.add_track(ctx, track):
                        queued += 1
                    else:
                        failed += 1
                    if not quiet and i % 10 == 0:
                        await loading_msg.edit(content=f"⏳ Queueing... {i}/{total} tracks (use `[p]stop` to cancel)")
                except Exception as e:
                    log.error(f"Error queuing track: {e}")
                    failed += 1
            result = f"✅ Queued **{queued}/{total}** tracks from **{playlist.name}**"
            if failed:
                result += f"\n⚠️ {failed} tracks failed"
            await loading_msg.edit(content=result)
        except Exception as e:
            await ctx.send(f"❌ Error: {e}")
            log.error(f"Playlist error: {e}")

    async def queue_album(self, ctx, url):
        """Queue an album via YouTube search."""
        match = re.search(r"album/([0-9]+)", url)
        if not match:
            await ctx.send("❌ Invalid album URL")
            return
        album_id = match.group(1)
        quiet = await self.config.quiet_mode()
        try:
            loading_msg = await ctx.send("⏳ Loading Tidal album...")
            album = await self.bot.loop.run_in_executor(None, self.session.album, album_id)
            tracks = await self.bot.loop.run_in_executor(None, album.tracks)
            total = len(tracks)
            if not quiet:
                await loading_msg.edit(content=f"⏳ Queueing **{album.name}** by {album.artist.name} ({total} tracks)...")
            queued, failed = 0, 0
            for i, track in enumerate(tracks, 1):
                try:
                    if await self.add_track(ctx, track):
                        queued += 1
                    else:
                        failed += 1
                except Exception as e:
                    log.error(f"Error queuing track: {e}")
                    failed += 1
            result = f"✅ Queued **{queued}/{total}** tracks from **{album.name}**"
            if failed:
                result += f"\n⚠️ {failed} tracks failed"
            await loading_msg.edit(content=result)
        except Exception as e:
            await ctx.send(f"❌ Error: {e}")
            log.error(f"Album error: {e}")

    async def queue_track(self, ctx, url):
        """Queue a single track via YouTube search."""
        match = re.search(r"track/([0-9]+)", url)
        if not match:
            await ctx.send("❌ Invalid track URL")
            return
        track_id = match.group(1)
        try:
            track = await self.bot.loop.run_in_executor(None, self.session.track, track_id)
            if await self.add_track(ctx, track):
                await ctx.send(f"✅ Queued: **{track.name}** by {track.artist.name}")
            else:
                await ctx.send(f"❌ Failed to queue: **{track.name}**")
        except Exception as e:
            await ctx.send(f"❌ Error: {e}")
            log.error(f"Track error: {e}")

    async def queue_mix(self, ctx, url):
        """Queue a Tidal Mix via YouTube search."""
        match = re.search(r"mix/([A-Za-z0-9]+)", url)
        if not match:
            await ctx.send("❌ Invalid mix URL")
            return
        mix_id = match.group(1)
        quiet = await self.config.quiet_mode()
        try:
            loading_msg = await ctx.send("⏳ Loading Tidal mix...")
            mix = await self.bot.loop.run_in_executor(None, self.session.mix, mix_id)
            items = await self.bot.loop.run_in_executor(None, mix.items)
            total = len(items)
            if not quiet:
                await loading_msg.edit(content=f"⏳ Queueing **{mix.title}** ({total} tracks)...")
            queued, failed = 0, 0
            for i, item in enumerate(items, 1):
                try:
                    if await self.add_track(ctx, item):
                        queued += 1
                    else:
                        failed += 1
                    if not quiet and i % 10 == 0:
                        await loading_msg.edit(content=f"⏳ Queueing... {i}/{total} tracks (use `[p]stop` to cancel)")
                except Exception as e:
                    log.error(f"Error queuing track: {e}")
                    failed += 1
            result = f"✅ Queued **{queued}/{total}** tracks from **{mix.title}**"
            if failed:
                result += f"\n⚠️ {failed} tracks failed"
            await loading_msg.edit(content=result)
        except Exception as e:
            await ctx.send(f"❌ Error: {e}")
            log.error(f"Mix error: {e}")

    async def add_track(self, ctx, track):
        """Add track to queue via YouTube search using Audio's play."""
        try:
            query = f"{track.artist.name} - {track.name}"
            play_command = self.bot.get_command("play")
            if not play_command:
                log.error("Play command not found")
                return False
            await ctx.invoke(play_command, query=query)
            return True
        except Exception as e:
            log.error(f"Error adding track: {e}")
            return False

async def setup(bot):
    """Setup function for Red-DiscordBot."""
    cog = TidalPlaylist(bot)
    await bot.add_cog(cog)
