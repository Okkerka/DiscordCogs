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

try:
    import spotipy
    from spotipy.oauth2 import SpotifyClientCredentials
    SPOTIFY_AVAILABLE = True
except ImportError:
    SPOTIFY_AVAILABLE = False

try:
    from googleapiclient.discovery import build
    YOUTUBE_API_AVAILABLE = True
except ImportError:
    YOUTUBE_API_AVAILABLE = False

log = logging.getLogger("red.tidalplayer")


class TidalPlayer(commands.Cog):
    """Play music from Tidal, Spotify, or YouTube via Tidal search (LOSSLESS)."""

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
            spotify_client_id=None,
            spotify_client_secret=None,
            youtube_api_key=None,
            quiet_mode=True,
        )
        self.config.register_guild(track_metadata=[])
        self.session = tidalapi.Session() if TIDALAPI_AVAILABLE else None
        self.sp = None
        self.yt = None
        bot.loop.create_task(self._initialize_apis())

    async def _initialize_apis(self):
        await self.bot.wait_until_ready()
        creds = await self.config.all()
        # Tidal OAuth session
        if TIDALAPI_AVAILABLE and all(creds.get(k) for k in ("token_type", "access_token", "refresh_token")):
            try:
                self.session.load_oauth_session(
                    creds["token_type"], creds["access_token"],
                    creds["refresh_token"], creds.get("expiry_time")
                )
                log.info("Tidal session loaded")
            except Exception as e:
                log.error(f"Tidal session load failed: {e}")
        # Spotify client
        if SPOTIFY_AVAILABLE and creds.get("spotify_client_id") and creds.get("spotify_client_secret"):
            try:
                self.sp = spotipy.Spotify(
                    client_credentials_manager=SpotifyClientCredentials(
                        creds["spotify_client_id"], creds["spotify_client_secret"]
                    )
                )
                log.info("Spotify client initialized")
            except Exception as e:
                log.error(f"Spotify init failed: {e}")
        # YouTube client
        if YOUTUBE_API_AVAILABLE and creds.get("youtube_api_key"):
            try:
                self.yt = build("youtube", "v3", developerKey=creds["youtube_api_key"])
                log.info("YouTube client initialized")
            except Exception as e:
                log.error(f"YouTube init failed: {e}")

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
            "artist": track.artist.name if getattr(track, "artist", None) else "Unknown",
            "album": track.album.name if getattr(track, "album", None) else None,
            "duration": int(getattr(track, "duration", 0) or 0),
            "quality": getattr(track, "audio_quality", "LOSSLESS")
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

    async def _play(self, ctx, track, show_embed=True):
        meta = self._extract_meta(track)
        await self._add_meta(ctx.guild.id, meta)
        emoji, label = self._get_quality_label(meta["quality"])
        if show_embed:
            desc = f"**{meta['title']}** • {meta['artist']}"
            if meta["album"]:
                desc += f"\n_{meta['album']}_"
            embed = discord.Embed(
                title=f"{emoji} Playing from Tidal",
                description=desc,
                color=discord.Color.blue()
            )
            embed.add_field(name="Quality", value=label, inline=True)
            embed.set_footer(text=f"Duration: {self._format_time(meta['duration'])}")
            await ctx.send(embed=embed)
        url = await self.bot.loop.run_in_executor(None, track.get_url)
        if not url:
            return False
        await self.bot.get_cog("Audio").command_play(ctx, query=url)
        return True

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

    def _suppress_enqueued(self, ctx):
        if hasattr(ctx, "_orig_send"):
            return
        ctx._orig_send = ctx.send

        async def send_override(*a, **k):
            e = k.get("embed") or (a[0] if a and isinstance(a[0], discord.Embed) else None)
            if e and "Track Enqueued" in getattr(e, "title", ""):
                return
            return await ctx._orig_send(*a, **k)

        ctx.send = send_override

    def _restore_send(self, ctx):
        if hasattr(ctx, "_orig_send"):
            ctx.send = ctx._orig_send
            delattr(ctx, "_orig_send")

    @commands.command(name="tplay")
    async def tplay(self, ctx, *, q: str):
        """
        Single command to search/play Tidal, Spotify, or YouTube playlists via Tidal search.
        """
        if not await self._check_ready(ctx):
            return
        quiet = await self.config.quiet_mode()
        if quiet:
            self._suppress_enqueued(ctx)
        try:
            if SPOTIFY_AVAILABLE and "spotify.com/playlist/" in q:
                await self._queue_spotify_playlist(ctx, q)
            elif YOUTUBE_API_AVAILABLE and ("list=" in q and ("youtube.com" in q or "youtu.be" in q)):
                await self._queue_youtube_playlist(ctx, q)
            elif "tidal.com" in q or re.search(r"(playlist|album|track|mix)/", q):
                await self._handle_tidal_url(ctx, q)
            else:
                await self._search_and_play(ctx, q)
        finally:
            if quiet:
                self._restore_send(ctx)

    async def _search_and_play(self, ctx, query: str):
        async with ctx.typing():
            res = await self.bot.loop.run_in_executor(None, self.session.search, query)
        tracks = res.get("tracks", [])
        if not tracks:
            return await ctx.send("❌ No tracks found.")
        await self._play(ctx, tracks[0])

    async def _handle_tidal_url(self, ctx, url: str):
        kind = ("playlist" if "playlist/" in url else
                "album" if "album/" in url else
                "mix" if "mix/" in url else
                "track")
        match = re.search(rf"{kind}/([A-Za-z0-9\-]+)", url)
        if not match:
            return await ctx.send(f"❌ Invalid Tidal {kind} URL")
        loader = getattr(self.session, kind)
        obj = await self.bot.loop.run_in_executor(None, loader, match.group(1))
        items = await self.bot.loop.run_in_executor(None, getattr(obj, "tracks", getattr(obj, "items")))
        name = getattr(obj, "name", getattr(obj, "title", ""))
        msg = await ctx.send(f"⏳ Queueing Tidal {kind} '{name}' ({len(items)} tracks)…")
        for t in items:
            await self._play(ctx, t, show_embed=False)
        await msg.edit(content=f"✅ Queued {len(items)} tracks from **{name}**")

    async def _queue_spotify_playlist(self, ctx, url: str):
        match = re.search(r"playlist/([A-Za-z0-9]+)", url)
        if not match or not self.sp:
            return await ctx.send("❌ Invalid Spotify URL or not configured")
        pid = match.group(1)
        items = self.sp.playlist_items(pid)["items"]
        await ctx.send(f"⏳ Queuing Spotify playlist ({len(items)} tracks)…")
        for item in items:
            tr = item["track"]
            query = f"{tr['artists'][0]['name']} {tr['name']}"
            await self._search_and_play(ctx, query)
        await ctx.send("✅ Done queueing Spotify playlist")

    async def _queue_youtube_playlist(self, ctx, url: str):
        match = re.search(r"list=([A-Za-z0-9_-]+)", url)
        if not match or not self.yt:
            return await ctx.send("❌ Invalid YouTube URL or not configured")
        pid = match.group(1)
        videos = []
        req = self.yt.playlistItems().list(part="snippet", playlistId=pid, maxResults=50)
        while req:
            res = req.execute()
            videos += [item["snippet"]["title"] for item in res["items"]]
            req = self.yt.playlistItems().list_next(req, res)
        await ctx.send(f"⏳ Queuing YouTube playlist ({len(videos)} videos)…")
        for title in videos:
            await self._search_and_play(ctx, title)
        await ctx.send("✅ Done queueing YouTube playlist")

    @commands.Cog.listener()
    async def on_player_stop(self, player):
        guild = self.bot.get_guild(int(player.guild_id))
        if guild:
            await self._pop_meta(guild.id)

    @commands.command(name="tqueue", aliases=["q"])
    async def tqueue(self, ctx):
        """Paginated display of the Tidal queue with metadata."""
        data: List[Dict] = await self.config.guild(ctx.guild).track_metadata()
        if not data:
            return await ctx.send("The queue is empty.")
        embeds = []
        for i in range(0, len(data), 10):
            chunk = data[i:i+10]
            desc = "\n".join(
                f"`{i+j+1}.` **{m['title']}** • {m['artist']} • `{self._format_time(m['duration'])}`"
                for j, m in enumerate(chunk)
            )
            embeds.append(discord.Embed(
                title="Tidal Queue",
                description=desc,
                color=discord.Color.green()
            ))
        await menu(ctx, embeds, DEFAULT_CONTROLS)

    @commands.command(name="tidalsetup")
    @commands.is_owner()
    async def tidalsetup(self, ctx):
        """Setup Tidal OAuth."""
        if not TIDALAPI_AVAILABLE:
            return await ctx.send("❌ Install tidalapi: `[p]pipinstall tidalapi`")
        login, fut = self.session.login_oauth()
        embed = discord.Embed(
            title="Tidal OAuth Setup",
            description=f"Visit:\n{login.verification_uri_complete}",
            color=discord.Color.blue()
        )
        embed.set_footer(text="Expires in 5 minutes")
        await ctx.send(embed=embed)
        try:
            await asyncio.wait_for(self.bot.loop.run_in_executor(None, fut.result), timeout=300)
        except asyncio.TimeoutError:
            return await ctx.send("⏱️ OAuth timed out.")
        if self.session.check_login():
            await self.config.token_type.set(self.session.token_type)
            await self.config.access_token.set(self.session.access_token)
            await self.config.refresh_token.set(self.session.refresh_token)
            if hasattr(self.session, "expiry_time"):
                await self.config.expiry_time.set(self.session.expiry_time.timestamp())
            await ctx.send("✅ Tidal setup complete!")
        else:
            await ctx.send("❌ Login failed.")

    @commands.group(name="tidalplay", invoke_without_command=True)
    @commands.is_owner()
    async def tidalplay(self, ctx):
        """Group for playlist-source setup (Spotify/YouTube)."""
        await ctx.send_help()

    @tidalplay.command(name="spotify")
    @commands.is_owner()
    async def spotify_setup(self, ctx, client_id: str, client_secret: str):
        """Store Spotify API credentials."""
        if not SPOTIFY_AVAILABLE:
            return await ctx.send("❌ Install spotipy: `[p]pipinstall spotipy`")
        await self.config.spotify_client_id.set(client_id)
        await self.config.spotify_client_secret.set(client_secret)
        await ctx.send("✅ Spotify credentials saved.")

    @tidalplay.command(name="youtube")
    @commands.is_owner()
    async def youtube_setup(self, ctx, api_key: str):
        """Store YouTube Data API key."""
        if not YOUTUBE_API_AVAILABLE:
            return await ctx.send("❌ Install google-api-python-client")
        await self.config.youtube_api_key.set(api_key)
        await ctx.send("✅ YouTube API key saved.")

    @commands.command(name="tidalquiet")
    @commands.is_owner()
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
