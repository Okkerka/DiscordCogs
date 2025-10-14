from redbot.core import commands, Config
import discord
import logging
import asyncio
import re
from typing import Dict, List

try:
    import tidalapi
    TIDALAPI_AVAILABLE = True
except ImportError:
    TIDALAPI_AVAILABLE = False

log = logging.getLogger("red.tidalplayer")

class TidalPlayer(commands.Cog):
    """Play music from Tidal in LOSSLESS quality with full metadata queue"""

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
        default_guild = {
            "track_metadata": []  # Stores metadata dicts for queued tracks
        }
        self.config.register_global(**default_global)
        self.config.register_guild(**default_guild)
        self.session = tidalapi.Session() if TIDALAPI_AVAILABLE else None

        if TIDALAPI_AVAILABLE:
            bot.loop.create_task(self._load_session())

    async def _load_session(self):
        """Load saved Tidal session from config."""
        await self.bot.wait_until_ready()
        try:
            creds = await self.config.all()
            if all(creds.get(k) for k in ("token_type", "access_token", "refresh_token")):
                self.session.load_oauth_session(
                    token_type=creds["token_type"],
                    access_token=creds["access_token"],
                    refresh_token=creds["refresh_token"],
                    expiry_time=creds.get("expiry_time")
                )
                log.info("Tidal session loaded" if self.session.check_login() else "Tidal session expired")
        except Exception as e:
            log.error(f"Failed loading session: {e}")

    async def _check_ready(self, ctx):
        """Ensure cog, session, and Audio are ready."""
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

    def _patch_ctx_send(self, ctx):
        """Suppress Audio cog 'Track Enqueued' embeds."""
        if not hasattr(ctx, "_original_send"):
            ctx._original_send = ctx.send
            async def send_override(*args, **kwargs):
                embed = kwargs.get("embed") or (args[0] if args and isinstance(args[0], discord.Embed) else None)
                if embed and getattr(embed, "title", "") and "Track Enqueued" in embed.title:
                    return
                return await ctx._original_send(*args, **kwargs)
            ctx.send = send_override

    def _restore_ctx_send(self, ctx):
        """Restore original ctx.send."""
        if hasattr(ctx, "_original_send"):
            ctx.send = ctx._original_send
            delattr(ctx, "_original_send")

    def _get_quality(self, track) -> str:
        """Map tidalapi quality to label."""
        if hasattr(track, "audio_quality"):
            return {
                "HI_RES": "HI_RES (MQA)",
                "LOSSLESS": "LOSSLESS (FLAC)",
                "HIGH": "HIGH (320kbps)",
                "LOW": "LOW (96kbps)"
            }.get(track.audio_quality, "LOSSLESS")
        return "LOSSLESS"

    def _extract_metadata(self, track) -> Dict:
        """Extract title, artist, album, duration, quality."""
        return {
            "title": getattr(track, "name", "Unknown"),
            "artist": getattr(track.artist, "name", "Unknown") if hasattr(track, "artist") and track.artist else "Unknown",
            "album": getattr(track.album, "name", "Unknown") if hasattr(track, "album") and track.album else "Unknown",
            "duration": int(getattr(track, "duration", 0)),
            "quality": self._get_quality(track)
        }

    async def _add_metadata(self, guild_id: int, meta: Dict):
        """Append metadata to the guild queue."""
        async with self.config.guild_from_id(guild_id).track_metadata() as q:
            q.append(meta)

    async def _remove_first_metadata(self, guild_id: int):
        """Pop the first metadata entry when a track ends."""
        async with self.config.guild_from_id(guild_id).track_metadata() as q:
            if q:
                q.pop(0)

    async def _clear_metadata(self, guild_id: int):
        """Clear all metadata (e.g., on stop)."""
        await self.config.guild_from_id(guild_id).track_metadata.set([])

    def _format_duration(self, seconds: int) -> str:
        """Convert seconds to 'MM:SS'."""
        m, s = divmod(seconds, 60)
        return f"{m:02d}:{s:02d}"

    def _create_embed(self, title: str, desc: str, quality: str) -> discord.Embed:
        """Build a Tidal info embed."""
        color = discord.Color.blue()
        if "HI_RES" in quality or "MQA" in quality:
            color = discord.Color.gold()
        embed = discord.Embed(title=title, description=desc, color=color)
        embed.add_field(name="Quality", value=quality, inline=True)
        return embed

    async def _play_track(self, ctx, track, show_embed: bool = True) -> bool:
        """Play a single tidalapi.Track and store its metadata."""
        try:
            meta = self._extract_metadata(track)
            await self._add_metadata(ctx.guild.id, meta)
            if show_embed:
                desc = f"**{meta['title']}**\nby {meta['artist']}"
                if meta["album"]:
                    desc += f" • *{meta['album']}*"
                await ctx.send(embed=self._create_embed("💎 Playing from Tidal", desc, meta["quality"]))
            url = await self.bot.loop.run_in_executor(None, track.get_url)
            if not url:
                return False
            audio = self.bot.get_cog("Audio")
            await audio.command_play(ctx, query=url)
            return True
        except Exception as e:
            log.error(f"Play track error: {e}")
            return False

    @commands.command(name="tplay")
    async def tidal_play(self, ctx, *, q: str):
        """
        Play or queue from Tidal.
        Supports search, playlist, album, track, or mix URLs.
        """
        if not await self._check_ready(ctx):
            return

        quiet = await self.config.quiet_mode()
        if quiet:
            self._patch_ctx_send(ctx)

        try:
            if "tidal.com" in q or "tidal.link" in q:
                await self._handle_url(ctx, q)
            else:
                await self._handle_search(ctx, q)
        finally:
            if quiet:
                self._restore_ctx_send(ctx)

    async def _handle_search(self, ctx, query: str):
        """Search Tidal and play the top result."""
        try:
            async with ctx.typing():
                res = await self.bot.loop.run_in_executor(None, self.session.search, query)
            tracks = res.get("tracks") or []
            if not tracks:
                return await ctx.send("❌ No tracks found.")
            await self._play_track(ctx, tracks[0])
        except Exception as e:
            log.error(f"Search error: {e}")
            await ctx.send(f"❌ Error: {e}")

    async def _handle_url(self, ctx, url: str):
        """Route Tidal URL types."""
        if "playlist/" in url:
            await self._queue_collection(ctx, url, "playlist")
        elif "album/" in url:
            await self._queue_collection(ctx, url, "album")
        elif "mix/" in url:
            await self._queue_collection(ctx, url, "mix")
        elif "track/" in url:
            await self._queue_single(ctx, url)
        else:
            await ctx.send("❌ Invalid Tidal URL")

    async def _queue_collection(self, ctx, url: str, kind: str):
        """Queue all tracks in playlist/album/mix."""
        pattern = {
            "playlist": r"playlist/([A-Za-z0-9\-]+)",
            "album": r"album/([0-9]+)",
            "mix": r"mix/([A-Za-z0-9]+)"
        }[kind]
        match = re.search(pattern, url)
        if not match:
            return await ctx.send(f"❌ Invalid {kind} URL")
        loader = getattr(self.session, kind)
        try:
            msg = await ctx.send(f"⏳ Loading {kind}...")
            obj = await self.bot.loop.run_in_executor(None, loader, match.group(1))
            pops = getattr(obj, "tracks", None) or getattr(obj, "items", None) or []
            pop_list = await self.bot.loop.run_in_executor(None, pops)
            total = len(pop_list)
            await msg.edit(content=f"⏳ Queueing **{getattr(obj, 'name', obj.title)}** ({total} tracks)...")
            for track in pop_list:
                await self._play_track(ctx, track, show_embed=False)
            await msg.edit(content=f"✅ Queued **{total}** tracks from **{getattr(obj, 'name', obj.title)}**")
        except Exception as e:
            log.error(f"Queue {kind} error: {e}")
            await ctx.send(f"❌ Error: {e}")

    async def _queue_single(self, ctx, url: str):
        """Queue a single track from URL."""
        match = re.search(r"track/([0-9]+)", url)
        if not match:
            return await ctx.send("❌ Invalid track URL")
        try:
            track = await self.bot.loop.run_in_executor(None, self.session.track, match.group(1))
            await self._play_track(ctx, track)
        except Exception as e:
            log.error(f"Single track error: {e}")
            await ctx.send(f"❌ Error: {e}")

    @commands.Cog.listener()
    async def on_player_stop(self, player):
        """When a track ends, remove its metadata."""
        try:
            guild = self.bot.get_guild(int(player.guild_id))
            if guild:
                await self._remove_first_metadata(guild.id)
        except Exception:
            pass

    @commands.command(name="tqueue", aliases=["q"])
    async def tqueue(self, ctx):
        """Show the Tidal queue with correct metadata."""
        data: List[Dict] = await self.config.guild(ctx.guild).track_metadata()
        if not data:
            return await ctx.send("The queue is empty.")
        lines = []
        for idx, m in enumerate(data, start=1):
            dur = self._format_duration(m["duration"])
            lines.append(f"`{idx}.` **{m['title']}** — {m['artist']} • `{dur}`")
        await ctx.send("\n".join(lines))

    @commands.is_owner()
    @commands.command()
    async def tidalsetup(self, ctx):
        """Authenticate with Tidal."""
        if not TIDALAPI_AVAILABLE:
            return await ctx.send("❌ Install: `[p]pipinstall tidalapi`")
        await ctx.send("Starting OAuth...")
        try:
            login, fut = self.session.login_oauth()
            embed = discord.Embed(
                title="Tidal OAuth Setup",
                description=f"Visit:\n{login.verification_uri_complete}",
                color=discord.Color.blue()
            )
            embed.set_footer(text="5 minute timeout")
            await ctx.send(embed=embed)
            await asyncio.wait_for(self.bot.loop.run_in_executor(None, fut.result), timeout=300)
            if self.session.check_login():
                await self.config.token_type.set(self.session.token_type)
                await self.config.access_token.set(self.session.access_token)
                await self.config.refresh_token.set(self.session.refresh_token)
                if hasattr(self.session, "expiry_time"):
                    await self.config.expiry_time.set(self.session.expiry_time.timestamp())
                await ctx.send("✅ Setup complete! Use `>tplay` to play.")
            else:
                await ctx.send("❌ Login failed.")
        except asyncio.TimeoutError:
            await ctx.send("⏱️ OAuth timed out.")
        except Exception as e:
            await ctx.send(f"❌ Error: {e}")

    @commands.is_owner()
    @commands.command()
    async def tidalquiet(self, ctx, mode: str = None):
        """Toggle quiet mode (suppresses 'Track Enqueued')."""
        if mode not in (None, "on", "off"):
            return await ctx.send("Usage: `>tidalquiet on/off`")
        if mode is None:
            status = "enabled" if await self.config.quiet_mode() else "disabled"
            return await ctx.send(f"Quiet mode is **{status}**.")
        await self.config.quiet_mode.set(mode == "on")
        await ctx.send(f"Quiet mode **{mode}**.")

def setup(bot):
    bot.add_cog(TidalPlayer(bot))
