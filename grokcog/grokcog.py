import asyncio
import hashlib
import json
import logging
import re
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import aiohttp
import discord
from redbot.core import Config, commands

try:
    from duckduckgo_search import DDGS
    HAS_DDG = True
except ImportError:
    HAS_DDG = False

log = logging.getLogger("red.grokcog")

GROQ_API_BASE = "https://api.groq.com/openai/v1"
DEFAULT_MODEL = "moonshotai/kimi-k2-instruct-0905"

K2_PROMPT = """You are DripBot, a helpful and chill AI assistant.

MODE SWITCHING:
1. CASUAL CHAT ("hi", "joke", "how are you"):
   - Be conversational, brief, and fun.
   - DO NOT mention confidence scores or sources.
   - Just say hello back.

2. INFORMATION/FACTS ("who is...", "explain...", "news"):
   - Use the provided SEARCH RESULTS to answer.
   - Cite sources with [1] inline.
   - Be accurate and detailed.

OUTPUT FORMAT (JSON ONLY):
{
  "answer": "Your response string here.",
  "confidence": 0.95,
  "sources": [{"title": "Example", "url": "https://example.com"}]
}

Rules:
- If sources list is empty, confidence is based on your internal knowledge.
- "sources" array should be empty for casual chat.
"""

class GrokCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=0x4B324B32, force_registration=True)
        self.config.register_global(
            api_key=None,
            model_name=DEFAULT_MODEL,
            enable_search=True,
            min_api_call_gap=0.5,
        )
        self._session: Optional[aiohttp.ClientSession] = None
        self._cache: Dict[str, Tuple[float, discord.Embed]] = {}
        self._api_lock = asyncio.Lock()
        self._last_call = 0.0

    async def cog_load(self):
        self._session = aiohttp.ClientSession()

    async def cog_unload(self):
        if self._session:
            await self._session.close()

    async def _get_search_results(self, query: str) -> List[Dict[str, str]]:
        if not HAS_DDG or not await self.config.enable_search():
            return []

        # Skip casual/short queries
        if len(query) < 5 or query.lower().split()[0] in ["hi", "hello", "hey", "yo"]:
            return []

        def _search():
            with DDGS() as ddgs:
                return list(ddgs.text(query, max_results=3))

        try:
            return await self.bot.loop.run_in_executor(None, _search)
        except Exception as e:
            log.warning(f"Search error: {e}")
            return []

    async def _ask_api(self, prompt: str, context: str = "") -> dict:
        api_key = await self.config.api_key()
        if not api_key:
            return {"answer": "⚠️ **API Key Missing**: Please set it with `[p]grokadmin setkey`.", "confidence": 0, "sources": []}

        async with self._api_lock:
            now = time.time()
            gap = await self.config.min_api_call_gap()
            if now - self._last_call < gap:
                await asyncio.sleep(gap - (now - self._last_call))
            self._last_call = time.time()

        messages = [{"role": "system", "content": K2_PROMPT}]
        user_msg = f"SEARCH RESULTS:\n{context}\n\nUSER QUESTION:\n{prompt}" if context else prompt
        messages.append({"role": "user", "content": user_msg})

        payload = {
            "model": await self.config.model_name(),
            "messages": messages,
            "temperature": 0.3,
            "max_completion_tokens": 4096,
            "stream": False
        }

        try:
            async with self._session.post(
                f"{GROQ_API_BASE}/chat/completions",
                json=payload,
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=30
            ) as resp:
                if resp.status != 200:
                    return {"answer": f"⚠️ **API Error {resp.status}**: {await resp.text()}", "confidence": 0, "sources": []}

                data = await resp.json()
                content = data["choices"][0]["message"]["content"]
                return self._parse_json(content)
        except Exception as e:
            log.error(f"API Request Failed: {e}")
            return {"answer": "⚠️ **Connection Error**: Could not reach AI provider.", "confidence": 0, "sources": []}

    def _parse_json(self, text: str) -> dict:
        try:
            return json.loads(text)
        except:
            # Regex fallback for formatted blocks
            for pattern in [r"``````", r"\{.*?\}"]:
                match = re.search(pattern, text, re.DOTALL)
                if match:
                    try:
                        return json.loads(match.group(1 if '```
                    except: pass
            return {"answer": text, "confidence": 0.5, "sources": []}

    def _create_embed(self, data: dict) -> discord.Embed:
        answer = data.get("answer", "No response.")
        confidence = data.get("confidence", 0.0)
        sources = data.get("sources", [])

        color = discord.Color.green() if confidence > 0.8 else discord.Color.gold()
        if "⚠️" in answer: color = discord.Color.red()

        embed = discord.Embed(
            title="DripBot's Response",
            description=answer[:4000],
            color=color,
            timestamp=datetime.now(timezone.utc)
        )

        # Hide stats for casual chat or errors
        if sources or (len(answer) > 100 and "⚠️" not in answer):
            embed.add_field(name="Confidence", value=f"{confidence:.0%}", inline=True)

        if sources:
            links = [f"[{i+1}. {s.get('title', 'Link')}]({s.get('url', '#')})" for i, s in enumerate(sources[:5])]
            embed.add_field(name="Sources", value="\n".join(links), inline=False)

        embed.set_footer(text="Powered by 2 Romanian kids -  Retardation only (fact-checks)")
        return embed

    @commands.command()
    @commands.cooldown(1, 3, commands.BucketType.user)
    async def grok(self, ctx: commands.Context, *, query: str):
        """Ask DripBot."""
        # Safety check to prevent silent failures
        try:
            async with ctx.typing():
                key = hashlib.md5(query.lower().encode()).hexdigest()

                # 1. Cache Check
                if key in self._cache:
                    ts, embed = self._cache[key]
                    if time.time() - ts < 3600:
                        await ctx.send(embed=embed)
                        return

                # 2. Search & Ask
                search_res = await self._get_search_results(query)
                context = "\n".join([f"- {r.get('title')}: {r.get('body')}" for r in search_res]) if search_res else ""

                response = await self._ask_api(query, context)

                # 3. Send
                embed = self._create_embed(response)
                self._cache[key] = (time.time(), embed)
                await ctx.send(embed=embed)

        except Exception as e:
            log.exception("Grok command failed")
            await ctx.send(f"❌ Critical Error: {str(e)}")

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot: return
        if self.bot.user in message.mentions:
            # Fix: Handle both <@ID> and <@!ID> (nickname) mentions
            content = re.sub(f"<@!?{self.bot.user.id}>", "", message.content).strip()
            if content:
                ctx = await self.bot.get_context(message)
                await ctx.invoke(self.grok, query=content)

    @commands.group()
    @commands.is_owner()
    async def grokadmin(self, ctx):
        pass

    @grokadmin.command()
    async def setkey(self, ctx, key: str):
        await self.config.api_key.set(key)
        await ctx.send("✅ API Key updated.")

    @grokadmin.command()
    async def togglesearch(self, ctx):
        val = not await self.config.enable_search()
        await self.config.enable_search.set(val)
        await ctx.send(f"Web search: {val}")

async def setup(bot):
    await bot.add_cog(GrokCog(bot))
