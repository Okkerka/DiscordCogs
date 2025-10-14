from redbot.core import commands, Config
from redbot.core.utils.menus import menu, DEFAULT_CONTROLS
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
    """Play music from Tidal in LOSSLESS quality with accurate queue metadata."""

    def __init__(self, bot):
        self.bot = bot
        self.config = Config.get_conf(
            self, identifier=1234567890, force_registration=True
        )
        self.config.register_global(
            token_type=None,
            access_token=None,
            refresh_token=None,
            expiry_time=None,
            quiet_mode=True
        )
        self.config.register_guild(track_metadata=[])
        self.session = tidalapi.Session() if TIDALAPI_AVAILABLE else None
        if TIDALAPI_AVAILABLE:
            bot.loop.create_task(self._load_session())

    async def _load_session(self):
        await self.bot.wait_until_ready()
        creds = await self.config.all()
        if all(creds.get(k) for k in ("token_type","access_token","refresh_token")):
            try:
                self.session.load_oauth_session(
                    creds["token_type"], creds["access_token"],
                    creds["refresh_token"], creds.get("expiry_time")
                )
                log.info("Tidal session loaded")
            except Exception as e:
                log.error(f"Session load failed: {e}")

    async def _check_ready(self, ctx):
        if not TIDALAPI_AVAILABLE:
            await ctx.send("❌ Install tidalapi: `[p]pipinstall tidalapi`")
            return False
        if not self.session or not self.session.check_login():
            await ctx.send("❌ Not authenticated. Run `>tidalsetup`")
            return False
        if not self.bot.get_cog("Audio"):
            await ctx.send("❌ Audio cog not loaded")
            return False
        return True

    def _get_quality_label(self, q):
        return {
            "HI_RES": ("💠", "HI_RES (MQA)"),
            "LOSSLESS": ("🎵", "LOSSLESS (FLAC)"),
            "HIGH": ("🎶", "HIGH (320kbps)"),
            "LOW": ("🔈", "LOW (96kbps)")
        }.get(q, ("🎵", "LOSSLESS"))

    def _extract_meta(self, track) -> Dict:
        return {
            "title": track.name or "Unknown",
            "artist": track.artist.name if track.artist else "Unknown",
            "album": track.album.name if track.album else None,
            "duration": int(track.duration or 0),
            "quality": track.audio_quality if hasattr(track, "audio_quality") else "LOSSLESS"
        }

    async def _add_meta(self, guild_id, meta):
        async with self.config.guild_from_id(guild_id).track_metadata() as q:
            q.append(meta)

    async def _pop_meta(self, guild_id):
        async with self.config.guild_from_id(guild_id).track_metadata() as q:
            if q:
                q.pop(0)

    def _format_time(self, sec):
        m, s = divmod(sec, 60)
        return f"{m:02d}:{s:02d}"

    def _build_embed(self, title, desc, color):
        return discord.Embed(title=title, description=desc, color=color)

    async def _play(self, ctx, track, show_embed=True):
        meta = self._extract_meta(track)
        await self._add_meta(ctx.guild.id, meta)
        emoji, label = self._get_quality_label(meta["quality"])
        if show_embed:
            desc = f"**{meta['title']}** • {meta['artist']}"
            if meta["album"]:
                desc += f"\n_{meta['album']}_"
            embed = self._build_embed(f"{emoji} Playing from Tidal", desc, discord.Color.blue())
            embed.add_field(name="Quality", value=label, inline=True)
            embed.set_footer(text=f"Duration: {self._format_time(meta['duration'])}")
            await ctx.send(embed=embed)
        url = await self.bot.loop.run_in_executor(None, track.get_url)
        if not url:
            return False
        await self.bot.get_cog("Audio").command_play(ctx, query=url)
        return True

    def _suppress_enqueued(self, ctx):
        if hasattr(ctx, "_orig_send"):
            return
        ctx._orig_send = ctx.send
        async def send_override(*args, **kwargs):
            embed = kwargs.get("embed") or (args[0] if args and isinstance(args[0], discord.Embed) else None)
            if embed and "Track Enqueued" in getattr(embed, "title", ""):
                return
            return await ctx._orig_send(*args, **kwargs)
        ctx.send = send_override

    def _restore_send(self, ctx):
        if hasattr(ctx, "_orig_send"):
            ctx.send = ctx._orig_send
            delattr(ctx, "_orig_send")

    @commands.command(name="tplay")
    async def tplay(self, ctx, *, q: str):
        """Search, play, or queue from Tidal."""
        if not await self._check_ready(ctx):
            return
        quiet = await self.config.quiet_mode()
        if quiet:
            self._suppress_enqueued(ctx)
        try:
            if re.search(r"(playlist|album|track|mix)/", q):
                await self._handle_url(ctx, q)
            else:
                await self._search_and_play(ctx, q)
        finally:
            if quiet:
                self._restore_send(ctx)

    async def _search_and_play(self, ctx, query):
        async with ctx.typing():
            res = await self.bot.loop.run_in_executor(None, self.session.search, query)
        tracks = res.get("tracks", [])
        if not tracks:
            return await ctx.send("❌ No tracks found.")
        await self._play(ctx, tracks[0])

    async def _handle_url(self, ctx, url):
        kind = ("playlist" if "playlist/" in url else
                "album" if "album/" in url else
                "mix" if "mix/" in url else
                "track")
        match = re.search(rf"{kind}/([A-Za-z0-9\-]+)", url)
        if not match:
            return await ctx.send(f"❌ Invalid {kind} URL")
        obj = await self.bot.loop.run_in_executor(None, getattr(self.session, kind), match.group(1))
        items = await self.bot.loop.run_in_executor(None, getattr(obj, "tracks", getattr(obj, "items")))
        name = getattr(obj, "name", getattr(obj, "title", ""))
        msg = await ctx.send(f"⏳ Queueing {kind} '{name}' ({len(items)} tracks)...")
        for t in items:
            await self._play(ctx, t, show_embed=False)
        await msg.edit(content=f"✅ Queued {len(items)} tracks from **{name}**")

    @commands.Cog.listener()
    async def on_player_stop(self, player):
        guild = self.bot.get_guild(int(player.guild_id))
        if guild:
            await self._pop_meta(guild.id)

    @commands.command(name="tqueue", aliases=["q"])
    async def tqueue(self, ctx):
        """Show the Tidal queue with correct metadata, paginated."""
        data: List[Dict] = await self.config.guild(ctx.guild).track_metadata()
        if not data:
            return await ctx.send("The queue is empty.")
        embeds = []
        for i in range(0, len(data), 10):
            chunk = data[i:i+10]
            desc = "\n".join(
                f"`{j+1+i}.` **{m['title']}** • {m['artist']} • `{self._format_time(m['duration'])}`"
                for j, m in enumerate(chunk)
            )
            embeds.append(discord.Embed(title="Tidal Queue", description=desc, color=discord.Color.green()))
        await menu(ctx, embeds, DEFAULT_CONTROLS)

    @commands.is_owner()
    @commands.command()
    async def tidalsetup(self, ctx):
        """Authenticate with Tidal."""
        if not TIDALAPI_AVAILABLE:
            return await ctx.send("❌ Install tidalapi: `[p]pipinstall tidalapi`")
        login, fut = self.session.login_oauth()
        embed = discord.Embed(
            title="Tidal OAuth Setup",
            description=f"Visit:\n{login.verification_uri_complete}",
            color=discord.Color.blue()
        )
        embed.set_footer(text="Expires in 5m")
        await ctx.send(embed=embed)
        try:
            await asyncio.wait_for(self.bot.loop.run_in_executor(None, fut.result), timeout=300)
        except asyncio.TimeoutError:
            return await ctx.send("⏱️ OAuth timed out.")
        if self.session.check_login():
            creds = {
                "token_type": self.session.token_type,
                "access_token": self.session.access_token,
                "refresh_token": self.session.refresh_token,
                "expiry_time": getattr(self.session, "expiry_time", None)
            }
            for k, v in creds.items():
                await getattr(self.config, k).set(v)
            await ctx.send("✅ Tidal setup complete!")
        else:
            await ctx.send("❌ Login failed.")

    @commands.is_owner()
    @commands.command()
    async def tidalquiet(self, ctx, mode: str = None):
        """Toggle quiet mode for enqueue suppression."""
        if mode not in (None, "on", "off"):
            return await ctx.send("Usage: `>tidalquiet on/off`")
        if mode is None:
            status = "enabled" if await self.config.quiet_mode() else "disabled"
            return await ctx.send(f"Quiet mode is **{status}**.")
        await self.config.quiet_mode.set(mode == "on")
        await ctx.send(f"Quiet mode **{mode}** toggled.")


def setup(bot):
    bot.add_cog(TidalPlayer(bot))
