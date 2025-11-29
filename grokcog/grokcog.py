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

# Optional: Web Search
try:
    from duckduckgo_search import DDGS

    HAS_DDG = True
except ImportError:
    HAS_DDG = False

log = logging.getLogger("red.grokcog")

GROQ_API_BASE = "https://api.groq.com/openai/v1"
DEFAULT_MODEL = "moonshotai/kimi-k2-instruct-0905"

# Optimized System Prompt
K2_PROMPT = """You are DripBot, a helpful and chill AI assistant.

MODE SWITCHING:
1. CASUAL CHAT ("hi", "joke", "how are you"):
   - Be conversational, brief, and fun.
   - DO NOT mention confidence scores or sources.
   - DO NOT say "I am a fact-based AI". Just say hello back.

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
        self.config = Config.get_conf(
            self, identifier=0x4B324B32, force_registration=True
        )
        self.config.register_global(
            api_key=None,
            model_name=DEFAULT_MODEL,
            enable_search=True,
            # Limits
            cooldown_seconds=5,
            max_requests_per_minute=60,
            min_api_call_gap=0.5,
        )
        self.config.register_user(last_request_time=0, request_count=0)

        self._session: Optional[aiohttp.ClientSession] = None
        self._cache: Dict[str, Tuple[float, discord.Embed]] = {}
        self._locks: Dict[int, asyncio.Lock] = {}  # User locks
        self._api_lock = asyncio.Lock()
        self._last_call = 0.0

    async def cog_load(self):
        self._session = aiohttp.ClientSession()
        if not HAS_DDG:
            log.warning(
                "duckduckgo-search is missing. Web search disabled. (pip install duckduckgo-search)"
            )

    async def cog_unload(self):
        if self._session:
            await self._session.close()

    async def _get_search_results(self, query: str) -> List[Dict[str, str]]:
        """Performs a web search if enabled and relevant."""
        if not HAS_DDG or not await self.config.enable_search():
            return []

        # Skip search for short/casual queries to save time
        if len(query) < 5 or query.lower() in ["hi", "hello", "help", "ping"]:
            return []

        def _search():
            with DDGS() as ddgs:
                # Get top 3 results
                return list(ddgs.text(query, max_results=3))

        try:
            return await self.bot.loop.run_in_executor(None, _search)
        except Exception as e:
            log.warning(f"Search failed: {e}")
            return []

    async def _ask_api(self, prompt: str, context: str = "") -> dict:
        api_key = await self.config.api_key()
        if not api_key:
            raise ValueError("API Key not set.")

        # Rate Limiting
        async with self._api_lock:
            now = time.time()
            gap = await self.config.min_api_call_gap()
            if now - self._last_call < gap:
                await asyncio.sleep(gap - (now - self._last_call))
            self._last_call = time.time()

        messages = [{"role": "system", "content": K2_PROMPT}]

        if context:
            user_msg = f"SEARCH RESULTS:\n{context}\n\nUSER QUESTION:\n{prompt}"
        else:
            user_msg = prompt

        messages.append({"role": "user", "content": user_msg})

        payload = {
            "model": await self.config.model_name(),
            "messages": messages,
            "temperature": 0.3,
            "max_completion_tokens": 4096,
            "stream": False,
        }

        headers = {"Authorization": f"Bearer {api_key}"}

        try:
            async with self._session.post(
                f"{GROQ_API_BASE}/chat/completions",
                json=payload,
                headers=headers,
                timeout=30,
            ) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    raise ValueError(f"API Error {resp.status}: {text}")

                data = await resp.json()
                content = data["choices"][0]["message"]["content"]
                return self._parse_json(content)
        except Exception as e:
            log.error(f"API Request Failed: {e}")
            return {
                "answer": "I encountered an error processing that.",
                "confidence": 0,
                "sources": [],
            }

    def _parse_json(self, text: str) -> dict:
        """Robust JSON extractor."""
        try:
            # Fast path
            return json.loads(text)
        except:
            # Regex path for markdown blocks
            match = re.search(r"``````", text, re.DOTALL)
            if match:
                try:
                    return json.loads(match.group(1))
                except:
                    pass
            # Regex path for raw braces
            match = re.search(r"\{.*?\}", text, re.DOTALL)
            if match:
                try:
                    return json.loads(match.group(0))
                except:
                    pass

            # Fallback
            return {"answer": text, "confidence": 0.5, "sources": []}

    def _create_embed(self, data: dict) -> discord.Embed:
        answer = data.get("answer", "No response.")
        confidence = data.get("confidence", 0.0)
        sources = data.get("sources", [])

        # Dynamic Color
        if confidence > 0.8:
            color = discord.Color.green()
        elif confidence > 0.5:
            color = discord.Color.gold()
        else:
            color = discord.Color.red()

        embed = discord.Embed(
            title="DripBot's Response",
            description=answer[:4000],
            color=color,
            timestamp=datetime.now(timezone.utc),
        )

        # SMART DISPLAY LOGIC:
        # Only show 'Sources' if they exist.
        # Only show 'Confidence' if there are sources OR it's a long, serious answer.
        # This hides stats for "Hi" or simple chat.
        is_serious = len(sources) > 0 or (len(answer) > 150 and confidence > 0)

        if is_serious:
            embed.add_field(name="Confidence", value=f"{confidence:.0%}", inline=True)

        if sources:
            links = []
            for i, s in enumerate(sources[:5], 1):
                title = s.get("title", "Source")
                url = s.get("url", "#")
                links.append(f"[{i}. {title}]({url})")

            if links:
                embed.add_field(name="Sources", value="\n".join(links), inline=False)

        embed.set_footer(
            text="Powered by 2 Romanian kids • Retardation only (fact-checks)"
        )
        return embed

    @commands.command()
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def grok(self, ctx: commands.Context, *, query: str):
        """Ask DripBot a question."""
        async with ctx.typing():
            # Check cache
            key = hashlib.md5(query.lower().encode()).hexdigest()
            if key in self._cache:
                ts, embed = self._cache[key]
                if time.time() - ts < 3600:  # 1 hour cache
                    await ctx.send(embed=embed)
                    return

            # 1. Web Search (if needed)
            search_data = await self._get_search_results(query)
            context_text = ""
            if search_data:
                context_text = "\n".join(
                    [
                        f"- Title: {r.get('title')}\n  URL: {r.get('href')}\n  Snippet: {r.get('body')}"
                        for r in search_data
                    ]
                )

            # 2. LLM Query
            response_data = await self._ask_api(query, context_text)

            # 3. Format & Send
            embed = self._create_embed(response_data)
            self._cache[key] = (time.time(), embed)  # Cache result

            await ctx.send(embed=embed)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return

        # Mention trigger logic
        if self.bot.user in message.mentions:
            content = message.content.replace(f"<@{self.bot.user.id}>", "").strip()
            if content:
                ctx = await self.bot.get_context(message)
                # Re-use the grok command logic
                await self.grok(ctx, query=content)

    # Admin Commands
    @commands.group()
    @commands.is_owner()
    async def grokadmin(self, ctx):
        """Admin settings for GrokCog."""
        pass

    @grokadmin.command()
    async def setkey(self, ctx, key: str):
        """Set Groq API Key."""
        await self.config.api_key.set(key)
        await ctx.send("✅ API Key updated.")

    @grokadmin.command()
    async def togglesearch(self, ctx):
        """Toggle web search."""
        val = not await self.config.enable_search()
        await self.config.enable_search.set(val)
        await ctx.send(f"Web search set to: {val}")


async def setup(bot):
    await bot.add_cog(GrokCog(bot))
