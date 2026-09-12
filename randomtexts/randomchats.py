import asyncio
import copy
import html
import json
import logging
import random
import re
import time
import xml.etree.ElementTree as ET
from collections import defaultdict, deque
from collections.abc import Callable
from typing import Literal

import aiohttp
import discord
from redbot.core import Config, commands
from redbot.core.bot import Red

log = logging.getLogger("red.randomtexts")
FETCH_DEADLINE = 10
CATEGORIES = ("brainrot", "showerthought", "dadjoke", "fact")
Category = Literal["brainrot", "showerthought", "dadjoke", "fact"]
DEFAULTS = {
    "enabled": False,
    "counter": 0,
    "target": 50,
    "channels": [],
    "categories": list(CATEGORIES),
    "frequency_min": 10,
    "frequency_max": 100,
}


class RandomText(commands.Cog):
    """Randomly sends Brainrot, Showerthoughts, Jokes, and Facts in chat."""

    def __init__(self, bot: Red) -> None:
        self.bot = bot
        self.session: aiohttp.ClientSession | None = None
        self.headers = {"User-Agent": "Red-RandomTexts/2.0"}

        # Server-wide config
        self.config = Config.get_conf(
            self, identifier=98429482394, force_registration=True
        )
        self.config.register_guild(**DEFAULTS)
        self.cache = {category: deque(maxlen=20) for category in CATEGORIES}
        self._settings: dict[int, dict] = {}
        self._locks = defaultdict(asyncio.Lock)
        self._load_locks = defaultdict(asyncio.Lock)
        self._busy: set[int] = set()
        self._network_slots = asyncio.Semaphore(4)
        self._rss_cache: dict[str, tuple[float, ET.Element]] = {}
        self._views: set = set()
        self._closed = False

    async def cog_load(self) -> None:
        self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8))

    async def cog_unload(self) -> None:
        self._closed = True
        for view in list(self._views):
            view.stop()
        self._views.clear()
        if self.session is not None:
            await self.session.close()

    async def get_settings(self, guild_id: int) -> dict:
        """Read a detached settings snapshot; mutations use update_settings."""
        if guild_id not in self._settings:
            async with self._load_locks[guild_id]:
                if guild_id not in self._settings:
                    self._settings[guild_id] = await self.config.guild_from_id(
                        guild_id
                    ).all()
        return copy.deepcopy(self._settings[guild_id])

    async def update_settings(self, guild_id: int, **changes) -> None:
        """Validate and persist a settings change under the counter lock."""
        await self._edit_settings(guild_id, lambda settings: settings.update(changes))

    async def _edit_settings(self, guild_id: int, edit: Callable[[dict], None]) -> dict:
        """Apply a read-modify-write operation to the latest locked snapshot."""
        async with self._locks[guild_id]:
            settings = await self.get_settings(guild_id)
            edit(settings)
            if not 1 <= settings["frequency_min"] <= settings["frequency_max"] <= 10000:
                raise ValueError(
                    "Frequency must be between 1 and 10,000 messages, minimum first."
                )
            if not settings["categories"] or set(settings["categories"]) - set(
                CATEGORIES
            ):
                raise ValueError("Select at least one valid category.")
            if len(settings["channels"]) > 25 or any(
                type(cid) is not int or cid <= 0 for cid in settings["channels"]
            ):
                raise ValueError("Choose up to 25 valid channels.")
            if not 1 <= settings["target"] <= 10000:
                raise ValueError("The next target must be between 1 and 10,000.")
            await self.config.guild_from_id(guild_id).set(settings)
            self._settings[guild_id] = settings
            return copy.deepcopy(settings)

    async def can_manage(self, user: discord.Member) -> bool:
        return (
            user.guild_permissions.manage_guild
            or await self.bot.is_owner(user)
            or await self.bot.is_admin(user)
        )

    async def cog_check(self, ctx: commands.Context) -> bool:
        """Enforce settings access on hybrid children as well as the group."""
        if ctx.command.qualified_name == "copypasta":
            return True
        if ctx.guild is None:
            raise commands.NoPrivateMessage()
        if not await self.can_manage(ctx.author):
            raise commands.CheckFailure(
                "You need Manage Server or Red administrator permission."
            )
        return True

    # --- HELPERS ---

    async def check_cache(self, category: str, content: str) -> bool:
        if content in self.cache[category]:
            return False
        self.cache[category].append(content)
        return True

    async def _fetch(self, url: str, *, headers: dict | None = None) -> bytes | None:
        """Bound provider concurrency, time and response size; never log bodies."""
        if self._closed or self.session is None or self.session.closed:
            return None
        try:
            async with (
                asyncio.timeout(FETCH_DEADLINE),
                self._network_slots,
                self.session.get(url, headers=headers or self.headers) as response,
            ):
                if response.status != 200:
                    log.debug("Random text provider returned HTTP %s", response.status)
                    return None
                body = bytearray()
                async for chunk in response.content.iter_chunked(16384):
                    body.extend(chunk)
                    if len(body) > 524288:
                        log.warning("Random text provider response exceeded 512 KiB")
                        return None
                return bytes(body)
        except (aiohttp.ClientError, asyncio.TimeoutError):
            log.debug("Random text provider request failed")
        return None

    async def fetch_rss(self, url: str) -> ET.Element | None:
        cached = self._rss_cache.get(url)
        if cached and cached[0] > time.monotonic():
            return cached[1]
        body = await self._fetch(url)
        if body is None:
            return None
        try:
            if b"<!DOCTYPE" in body.upper() or b"<!ENTITY" in body.upper():
                return None
            root = ET.fromstring(body)
        except ET.ParseError:
            log.debug("Random text provider returned malformed RSS")
            return None
        self._rss_cache[url] = (time.monotonic() + 300, root)
        return root

    def clean_content(self, content):
        if not content:
            return ""
        content = html.unescape(content)
        if "submitted by" in content:
            content = content.split("submitted by")[0]
        content = content.replace("<!-- SC_OFF -->", "").replace("<!-- SC_ON -->", "")
        content = re.sub(r"<[^>]+>", "", content)
        return re.sub(r"\s+", " ", content).strip()

    async def send_split_message(self, channel, text: str) -> None:
        """Send at most three embed pages, with a plaintext permission fallback."""
        destination = getattr(channel, "channel", channel)
        use_embed = (
            not getattr(destination, "guild", None)
            or destination.permissions_for(destination.guild.me).embed_links
        )
        size = 4000 if use_embed else 1900
        text = text[: size * 3 - 1] + "…" if len(text) > size * 3 else text
        for start in range(0, len(text), size):
            chunk = text[start : start + size]
            if use_embed:
                await channel.send(
                    embed=discord.Embed(
                        description=chunk, color=discord.Color.blurple()
                    ),
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            else:
                await channel.send(
                    chunk, allowed_mentions=discord.AllowedMentions.none()
                )

    # --- GENERATORS ---

    async def get_brainrot(self):
        subjects = [
            "Baby Gronk",
            "The Rizzler",
            "Livvy Dunne",
            "Kai Cenat",
            "Duke Dennis",
            "Kevin G",
            "Grimace",
            "Skibidi Toilet",
            "John Pork",
            "IShowSpeed",
            "Caseoh",
            "Quandale Dingle",
            "Adin Ross",
            "Andrew Tate",
            "The Ocky Way guy",
            "The level 10 gyatt",
            "Average Ohio resident",
            "Glizzy Gladiator",
            "The TikTok Rizz Party",
            "Generic npc",
            "The main character",
            "Lil bro",
            "Blud",
            "The opps",
            "My sleep paralysis demon",
            "The skinwalker",
            "Garten of Banban",
            "Huggy Wuggy",
            "Freddy Fazbear",
            "Cocomelon kid",
            "The Jonkler",
            "Man (Arkham)",
            "DaBaby",
            "Ice Spice",
            "Hasbulla",
            "The Pizza Tower guy",
            "Peppino",
            "Talking Ben",
            "MrBeast's clone",
            "The imposter from Among Us",
            "A discord moderator",
            "A kitten",
            "The alpha wolf",
            "The beta cuck",
            "Turkish Quandale Dingle",
            "Galvanized Square Steel",
            "Little John",
            "Eco-friendly Wood Veneer",
        ]

        actions = [
            "just fanum taxed",
            "is mewing at",
            "griddied on",
            "edged to",
            "glazed",
            "mogged",
            "rizzed up",
            "gooned with",
            "hit the griddy on",
            "gatekept",
            "looksmaxxed",
            "crashed out on",
            "hit the hawk tuah on",
            "started jelqing with",
            "broke the edging streak of",
            "hit a clip on",
            "hit the thug shaker with",
            "drank the grimace shake with",
            "fumbled the bag with",
            "got caught in 4k by",
            "is yapping to",
            "is gaslighting",
            "is gatekeeping",
            "hit the boogie down on",
            "cranked 90s on",
            "stream sniped",
            "ratioed",
            "got fanum taxed by",
            "is gooning to",
            "started muning with",
            "hit the griddy in front of",
            "did the lightskin stare at",
            "threw it back for",
            "borrowed screws from aunt for",
            "expanded the room for",
        ]

        objects = [
            "the level 10 gyatt",
            "a grimace shake",
            "the ocky way",
            "the skibidi toilet",
            "the ohio rizz",
            "the sigma",
            "the beta male",
            "the ice spice song",
            "the edging streak",
            "the looksmaxxing tutorial",
            "the subway surfers gameplay",
            "the family guy funny moments",
            "the goth mommy",
            "the rizz god",
            "the fanum tax write-off",
            "the skibidi toilet episode 69",
            "the aura points",
            "the grimace shake recipe",
            "the lunchly meal",
            "the prime bottle",
            "the zaza",
            "the forbidden pre-workout",
            "the fortnite battle pass",
            "the 19 dollar fortnite card",
            "the among us potion",
            "the sussy baka",
            "the goofy ahh uncle",
            "the metal pipe falling sound",
            "the vine boom",
            "the galvanized square steel",
            "the eco-friendly wood veneers",
            "the screws from aunt",
        ]

        locations = [
            "in Ohio",
            "in the backrooms",
            "at the function",
            "in Fortnite",
            "during the grimace shake incident",
            "at 3am",
            "in skibidi city",
            "in the rizz academy",
            "at the sigma convention",
            "in tilted towers",
            "in the pizza tower",
            "at the rizz party",
            "in roblox brookhaven",
            "in the hood",
            "at the looksmaxxing clinic",
            "inside the walls",
            "in the gulag",
            "at the fazbear pizzaria",
            "in chapter 5 season 2",
            "at the wendy's dumpster",
            "in o block",
            "at the tiktok rizz party",
            "in the goon cave",
            "during the winter arc",
        ]

        reactions = [
            "no cap fr",
            "on god",
            "what the sigma?",
            "blud is cooked",
            "it's over for bro",
            "skull emoji x7",
            "vine boom sound effect",
            "literally 1984",
            "average ohio moment",
            "L mans",
            "W rizz",
            "negative canthal tilt",
            "bombastic side eye",
            "criminal offensive side eye",
            "bro thinks he's him",
            "chat is this real?",
            "type sh*t",
            "i'm calling the opps",
            "bro fell off",
            "skill issue",
            "L + ratio",
            "imagine being this cooked",
            "bro needs to lock in",
            "absolute cinema",
            "bro is onto nothing",
            "who let him cook?",
            "i'm crashing out",
            "bro lost his aura",
            "minus 1000 aura",
            "looksmaxxing final boss",
            "is this physiquemaxxing?",
            "hawk tuah spit on that thing",
            "trippi troppi",
        ]

        roll = random.random()
        s1 = f"{random.choice(subjects)} {random.choice(actions)} {random.choice(objects)} {random.choice(locations)}. {random.choice(reactions)}"

        if roll < 0.2:
            s2 = f"{random.choice(subjects)} {random.choice(actions)} {random.choice(objects)}."
            final = f"{s1} {s2}"
        elif roll > 0.9:
            s2 = f"{random.choice(reactions).upper()} {random.choice(reactions).upper()} {random.choice(subjects)} IS COOKED."
            final = f"{s1} {s2}"
        else:
            final = s1

        if await self.check_cache("brainrot", final):
            return final
        return None

    async def get_showerthought(self):
        root = await self.fetch_rss(
            "https://www.reddit.com/r/showerthoughts/top.rss?t=week&limit=25"
        )
        if root is not None:
            entries = root.findall("{http://www.w3.org/2005/Atom}entry")
            random.shuffle(entries)
            for entry in entries:
                title = entry.findtext("{http://www.w3.org/2005/Atom}title", "").strip()
                if title and await self.check_cache("showerthought", title):
                    return f"🚿 **Shower Thought:**\n{title}"
        return None

    async def get_dadjoke(self):
        return await self._json_text(
            "https://icanhazdadjoke.com/", "joke", "dadjoke", "😂 **Dad Joke:**"
        )

    async def get_fact(self):
        return await self._json_text(
            "https://uselessfacts.jsph.pl/random.json?language=en",
            "text",
            "fact",
            "🧠 **Fact:**",
        )

    async def _json_text(
        self, url: str, key: str, category: str, label: str
    ) -> str | None:
        body = await self._fetch(
            url, headers={**self.headers, "Accept": "application/json"}
        )
        if body is None:
            return None
        try:
            data = json.loads(body)
        except (ValueError, UnicodeError):
            log.debug("Random text provider returned invalid JSON")
            return None
        value = data.get(key) if isinstance(data, dict) else None
        if (
            isinstance(value, str)
            and value.strip()
            and len(value) <= 12000
            and await self.check_cache(category, value)
        ):
            return f"{label}\n{value}"
        return None

    async def get_copypasta_text(self):
        root = await self.fetch_rss(
            "https://www.reddit.com/r/copypasta/new.rss?limit=25"
        )
        if root is not None:
            entries = root.findall("{http://www.w3.org/2005/Atom}entry")
            random.shuffle(entries)
            for entry in entries:
                title = entry.findtext("{http://www.w3.org/2005/Atom}title", "")
                content = entry.findtext("{http://www.w3.org/2005/Atom}content", "")
                body = self.clean_content(content)

                if len(body) < 10 or body.strip() == title.strip():
                    final = title
                else:
                    final = f"**{title}**\n\n{body}"

                if len(final) > 20:
                    return final
        return "❌ Failed to fetch copypasta."

    # --- LISTENERS & LOGIC ---

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if self._closed or message.author.bot or not message.guild:
            return
        if await self.bot.cog_disabled_in_guild(self, message.guild):
            return
        if not await self.bot.allowed_by_whitelist_blacklist(message.author):
            return
        permissions = message.channel.permissions_for(message.guild.me)
        can_send = (
            permissions.send_messages_in_threads
            if isinstance(message.channel, discord.Thread)
            else permissions.send_messages
        )
        if not can_send:
            return
        settings = await self.get_settings(message.guild.id)
        channel_ids = (message.channel.id, getattr(message.channel, "parent_id", None))
        if not settings["enabled"] or (
            settings["channels"]
            and not set(channel_ids).intersection(settings["channels"])
        ):
            return
        ctx = await self.bot.get_context(message)
        if ctx.valid:
            return
        guild_id = message.guild.id
        async with self._locks[guild_id]:
            settings = await self.get_settings(guild_id)
            if (
                self._closed
                or guild_id in self._busy
                or not settings["enabled"]
                or (
                    settings["channels"]
                    and not set(channel_ids).intersection(settings["channels"])
                )
            ):
                return
            settings["counter"] += 1
            if settings["counter"] < settings["target"]:
                await self.config.guild_from_id(guild_id).counter.set(
                    settings["counter"]
                )
                self._settings[guild_id] = settings
                return
            settings["counter"] = 0
            settings["target"] = random.randint(
                settings["frequency_min"], settings["frequency_max"]
            )
            await self.config.guild_from_id(guild_id).set(settings)
            self._settings[guild_id] = settings
            self._busy.add(guild_id)
        try:
            text = await self.generate_text(settings["categories"])
            # Configuration can change while a provider request is in flight.
            current = await self.get_settings(guild_id)
            if (
                text
                and not self._closed
                and current["enabled"]
                and current["categories"] == settings["categories"]
                and (
                    not current["channels"]
                    or set(channel_ids).intersection(current["channels"])
                )
            ):
                await self.send_split_message(message.channel, text)
        except discord.HTTPException:
            log.warning("Could not deliver random text in guild %s", guild_id)
        finally:
            self._busy.discard(guild_id)

    async def generate_text(self, categories: list[str]) -> str | None:
        """Try only enabled categories, at most once each."""
        choices = list(dict.fromkeys(c for c in categories if c in CATEGORIES))
        random.shuffle(choices)
        for category in choices:
            text = await getattr(self, f"get_{category}")()
            if text:
                return text
        return None

    # --- COMMANDS ---

    @commands.hybrid_group(invoke_without_command=True, fallback="settings")
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    async def randomtext(self, ctx: commands.Context) -> None:
        """Open the random text settings panel."""
        from .ui import RandomTextView

        view = RandomTextView(self, ctx.author.id, ctx.guild.id)
        await view.build()
        view.message = await ctx.send(
            view=view, ephemeral=True, allowed_mentions=discord.AllowedMentions.none()
        )

    @randomtext.command()
    async def toggle(self, ctx: commands.Context) -> None:
        """Enable/Disable random text for the ENTIRE server."""

        def edit(settings: dict) -> None:
            settings["enabled"] = not settings["enabled"]

        settings = await self._edit_settings(ctx.guild.id, edit)
        state = "Enabled" if settings["enabled"] else "Disabled"
        await ctx.send(f"✅ Random Text is now **{state}** for this server.")

    @randomtext.command()
    async def settarget(self, ctx: commands.Context, messages: int) -> None:
        """Set the number of messages required to trigger the NEXT text."""
        if not 1 <= messages <= 10000:
            await ctx.send("Choose between 1 and 10,000 messages.")
            return
        await self.update_settings(ctx.guild.id, target=messages, counter=0)
        await ctx.send(
            f"✅ Counter reset! The next random text will trigger after **{messages}** chat messages."
        )

    @randomtext.command(name="frequency")
    async def frequency(
        self, ctx: commands.Context, minimum: int, maximum: int
    ) -> None:
        """Set the persistent random message interval and reset the counter."""
        if not 1 <= minimum <= maximum <= 10000:
            return await ctx.send("Use 1–10,000 messages, minimum first.")
        await self.update_settings(
            ctx.guild.id,
            frequency_min=minimum,
            frequency_max=maximum,
            target=random.randint(minimum, maximum),
            counter=0,
        )
        await ctx.send(
            f"Future posts will be spaced {minimum}–{maximum} eligible messages apart."
        )

    @randomtext.command(name="category")
    async def category(
        self, ctx: commands.Context, category: Category, enabled: bool
    ) -> None:
        """Enable or disable one content category."""

        def edit(settings: dict) -> None:
            settings["categories"] = [
                c
                for c in CATEGORIES
                if (c == category and enabled)
                or (c != category and c in settings["categories"])
            ]

        try:
            await self._edit_settings(ctx.guild.id, edit)
        except ValueError as error:
            return await ctx.send(str(error))
        await ctx.send(f"{category}: {'enabled' if enabled else 'disabled'}.")

    @randomtext.command(name="channel")
    async def channel(
        self,
        ctx: commands.Context,
        action: Literal["add", "remove", "all"],
        channel: discord.TextChannel | None = None,
    ) -> None:
        """Restrict automatic posts to channels; all restores server-wide behavior."""

        def edit(settings: dict) -> None:
            channels = settings["channels"]
            if action == "all":
                settings["channels"] = []
            elif channel is None:
                raise ValueError("Choose a text channel.")
            elif action == "add" and channel.id not in channels:
                channels.append(channel.id)
            elif action == "remove" and channel.id in channels:
                if len(channels) == 1:
                    raise ValueError(
                        "Use channel all to allow every channel, or toggle to disable automatic posts."
                    )
                channels.remove(channel.id)

        try:
            await self._edit_settings(ctx.guild.id, edit)
        except ValueError as error:
            return await ctx.send(str(error))
        await ctx.send("Automatic posting channels updated.")

    @commands.hybrid_command()
    @commands.cooldown(1, 15, commands.BucketType.user)
    async def copypasta(self, ctx: commands.Context) -> None:
        """Post a random copypasta (Manual Command)."""
        await ctx.defer()
        text = await self.get_copypasta_text()
        # Send through Context so slash invocations receive a completed response.
        await self.send_split_message(ctx, text)

    @commands.Cog.listener()
    async def on_guild_remove(self, guild: discord.Guild) -> None:
        self._settings.pop(guild.id, None)

    async def red_delete_data_for_user(self, *, requester: str, user_id: int) -> None:
        """No user records are persisted by this cog."""
