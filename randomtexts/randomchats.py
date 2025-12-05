import asyncio
import html
import random
import re
import xml.etree.ElementTree as ET

import aiohttp
import discord
from redbot.core import Config, commands
from redbot.core.bot import Red


class RandomText(commands.Cog):
    """Randomly sends Brainrot, Showerthoughts, Jokes, and Facts in chat."""

    def __init__(self, bot: Red):
        self.bot = bot
        self.session = aiohttp.ClientSession()
        self.headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

        # Config to store per-channel settings
        self.config = Config.get_conf(
            self, identifier=98429482394, force_registration=True
        )
        default_channel = {"enabled": False, "counter": 0, "target": 50}
        self.config.register_channel(**default_channel)

        # Transient Cache
        self.cache = {"brainrot": [], "showerthought": [], "dadjoke": [], "fact": []}

    async def cog_unload(self):
        await self.session.close()

    # --- HELPERS ---

    async def check_cache(self, category, content):
        if content in self.cache[category]:
            return False
        self.cache[category].append(content)
        if len(self.cache[category]) > 20:
            self.cache[category].pop(0)
        return True

    async def fetch_rss(self, url):
        try:
            async with self.session.get(
                url, headers=self.headers, timeout=4
            ) as response:
                if response.status == 200:
                    text = await response.text()
                    return ET.fromstring(text)
        except:
            pass
        return None

    def clean_content(self, content):
        if not content:
            return ""
        content = html.unescape(content)
        if "submitted by" in content:
            content = content.split("submitted by")[0]
        content = content.replace("<!-- SC_OFF -->", "").replace("<!-- SC_ON -->", "")
        content = re.sub(r"<[^>]+>", "", content)
        return re.sub(r"\s+", " ", content).strip()

    async def send_split_message(self, channel, text):
        if len(text) <= 2000:
            await channel.send(text)
            return
        chunks = [text[i : i + 1990] for i in range(0, len(text), 1990)]
        for chunk in chunks:
            await channel.send(chunk)
            await asyncio.sleep(1)

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
        return await self.get_brainrot()

    async def get_showerthought(self):
        root = await self.fetch_rss(
            "https://www.reddit.com/r/showerthoughts/top.rss?t=week&limit=25"
        )
        if root is not None:
            entries = root.findall("{http://www.w3.org/2005/Atom}entry")
            random.shuffle(entries)
            for entry in entries:
                title = entry.find("{http://www.w3.org/2005/Atom}title").text or ""
                if await self.check_cache("showerthought", title):
                    return f"🚿 **Shower Thought:**\n{title}"
        return None

    async def get_dadjoke(self):
        try:
            async with self.session.get(
                "https://icanhazdadjoke.com/", headers={"Accept": "application/json"}
            ) as r:
                if r.status == 200:
                    js = await r.json()
                    joke = js["joke"]
                    if await self.check_cache("dadjoke", joke):
                        return f"😂 **Dad Joke:**\n{joke}"
        except:
            pass
        return None

    async def get_fact(self):
        try:
            async with self.session.get(
                "https://uselessfacts.jsph.pl/random.json?language=en"
            ) as r:
                if r.status == 200:
                    js = await r.json()
                    fact = js["text"]
                    if await self.check_cache("fact", fact):
                        return f"🧠 **Fact:**\n{fact}"
        except:
            pass
        return None

    async def get_copypasta_text(self):
        root = await self.fetch_rss(
            "https://www.reddit.com/r/copypasta/new.rss?limit=25"
        )
        if root is not None:
            entries = root.findall("{http://www.w3.org/2005/Atom}entry")
            random.shuffle(entries)
            for entry in entries:
                title = entry.find("{http://www.w3.org/2005/Atom}title").text or ""
                content = entry.find("{http://www.w3.org/2005/Atom}content").text or ""
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
    async def on_message(self, message):
        if message.author.bot:
            return
        if not message.guild:
            return

        if not await self.config.channel(message.channel).enabled():
            return

        current = await self.config.channel(message.channel).counter()
        target = await self.config.channel(message.channel).target()

        current += 1

        if current >= target:
            await self.trigger_random_text(message.channel)
            await self.config.channel(message.channel).counter.set(0)
            await self.config.channel(message.channel).target.set(
                random.randint(10, 100)
            )
        else:
            await self.config.channel(message.channel).counter.set(current)

    async def trigger_random_text(self, channel):
        options = [
            self.get_brainrot,
            self.get_showerthought,
            self.get_dadjoke,
            self.get_fact,
        ]
        func = random.choice(options)

        text = await func()
        if text is None:
            text = await self.get_brainrot()

        await self.send_split_message(channel, text)

    # --- COMMANDS ---

    @commands.group()
    @commands.guild_only()
    @commands.admin_or_permissions(manage_channels=True)
    async def randomtext(self, ctx):
        """Manage Random Text settings."""
        pass

    @randomtext.command()
    async def toggle(self, ctx):
        """Enable/Disable random text in this channel."""
        current = await self.config.channel(ctx.channel).enabled()
        await self.config.channel(ctx.channel).enabled.set(not current)
        state = "Enabled" if not current else "Disabled"
        await ctx.send(f"✅ Random Text is now **{state}** in this channel.")

    @randomtext.command()
    async def force(self, ctx, messages: int):
        """Set the number of messages required to trigger the next random text."""
        if messages < 1:
            await ctx.send("❌ Number must be at least 1.")
            return
        await self.config.channel(ctx.channel).target.set(messages)
        await self.config.channel(ctx.channel).counter.set(0)
        await ctx.send(
            f"✅ Next random text will trigger after **{messages}** messages."
        )

    @commands.command()
    async def copypasta(self, ctx):
        """Post a random copypasta (Manual Command)."""
        text = await self.get_copypasta_text()
        await self.send_split_message(ctx.channel, text)
