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

# --- SEARCH MODULE CHECK ---
try:
    from duckduckgo_search import DDGS
    HAS_DDG = True
except ImportError:
    HAS_DDG = False

# --- CONSTANTS ---
log = logging.getLogger("red.grokcog")

# Groq API Endpoint
GROQ_API_BASE = "https://api.groq.com/openai/v1"

# CHANGED: Default to a model that actually exists on Groq
DEFAULT_MODEL = "llama-3.3-70b-versatile"

# Compiled Regex for Safety
JSON_BLOCK_REGEX = re.compile(r"```(?:json)?\s*(.*?)\s*```
JSON_BRACE_REGEX = re.compile(r"\{.*\}", re.DOTALL)

# --- SYSTEM PROMPT ---
K2_PROMPT = """You are DripBot, a chill and helpful AI assistant.

MODES:
1. CASUAL ("hi", "yo"): Just say hello back. No sources.
2. FACTS: Use SEARCH RESULTS if provided. Cite  inline.[1]

OUTPUT JSON ONLY:
{
  "answer": "Response here.",
  "confidence": 0.9,
  "sources": [{"title": "T", "url": "U"}]
}
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
        log.info("GrokCog loaded. Search enabled: %s", HAS_DDG)

    async def cog_unload(self):
        if self._session:
            await self._session.close()

    async def _get_search_results(self, query: str) -> List[Dict[str, str]]:
        if not HAS_DDG or not await self.config.enable_search():
            return []

        # Skip casual words
        if len(query) < 4 or query.lower().split() in ["hi", "hey", "yo", "hello"]:
            return []

        # Run search in thread to avoid blocking bot
        def _search():
            try:
                with DDGS() as ddgs:
                    return list(ddgs.text(query, max_results=3))
            except Exception as e:
                log.warning(f"DDG Search Error: {e}")
                return []

        return await self.bot.loop.run_in_executor(None, _search)

    async def _ask_api(self, prompt: str, context: str = "") -> dict:
        api_key = await self.config.api_key()
        if not api_key:
            return {"answer": "⚠️ API Key not set. Use `[p]grokadmin setkey`.", "confidence": 0, "sources": []}

        # Rate limit check
        async with self._api_lock:
            now = time.time()
            gap = await self.config.min_api_call_gap()
            if now - self._last_call < gap:
                await asyncio.sleep(gap - (now - self._last_call))
            self._last_call = time.time()

        # Construct Payload
        messages = [{"role": "system", "content": K2_PROMPT}]
        content_str = f"SEARCH RESULTS:\n{context}\n\nUSER:\n{prompt}" if context else prompt
        messages.append({"role": "user", "content": content_str})

        model = await self.config.model_name()

        try:
            async with self._session.post(
                f"{GROQ_API_BASE}/chat/completions",
                json={
                    "model": model,
                    "messages": messages,
                    "temperature": 0.3,
                    "max_completion_tokens": 4096,
                    "stream": False
                },
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=30
            ) as resp:
                text = await resp.text()

                if resp.status != 200:
                    log.error(f"Groq API Error {resp.status}: {text}")
                    return {"answer": f"⚠️ API Error {resp.status}: {text}", "confidence": 0}

                data = json.loads(text)
                return self._parse_json(data["choices"]["message"]["content"])

        except Exception as e:
            log.error(f"Connection Failed: {e}")
            return {"answer": "⚠️ Connection failed.", "confidence": 0}

    def _parse_json(self, text: str) -> dict:
        # 1. Try pure JSON
        try:
            return json.loads(text)
        except:
            pass

        # 2. Regex for ```json ... ```
        match = JSON_BLOCK_REGEX.search(text)
        if match:
            try:
                return json.loads(match.group(1))
            except:
                pass

        # 3. Regex for { ... }
        match = JSON_BRACE_REGEX.search(text)
        if match:
            try:
                return json.loads(match.group(0))
            except:
                pass

        # 4. Fallback
        return {"answer": text, "confidence": 0.5, "sources": []}

    def _create_embed(self, data: dict) -> discord.Embed:
        answer = data.get("answer", "No response.")
        confidence = data.get("confidence", 0.0)
        sources = data.get("sources", [])

        # Color logic
        color = discord.Color.green() if confidence > 0.8 else discord.Color.gold()
        if "⚠️" in answer: color = discord.Color.red()

        embed = discord.Embed(
            title="DripBot's Response",
            description=answer[:4000],
            color=color,
            timestamp=datetime.now(timezone.utc)
        )

        # Smart Footer/Fields
        show_stats = sources or (len(answer) > 100 and "⚠️" not in answer)

        if show_stats:
            embed.add_field(name="Confidence", value=f"{confidence:.0%}", inline=True)

        if sources:
            links = [f"[{i+1}. {s.get('title','Src')}]({s.get('url','#')})" for i,s in enumerate(sources[:5])]
            embed.add_field(name="Sources", value="\n".join(links), inline=False)

        embed.set_footer(text="Powered by 2 Romanian kids -  Retardation only (fact-checks)")
        return embed

    @commands.command()
    @commands.cooldown(1, 3, commands.BucketType.user)
    async def grok(self, ctx: commands.Context, *, query: str):
        """Ask the AI."""
        try:
            async with ctx.typing():
                # Cache Key
                key = hashlib.md5(query.lower().encode()).hexdigest()

                # Check Cache
                if key in self._cache:
                    ts, cached_embed = self._cache[key]
                    if time.time() - ts < 3600:
                        await ctx.send(embed=cached_embed)
                        return

                # Pipeline: Search -> Ask -> Format
                search_res = await self._get_search_results(query)

                context = ""
                if search_res:
                    context = "\n".join([f"- {r.get('title')}: {r.get('body')}" for r in search_res])

                response_data = await self._ask_api(query, context)

                final_embed = self._create_embed(response_data)
                self._cache[key] = (time.time(), final_embed)

                await ctx.send(embed=final_embed)

        except Exception as e:
            log.exception("Command failed")
            await ctx.send(f"❌ internal error: {e}")

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot: return

        # Robust mention check
        if self.bot.user.id in [m.id for m in message.mentions]:
            # Remove mention from text
            clean_content = re.sub(f"<@!?{self.bot.user.id}>", "", message.content).strip()
            if clean_content:
                ctx = await self.bot.get_context(message)
                await ctx.invoke(self.grok, query=clean_content)

    @commands.group()
    @commands.is_owner()
    async def grokadmin(self, ctx):
        pass

    @grokadmin.command()
    async def setkey(self, ctx, key: str):
        await self.config.api_key.set(key)
        await ctx.send("✅ API Key updated.")

    @grokadmin.command()
    async def setmodel(self, ctx, model: str):
        await self.config.model_name.set(model)
        await ctx.send(f"✅ Model set to `{model}`")

    @grokadmin.command()
    async def togglesearch(self, ctx):
        val = not await self.config.enable_search()
        await self.config.enable_search.set(val)
        await ctx.send(f"Search enabled: {val}")

async def setup(bot):
    await bot.add_cog(GrokCog(bot))
