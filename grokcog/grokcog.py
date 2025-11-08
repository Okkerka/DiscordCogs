# grok_cog.py
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
import time
import weakref
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import aiohttp
import discord
from pydantic import BaseModel, Field, ValidationError
from redbot.core import Config, checks, commands
from redbot.core.bot import Red
from redbot.core.utils.chat_formatting import pagify

log = logging.getLogger("red.grokcog")

# ---------------------------------------------------------------------------
# Config & constants
# ---------------------------------------------------------------------------

GROQ_API: str = "https://api.groq.com/openai/v1/chat/completions"
SEARCH_TIMEOUT: float = 5.0
HTTP_TIMEOUT: aiohttp.ClientTimeout = aiohttp.ClientTimeout(total=30)
MAX_TOKENS: Dict[str, int] = {
    "math": 120,
    "default": 640,
}
CACHE_TTL: int = 3600 * 6  # 6 h
CACHE_SIZE: int = 256

# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class SearchResult(BaseModel):
    title: str = "Untitled"
    snippet: str = ""
    url: str = ""

class GroqChoice(BaseModel):
    message: Dict[str, Any]
    finish_reason: Optional[str]

class GroqResponse(BaseModel):
    choices: List[GroqChoice]

class RouterAnswer(BaseModel):
    type: str
    answer: str
    bullets: List[str] = Field(default_factory=list)
    citations: List[int] = Field(default_factory=list)
    verdict: Optional[str] = None
    reason: Optional[str] = None

# ---------------------------------------------------------------------------
# Token counter (cheap & fast)
# ---------------------------------------------------------------------------

try:
    import tiktoken

    _enc = tiktoken.get_encoding("cl100k_base")

    def count_tokens(text: str) -> int:
        return len(_enc.encode(text))

except ImportError:
    # crude fallback: 1 token ≈ 4 chars
    def count_tokens(text: str) -> int:
        return len(text) // 4

# ---------------------------------------------------------------------------
# LRU cache (thread-safe, memory-capped)
# ---------------------------------------------------------------------------

class LRUCache:
    __slots__ = ("_data", "_lock", "_maxsize")

    def __init__(self, maxsize: int = 128):
        self._maxsize = maxsize
        self._data: OrderedDict[str, Tuple[float, str]] = OrderedDict()
        self._lock = asyncio.Lock()

    async def get(self, key: str, ttl: int = CACHE_TTL) -> Optional[str]:
        async with self._lock:
            if key not in self._data:
                return None
            ts, val = self._data[key]
            if time.time() - ts > ttl:
                self._data.pop(key, None)
                return None
            # move to end (LRU)
            self._data.move_to_end(key)
            return val

    async def set(self, key: str, val: str) -> None:
        async with self._lock:
            self._data[key] = (time.time(), val)
            self._data.move_to_end(key)
            while len(self._data) > self._maxsize:
                self._data.popitem(last=False)

# ---------------------------------------------------------------------------
# Circuit-breaker helper
# ---------------------------------------------------------------------------

class CircuitBreaker:
    __slots__ = ("failures", "last_fail", "threshold", "timeout", "lock")

    def __init__(self, threshold: int = 5, timeout: float = 60):
        self.failures = 0
        self.last_fail = 0.0
        self.threshold = threshold
        self.timeout = timeout
        self.lock = asyncio.Lock()

    async def trip(self) -> bool:
        async with self.lock:
            if self.failures >= self.threshold:
                if time.time() - self.last_fail < self.timeout:
                    return True
                self.failures = 0
            return False

    async def success(self) -> None:
        async with self.lock:
            self.failures = 0

    async def fail(self) -> None:
        async with self.lock:
            self.failures += 1
            self.last_fail = time.time()

# ---------------------------------------------------------------------------
# Cog
# ---------------------------------------------------------------------------

