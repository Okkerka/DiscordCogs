# grokcog.py - FIXED FOR YOUR FILE NAME
"""
GrokCog - K2 Thinking with DripBot branding
Version: 3.0.1
"""

import asyncio
import json
import logging
from datetime import datetime
from typing import Dict, Optional, Tuple
import hashlib
import aiohttp

import discord
from redbot.core import commands, Config, checks
from redbot.core.utils.mod import is_admin_or_superior

log = logging.getLogger("red.grokcog")

K2_MODEL = "kimi-k2-thinking"
K2_PROMPT = """You are DripBot's AI brain, powered by Kimi K2. You have built-in search and advanced reasoning.

TASK: Answer the user's question accurately. Use your search capability when needed. Cite sources naturally.

RESPONSE FORMAT:
{
  "answer": "Your clear, concise answer with [1], [2] citations",
  "confidence": 0.95,
  "sources": [{"title": "Page Title", "url": "https://example.com"}]
}

RULES:
- Search the web if you lack recent information
- Cite sources using bracket notation
- Confidence score 0.0-1.0
- Be direct, helpful, and occasionally witty
"""

class GrokCog(commands.Cog):
    """🧠 DripBot's AI brain - Powered by Kimi K2 Thinking"""

    __version__ = "3.0.1"

    def __init__(self, bot):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=0x4B324B32, force_registration=True)

        self.config.register_global(
            api_key=None,
            timeout=60
        )
        self.config.register_guild(
            enabled=True,
            max_input_length=4000
        )
        self.config.register_user(
            request_count=0,
            last_request_time=None
        )

        self._active: Dict[int, asyncio.Task] = {}
        self._cache: Dict[str, Tuple[float, str]] = {}
        self._session: Optional[aiohttp.ClientSession] = None

    async def cog_load(self):
        self._session = aiohttp.ClientSession()
        log.info("GrokCog (DripBot AI) loaded successfully")

    async def cog_unload(self):
        if self._session:
            await self._session.close()
        for task in self._active.values():
            task.cancel()
        self._active.clear()

    @staticmethod
    def _key(text: str) -> str:
        return hashlib.sha256(text.encode()).hexdigest()[:12]

    def _cache_get(self, key: str, ttl: int = 3600) -> Optional[str]:
        if not (item := self._cache.get(key)):
            return None
        ts, val = item
        if datetime.utcnow().timestamp() - ts > ttl:
            self._cache.pop(key, None)
            return None
        return val

    def _cache_set(self, key: str, val: str):
        self._cache[key] = (datetime.utcnow().timestamp(), val)
        if len(self._cache) > 256:
            oldest = sorted(self._cache.items(), key=lambda x: x[1][0])[:64]
            for k, _ in oldest:
                self._cache.pop(k, None)

    async def _delete(self, msg: Optional[discord.Message]):
        if msg:
            try:
                await msg.delete()
            except:
                pass

    async def _ask_k2(self, question: str) -> Dict:
        api_key = await self.config.api_key()
        if not api_key:
            raise ValueError("API key not configured")

        messages = [
            {"role": "system", "content": K2_PROMPT},
            {"role": "user", "content": question}
        ]

        payload = {
            "model": K2_MODEL,
            "messages": messages,
            "temperature": 0.3,
            "max_tokens": 2000,
            "tools": [{"type": "builtin", "name": "search"}]
        }

        async with self._session.post(
            "https://api.moonshot.cn/v1/chat/completions",
            json=payload,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json"
            },
            timeout=aiohttp.ClientTimeout(total=60)
        ) as resp:
            resp.raise_for_status()
            data = await resp.json()
            content = data["choices"][0]["message"]["content"]
            return json.loads(content)

    async def _process(self, user_id: int, guild_id: Optional[int], question: str, channel):
        if not (question := question.strip()) or len(question) > 4000:
            await channel.send("❌ Invalid question")
            return

        if user_id in self._active:
            await channel.send("⏳ Already processing")
            return

        if guild_id and not await self.config.guild_from_id(guild_id).enabled():
            await channel.send("❌ Disabled here")
            return

        self._active[user_id] = asyncio.current_task()
        status = None

        try:
            key = self._key(question)
            if cached := self._cache_get(key):
                await channel.send(cached)
                return

            status = await channel.send("🧠 **DripBot is thinking...**")

            result = await self._ask_k2(question)
            text = self._format(result)

            await self._delete(status)
            await channel.send(text)
            self._cache_set(key, text)

            await self.config.user_from_id(user_id).request_count.set(
                await self.config.user_from_id(user_id).request_count() + 1
            )
            await self.config.user_from_id(user_id).last_request_time.set(
                datetime.utcnow().isoformat()
            )

        except Exception as e:
            await self._delete(status)
            log.error(f"Error: {e}")
            await channel.send(f"❌ Error: {str(e)[:100]}")
        finally:
            self._active.pop(user_id, None)

    def _format(self, data: Dict) -> str:
        answer = data.get("answer", "")
        confidence = data.get("confidence", 0.0)
        sources = data.get("sources", [])

        text = answer

        if confidence > 0:
            emoji = "🟢" if confidence > 0.8 else "🟡" if confidence > 0.6 else "🔴"
            text += f"\n\n{emoji} **Confidence:** {confidence:.0%}"

        if sources:
            text += "\n\n**📚 Sources:**"
            for i, src in enumerate(sources[:3], 1):
                title = src.get("title", "Source")[:60]
                url = src.get("url", "")
                text += f"\n{i}. {title} – {url}"

        return text

    @commands.Cog.listener()
    async def on_message(self, msg: discord.Message):
        if msg.author.bot:
            return

        if msg.guild and self.bot.user in msg.mentions:
            if not await self.config.guild(msg.guild).enabled():
                return

            content = msg.content
            for mention in msg.mentions:
                content = content.replace(f"<@{mention.id}>", "")
            question = content.strip()

            if msg.reference and (replied := await msg.channel.fetch_message(msg.reference.message_id)):
                question += f"\n\nContext: {replied.content[:500]}"

            if question:
                await self._process(msg.author.id, msg.guild.id, question, msg.channel)

        elif isinstance(msg.channel, discord.DMChannel):
            if not msg.content.startswith((">", "/", "!")):
                await self._process(msg.author.id, None, msg.content, msg.channel)

    @commands.hybrid_group(name="grok", invoke_without_command=True)
    @commands.cooldown(1, 10, commands.BucketType.user)
    async def grok(self, ctx: commands.Context, *, question: str):
        """Ask DripBot's AI anything - it searches and reasons automatically"""
        await self._process(ctx.author.id, ctx.guild.id, question, ctx.channel)

    @grok.command(name="stats")
    async def stats(self, ctx: commands.Context):
        """Your usage stats"""
        stats = await self.config.user(ctx.author).all()
        embed = discord.Embed(title="📊 Your Grok Stats", color=discord.Color.gold())
        embed.add_field(name="Queries", value=stats["request_count"])
        if stats["last_request_time"]:
            ts = int(datetime.fromisoformat(stats["last_request_time"]).timestamp())
            embed.add_field(name="Last", value=f"<t:{ts}:R>")
        await ctx.send(embed=embed)

    @grok.group(name="admin")
    @commands.check(is_admin_or_superior)
    async def grok_admin(self, ctx: commands.Context):
        """Admin settings for Grok"""
        pass

    @grok_admin.command(name="apikey")
    @commands.is_owner()
    async def apikey(self, ctx: commands.Context, *, key: str):
        """Set the API key (Owner only)"""
        await self.config.api_key.set(key)
        await ctx.send("✅ API key saved")

    @grok_admin.command(name="toggle")
    async def toggle(self, ctx: commands.Context):
        """Toggle Grok in this server"""
        enabled = await self.config.guild(ctx.guild).enabled()
        await self.config.guild(ctx.guild).enabled.set(not enabled)
        await ctx.send(f"{'✅ Grok **ENABLED**' if not enabled else '❌ Grok **DISABLED**'}")

async def setup(bot):
    await bot.add_cog(GrokCog(bot))