class GrokCog(commands.Cog):
    """Private Groq assistant with search, mentions, >grok and DMs."""

    __version__ = "2.0.0"

    def __init__(self, bot: Red):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=0x_GROK, force_registration=True)
        self.session: Optional[aiohttp.ClientSession] = None
        self._search_cache = LRUCache(CACHE_SIZE)
        self._answer_cache = LRUCache(CACHE_SIZE)
        self._cb_groq = CircuitBreaker()
        self._cb_search = CircuitBreaker()
        self._user_sem: weakref.WeakValueDictionary[int, asyncio.Semaphore] = weakref.WeakValueDictionary()

        default_global = {"api_key": None, "model": "llama-3.3-70b-versatile"}
        default_guild = {"enabled": True, "max_input_length": 2000}
        default_user = {"request_count": 0, "last_request_time": None}
        self.config.register_global(**default_global)
        self.config.register_guild(**default_guild)
        self.config.register_user(**default_user)

    # -----------------------------------------------------------------------
    # Life-cycle
    # -----------------------------------------------------------------------

    async def cog_load(self) -> None:
        self.session = aiohttp.ClientSession(timeout=HTTP_TIMEOUT)

    async def cog_unload(self) -> None:
        if self.session and not self.session.closed:
            await self.session.close()

    # -----------------------------------------------------------------------
    # Utilities
    # -----------------------------------------------------------------------

    def _get_sem(self, user_id: int) -> asyncio.Semaphore:
        if user_id not in self._user_sem:
            self._user_sem[user_id] = asyncio.Semaphore(1)
        return self._user_sem[user_id]

    @staticmethod
    def _hash(text: str) -> str:
        import hashlib
        return hashlib.sha256(text.encode()).hexdigest()[:12]

    # -----------------------------------------------------------------------
    # Search
    # -----------------------------------------------------------------------

    @staticmethod
    async def _ddgs_search(query: str, max_results: int = 5) -> List[SearchResult]:
        # ddgs is sync, so run in thread
        def _sync() -> List[SearchResult]:
            try:
                from ddgs import DDGS
                raw = list(DDGS().text(query, max_results=max_results))
                return [SearchResult(**r) for r in raw]
            except Exception as e:
                log.warning("Search failed: %s", e)
                return []
        return await asyncio.to_thread(_sync)

    async def _search(self, query: str) -> List[SearchResult]:
        if await self._cb_search.trip():
            raise RuntimeError("Search circuit-breaker is open")
        try:
            results = await asyncio.wait_for(self._ddgs_search(query), timeout=SEARCH_TIMEOUT)
            await self._cb_search.success()
            return results
        except Exception as e:
            await self._cb_search.fail()
            raise e

    def _render_sources(self, results: List[SearchResult]) -> str:
        if not results:
            return "**Sources:** none"
        lines = ["**Sources:**"]
        for idx, res in enumerate(results, 1):
            snippet = res.snippet.replace("\n", " ")[:250].strip()
            lines.append(f"{idx}) {res.title} — «{snippet}»")
        return "\n".join(lines)

    # -----------------------------------------------------------------------
    # Groq
    # -----------------------------------------------------------------------

    async def _groq_request(self, payload: Dict[str, Any]) -> str:
        if await self._cb_groq.trip():
            raise RuntimeError("Groq circuit-breaker is open")
        api_key = await self.config.api_key()
        if not api_key:
            raise RuntimeError("No API key configured")
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        try:
            async with self.session.post(GROQ_API, json=payload, headers=headers) as resp:
                if resp.status != 200:
                    raise aiohttp.ClientResponseError(resp.request_info, resp.history, status=resp.status)
                raw = await resp.text()
                parsed = GroqResponse.parse_raw(raw)
                await self._cb_groq.success()
                return parsed.choices[0].message["content"]
        except Exception as e:
            await self._cb_groq.fail()
            raise e

    # -----------------------------------------------------------------------
    # Router
    # -----------------------------------------------------------------------

    SYSTEM_PROMPT = (
        "You are a helpful assistant that answers concisely and cites sources when possible.\n"
        "If the query is pure math return JSON: {\"type\":\"math\",\"answer\":\"...\"}\n"
        "If it is a fact-check return JSON: {\"type\":\"fact\",\"verdict\":\"TRUE|FALSE|UNCLEAR\",\"reason\":\"...\"}\n"
        "Otherwise return JSON: {\"type\":\"qa\",\"answer\":\"...\",\"bullets\":[\"...\"]}"
    )

    def _build_messages(self, user_input: str, sources_text: str) -> List[Dict[str, str]]:
        user = f"User: {user_input}\n\n{sources_text}"
        return [{"role": "system", "content": self.SYSTEM_PROMPT}, {"role": "user", "content": user}]

    async def _call_router(self, user_input: str, sources_text: str) -> RouterAnswer:
        messages = self._build_messages(user_input, sources_text)
        is_math = bool(re.fullmatch(r"[0-9+\-*/^().= xX]+", user_input.strip()))
        payload = {
            "model": await self.config.model(),
            "messages": messages,
            "temperature": 0.1 if is_math else 0.3,
            "max_tokens": MAX_TOKENS["math"] if is_math else MAX_TOKENS["default"],
            "response_format": {"type": "json_object"},
        }
        raw = await self._groq_request(payload)
        try:
            return RouterAnswer.parse_raw(raw)
        except ValidationError:
            # fallback plain text
            return RouterAnswer(type="qa", answer=raw)

    # -----------------------------------------------------------------------
    # Core flow
    # -----------------------------------------------------------------------

    async def _answer(self, user_id: int, guild_id: Optional[int], question: str, channel: discord.abc.Messageable) -> None:
        if not question.strip():
            await channel.send("❌ Empty query")
            return
        if len(question) > await self.config.guild_from_id(guild_id).max_input_length() if guild_id else 2000:
            await channel.send("❌ Query too long")
            return

        sem = self._get_sem(user_id)
        if not sem.locked():
            async with sem:
                await self._do_answer(user_id, guild_id, question, channel)
        else:
            await channel.send("❌ Already processing a query for you")

    async def _do_answer(self, user_id: int, guild_id: Optional[int], question: str, channel: discord.abc.Messageable) -> None:
        cache_key = self._hash(question)
        cached = await self._answer_cache.get(cache_key)
        if cached:
            for page in pagify(cached, page_length=1900):
                await channel.send(page)
            return

        async with channel.typing():
            s_key = self._hash("search|" + question.lower())
            sources_text = await self._search_cache.get(s_key)
            if sources_text is None:
                results = await self._search(question)
                sources_text = self._render_sources(results)
                await self._search_cache.set(s_key, sources_text)

            answer_obj = await self._call_router(question, sources_text)
            if answer_obj.type == "math":
                text = answer_obj.answer
            elif answer_obj.type == "fact":
                text = f"**Verdict:** {answer_obj.verdict}\n**Reason:** {answer_obj.reason}"
            else:
                parts = [answer_obj.answer] + [f"• {b}" for b in answer_obj.bullets]
                text = "\n".join(parts)
            text = text.strip()
            await self._answer_cache.set(cache_key, text)

        for page in pagify(text, page_length=1900):
            await channel.send(page)

        # stats
        async with self.config.user_from_id(user_id).all() as u:
            u["request_count"] += 1
            u["last_request_time"] = datetime.now(timezone.utc).isoformat()

    # -----------------------------------------------------------------------
    # Triggers
    # -----------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_message(self, msg: discord.Message) -> None:
        if msg.author.bot or not self.session:
            return
        question = ""
        if msg.guild and self.bot.user in msg.mentions:
            question = msg.content
            for u in msg.mentions:
                question = question.replace(f"<@{u.id}>", "").replace(f"<@!{u.id}>", "")
            question = question.strip()
            if msg.reference:
                try:
                    ref = await msg.channel.fetch_message(msg.reference.message_id)
                    question = ref.content
                except Exception:
                    pass
        elif isinstance(msg.channel, (discord.DMChannel, discord.GroupChannel)):
            question = msg.content.strip()
            if question.startswith((">", "/")):
                return
        if question:
            await self._answer(msg.author.id, msg.guild.id if msg.guild else None, question, msg.channel)

    # -----------------------------------------------------------------------
    # Commands
    # -----------------------------------------------------------------------

    @commands.group(name="grok", invoke_without_command=True)
    @commands.cooldown(1, 20, commands.BucketType.user)
    async def grok(self, ctx: commands.Context, *, question: str) -> None:
        """Ask a question or fact-check a claim."""
        await self._answer(ctx.author.id, ctx.guild.id if ctx.guild else None, question, ctx.channel)

    @grok.command(name="stats")
    async def stats(self, ctx: commands.Context) -> None:
        """Show your usage stats."""
        cfg = await self.config.user(ctx.author).all()
        em = discord.Embed(title="Grok stats", color=discord.Color.blue())
        em.add_field(name="Queries", value=cfg["request_count"])
        if cfg["last_request_time"]:
            ts = int(datetime.fromisoformat(cfg["last_request_time"]).timestamp())
            em.add_field(name="Last", value=f"<t:{ts}:R>")
        await ctx.send(embed=em)

    # -----------------------------------------------------------------------
    # Admin
    # -----------------------------------------------------------------------

    @grok.group(name="set")
    async def grok_set(self, ctx: commands.Context) -> None:
        """Admin settings."""

    @grok_set.command(name="apikey")
    async def set_apikey(self, ctx: commands.Context, key: str) -> None:
        """Set Groq API key (bot-owner only)."""
        if not await self.bot.is_owner(ctx.author):
            return await ctx.send("❌ Owner only")
        await self.config.api_key.set(key)
        await ctx.send("✅ API key saved")

    @grok_set.command(name="model")
    async def set_model(self, ctx: commands.Context, *, name: str = "llama-3.3-70b-versatile") -> None:
        """Change model (bot-owner only)."""
        if not await self.bot.is_owner(ctx.author):
            return await ctx.send("❌ Owner only")
        await self.config.model.set(name)
        await ctx.send(f"✅ Model set to {name}")

    @grok_set.command(name="toggle")
    async def toggle_guild(self, ctx: commands.Context) -> None:
        """Enable/disable the cog in this server."""
        if not ctx.guild:
            return await ctx.send("❌ Use in a server")
        if not await checks.admin_or_permissions(manage_guild=True).predicate(ctx):
            return await ctx.send("❌ Admin only")
        cur = await self.config.guild(ctx.guild).enabled()
        await self.config.guild(ctx.guild).enabled.set(not cur)
        await ctx.send("✅ Enabled" if not cur else "❌ Disabled")

    # -----------------------------------------------------------------------
    # Error handler
    # -----------------------------------------------------------------------

    async def cog_command_error(self, ctx: commands.Context, error: Exception) -> None:
        if getattr(ctx, "_handled", False):
            return
        if isinstance(error, commands.CommandOnCooldown):
            await ctx.send(f"⏱️ Cooldown: {error.retry_after:.1f}s")
        else:
            log.exception("Unhandled error in grok command")
        ctx._handled = True


async def setup(bot: Red) -> None:
    await bot.add_cog(GrokCog(bot))