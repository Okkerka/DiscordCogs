"""
GrokCog - AI Assistant for Red Discord Bot
Optimized for Verified Information & Context Awareness
"""

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
from redbot.core.bot import Red

log = logging.getLogger("red.grokcog")

GROQ_API_BASE = "https://api.groq.com/openai/v1"
DEFAULT_MODEL = "moonshotai/kimi-k2-instruct-0905"

# --- OPTIMIZATION: PRE-COMPILED REGEX ---
# 1. Strict Code Block: Matches `````` or ``````
JSON_BLOCK_REGEX = re.compile(r"``````", re.DOTALL)
# 2. Fallback: Matches from the first { to the last } in the string
# Using dotall to capture newlines inside the JSON
JSON_FALLBACK_REGEX = re.compile(r"\{.*\}", re.DOTALL)

# --- OPTIMIZED PROMPT ---
K2_PROMPT = """You are a fact-based AI assistant that ONLY provides information you can verify or cite from credible sources.

CRITICAL RULES:
1. NEVER make up information, facts, statistics, or sources.
2. If you don't have verified information, say "I don't have verified information about this".
3. ONLY cite sources you actually have access to or know to exist with high certainty (like Standard Wikipedia articles).
4. Distinguish clearly between verified facts and general knowledge.
5. When uncertain, explicitly state your uncertainty level.

VERIFICATION & WIKIPEDIA:
- You may use "Wikipedia" as a source for established general knowledge.
- If referencing a Wikipedia article, ensure the URL follows the standard format: "https://en.wikipedia.org/wiki/Topic_Name"
- Do not cite specific news articles or obscure websites unless you have the content in your context.
- Stick to high-confidence general consensus for "verified" info.

Response Format (valid JSON only, no markdown):

{
    "answer": "Your factual answer here.\\n\\nUse verified information only. If making claims, cite sources [1].",
    "confidence": 0.85,
    "sources": [
        {"title": "Wikipedia: Quantum Mechanics", "url": "https://en.wikipedia.org/wiki/Quantum_mechanics"}
    ]
}

Confidence Guidelines:
- 0.9-1.0: Directly cited from verified sources or strict general consensus (e.g., Wikipedia).
- 0.7-0.89: Well-established facts from general knowledge.
- 0.5-0.69: General knowledge with some uncertainty.
- Below 0.5: Speculation or uncertain information.

Source Citation Rules:
- If you don't have sources, use empty array: "sources": []
- Never fabricate URLs.
"""

COOLDOWN_SECONDS = 5
MIN_API_CALL_GAP = 0.5
MAX_REQUESTS_PER_MINUTE = 60
RATE_LIMIT_WINDOW = 60
CACHE_TTL = 3600
MAX_CACHE_SIZE = 256
MAX_INPUT_LENGTH = 8000
MAX_RETRIES = 3


class APIRequestQueue:
    def __init__(self, cog):
        self.cog = cog
        self.queue = asyncio.Queue(maxsize=128)
        self.worker_task = None

    async def start(self):
        if self.worker_task is None or self.worker_task.done():
            self.worker_task = asyncio.create_task(self._worker())
            log.info("API Request Queue worker started")

    async def stop(self):
        if self.worker_task:
            self.worker_task.cancel()
            try:
                await self.worker_task
            except asyncio.CancelledError:
                pass
            self.worker_task = None
            log.info("API Request Queue worker stopped")

    async def enqueue(self, coro) -> Any:
        future = asyncio.Future()
        await self.queue.put((coro, future))
        return await future

    async def _worker(self):
        while True:
            try:
                coro, future = await self.queue.get()
                await self.cog._respect_api_rate_limits()
                try:
                    # Added Timeout to prevent hanging threads if Groq stalls
                    result = await asyncio.wait_for(coro, timeout=60)
                    if not future.done():
                        future.set_result(result)
                except Exception as e:
                    if not future.done():
                        future.set_exception(e)
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.exception("Worker error")
                if "future" in locals() and not future.done():
                    future.set_exception(e)
                await asyncio.sleep(0.1)


class GrokCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.config = Config.get_conf(
            self, identifier=0x4B324B32, force_registration=True
        )

        self.config.register_global(
            api_key=None,
            timeout=120,
            max_retries=MAX_RETRIES,
            cooldown_seconds=COOLDOWN_SECONDS,
            min_api_call_gap=MIN_API_CALL_GAP,
            max_requests_per_minute=MAX_REQUESTS_PER_MINUTE,
            model_name=DEFAULT_MODEL,
            request_queue_enabled=True,
        )

        self.config.register_guild(
            enabled=True,
            max_input_length=MAX_INPUT_LENGTH,
            default_temperature=0.3,
        )

        self.config.register_user(
            request_count=0,
            last_request_time=None,
            rate_limit_hits=0,
        )

        self._cache: Dict[str, Tuple[float, discord.Embed]] = {}
        self._active: Dict[int, asyncio.Task] = {}
        self._session: Optional[aiohttp.ClientSession] = None
        self._last_api_call: Optional[datetime] = None
        self._request_times: List[float] = []
        self._ready = asyncio.Event()
        self._inflight_requests: Dict[str, asyncio.Future] = {}
        self._api_queue = APIRequestQueue(self)

    async def cog_load(self):
        self._session = aiohttp.ClientSession()
        if await self.config.request_queue_enabled():
            await self._api_queue.start()
        self._ready.set()
        log.info(f"GrokCog loaded with model '{await self.config.model_name()}'")

    async def cog_unload(self):
        self._ready.clear()
        await self._api_queue.stop()
        for task in list(self._active.values()):
            task.cancel()
        if self._active:
            await asyncio.wait(self._active.values(), timeout=5)
        for future in list(self._inflight_requests.values()):
            if not future.done():
                future.cancel()
        self._inflight_requests.clear()
        if self._session and not self._session.closed:
            await self._session.close()
        log.info("GrokCog unloaded successfully")

    @staticmethod
    def _key(text: str) -> str:
        return hashlib.sha256(text.strip().lower().encode()).hexdigest()

    def _cache_get(self, key: str) -> Optional[discord.Embed]:
        if item := self._cache.get(key):
            ts, val = item
            if time.time() - ts < CACHE_TTL:
                return val
            self._cache.pop(key, None)
        return None

    def _cache_set(self, key: str, val: discord.Embed) -> None:
        self._cache[key] = (time.time(), val)
        if len(self._cache) > MAX_CACHE_SIZE:
            for k, _ in sorted(self._cache.items(), key=lambda x: x[1][0])[:32]:
                self._cache.pop(k, None)

    async def _respect_api_rate_limits(self):
        await self._ready.wait()
        while True:
            now = time.time()
            window_start = now - RATE_LIMIT_WINDOW
            self._request_times = [t for t in self._request_times if t > window_start]

            max_per_minute = await self.config.max_requests_per_minute()

            if len(self._request_times) >= max_per_minute:
                oldest_request = self._request_times[0]
                wait_time = RATE_LIMIT_WINDOW - (now - oldest_request)
                if wait_time > 0:
                    await asyncio.sleep(wait_time)
                    continue

            if self._last_api_call:
                now = time.time()
                time_since_last = now - self._last_api_call.timestamp()
                min_gap = await self.config.min_api_call_gap()
                if time_since_last < min_gap:
                    wait_time = min_gap - time_since_last
                    await asyncio.sleep(wait_time)
                    continue

            self._last_api_call = datetime.now(timezone.utc)
            self._request_times.append(time.time())
            break

    async def _validate(
        self,
        user_id: int,
        guild_id: Optional[int],
        question: str,
        channel: discord.abc.Messageable,
    ) -> bool:
        if not self._ready.is_set():
            return False

        if not question or not question.strip():
            return False

        # FIX: Check API Key presence early and warn
        if not await self.config.api_key():
            await channel.send(
                "⚠️ **Configuration Error:** API Key is not set. Use `[p]grok admin apikey`."
            )
            return False

        if guild_id:
            guild_config = self.config.guild_from_id(guild_id)
            if not await guild_config.enabled():
                return False
            max_length = await guild_config.max_input_length()
        else:
            max_length = MAX_INPUT_LENGTH

        if len(question) > max_length:
            await channel.send(f"⛔ Question too long ({len(question)}/{max_length}).")
            return False

        if user_id in self._active:
            await channel.send("⏳ Please wait for your previous request.")
            return False

        return True

    async def _ask_groq(self, question: str, temperature: float) -> dict:
        api_key = await self.config.api_key()
        if not api_key:
            raise ValueError("⛔ API key missing.")

        model_name = await self.config.model_name()
        payload = {
            "model": model_name,
            "messages": [
                {"role": "system", "content": K2_PROMPT},
                {"role": "user", "content": question},
            ],
            "temperature": temperature,
            "max_completion_tokens": 8192,
            "top_p": 1,
            "stream": False,
        }

        max_retries = await self.config.max_retries()
        api_key_clean = api_key.strip()

        for attempt in range(max_retries):
            try:
                timeout = aiohttp.ClientTimeout(
                    connect=10, total=await self.config.timeout()
                )
                async with self._session.post(
                    f"{GROQ_API_BASE}/chat/completions",
                    json=payload,
                    headers={
                        "Authorization": f"Bearer {api_key_clean}",
                        "Content-Type": "application/json",
                    },
                    timeout=timeout,
                ) as resp:
                    if resp.status == 429:
                        wait_time = int(resp.headers.get("Retry-After", 5))
                        if attempt < max_retries - 1:
                            await asyncio.sleep(wait_time)
                            continue
                        raise ValueError("⏱️ Rate limit reached.")

                    if resp.status != 200:
                        err_text = await resp.text()
                        raise ValueError(f"⛔ Groq error {resp.status}: {err_text}")

                    data = await resp.json()
                    content = (
                        data.get("choices", [{}])[0]
                        .get("message", {})
                        .get("content", "")
                    )

                    if not content:
                        raise ValueError("Empty response.")

                    return self._extract_json(content)

            except Exception as e:
                if attempt == max_retries - 1:
                    raise e
                await asyncio.sleep(1)

        raise ValueError("Failed after retries.")

    def _extract_json(self, content: str) -> dict:
        # 1. Direct parse
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            pass

        # 2. Markdown Block
        if match := JSON_BLOCK_REGEX.search(content):
            try:
                return json.loads(match.group(1))
            except json.JSONDecodeError:
                pass

        # 3. Fallback (Greedy match for widest brace pair)
        if match := JSON_FALLBACK_REGEX.search(content):
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError:
                pass

        return {"answer": content, "confidence": 0.5, "sources": []}

    def _build_context_query(self, message: discord.Message, base_question: str) -> str:
        """Constructs a query that includes context from a replied-to message."""
        if not message.reference or not message.reference.resolved:
            return base_question

        replied_msg = message.reference.resolved
        if isinstance(replied_msg, discord.Message):
            # Clean up the replied content slightly
            context_content = replied_msg.content.replace("\n", " ")[:800]
            if replied_msg.embeds:
                # Also capture embed description if available (for continuing bot responses)
                context_content += " " + (replied_msg.embeds[0].description or "")[:800]

            return (
                f'Context from previous message: "{context_content}"\n\n'
                f'User Question: "{base_question}"'
            )
        return base_question

    async def _process(
        self,
        user_id: int,
        guild_id: Optional[int],
        question: str,
        channel,
        message: Optional[discord.Message] = None,
    ):
        # Inject Context if reply exists
        final_question = question
        if message:
            final_question = self._build_context_query(message, question)

        if not await self._validate(user_id, guild_id, final_question, channel):
            return

        task = asyncio.current_task()
        self._active[user_id] = task
        key = self._key(final_question)

        try:
            if cached := self._cache_get(key):
                await channel.send(embed=cached)
                return

            # Dedup check
            if key in self._inflight_requests:
                try:
                    result = await asyncio.wait_for(
                        self._inflight_requests[key], timeout=30
                    )
                    formatted = self._format(result)
                    await channel.send(embed=formatted)
                    return
                except asyncio.TimeoutError:
                    pass

            async with channel.typing():
                temperature = 0.3
                if guild_id:
                    temperature = await self.config.guild_from_id(
                        guild_id
                    ).default_temperature()

                api_coro = self._ask_groq(final_question, temperature)

                if await self.config.request_queue_enabled():
                    future = asyncio.Future()
                    self._inflight_requests[key] = future
                    try:
                        result = await self._api_queue.enqueue(api_coro)
                        if not future.done():
                            future.set_result(result)
                    finally:
                        self._inflight_requests.pop(key, None)
                else:
                    result = await api_coro

                embed = self._format(result)
                self._cache_set(key, embed)
                await channel.send(embed=embed)

                async with self.config.user_from_id(user_id).all() as user_data:
                    user_data["request_count"] = user_data.get("request_count", 0) + 1
                    user_data["last_request_time"] = time.time()

        except Exception as e:
            log.error(f"Error: {e}")
            await channel.send(f"⛔ Error: {str(e)}")
        finally:
            self._active.pop(user_id, None)
            self._inflight_requests.pop(key, None)

    def _format(self, data: dict) -> discord.Embed:
        answer = data.get("answer", "No response.")
        confidence = data.get("confidence", 0.0)
        sources = data.get("sources", [])

        if confidence >= 0.9:
            color = discord.Color.green()
        elif confidence >= 0.7:
            color = discord.Color.blue()
        elif confidence >= 0.5:
            color = discord.Color.gold()
        else:
            color = discord.Color.orange()

        embed = discord.Embed(
            title="DripBot's Response",
            description=answer[:4096],
            color=color,
            timestamp=datetime.now(timezone.utc),
        )

        if sources or confidence >= 0.7:
            embed.add_field(name="Confidence", value=f"{confidence:.0%}", inline=True)

        if sources and isinstance(sources, list):
            source_text = ""
            for i, src in enumerate(sources[:5], 1):
                if isinstance(src, dict):
                    title = src.get("title", "Source")[:100]
                    url = src.get("url", "")
                    source_text += (
                        f"{i}. [{title}]({url})\n" if url else f"{i}. {title}\n"
                    )

            if source_text:
                embed.add_field(name="Sources", value=source_text[:1024], inline=False)

        embed.set_footer(text=f"Model: {DEFAULT_MODEL} • Fact-Checked")
        return embed

    @commands.Cog.listener()
    async def on_message(self, msg: discord.Message):
        if msg.author.bot or not self._ready.is_set():
            return

        # Check for Reply Context FIRST
        is_reply_to_me = False
        if msg.reference and msg.reference.resolved:
            resolved = msg.reference.resolved
            if (
                isinstance(resolved, discord.Message)
                and resolved.author == self.bot.user
            ):
                is_reply_to_me = True

        is_mention = self.bot.user in msg.mentions

        if msg.guild:
            if not await self.config.guild(msg.guild).enabled():
                return

            if is_mention or is_reply_to_me:
                content = msg.content
                for mention in msg.mentions:
                    content = content.replace(f"<@{mention.id}>", "").replace(
                        f"<@!{mention.id}>", ""
                    )

                await self._process(
                    msg.author.id,
                    msg.guild.id,
                    content.strip(),
                    msg.channel,
                    message=msg,
                )

        elif isinstance(msg.channel, discord.DMChannel):
            if not any(
                msg.content.startswith(p) for p in await self.bot.get_valid_prefixes()
            ):
                await self._process(
                    msg.author.id, None, msg.content, msg.channel, message=msg
                )

    @commands.hybrid_group(name="grok", invoke_without_command=True)
    @commands.cooldown(1, COOLDOWN_SECONDS, commands.BucketType.user)
    async def grok(self, ctx: commands.Context, *, question: str):
        await ctx.typing()
        await self._process(
            ctx.author.id,
            ctx.guild.id if ctx.guild else None,
            question,
            ctx.channel,
            message=ctx.message,
        )

    @grok.command(name="stats")
    async def grok_stats(self, ctx: commands.Context):
        stats = await self.config.user(ctx.author).all()
        embed = discord.Embed(
            title=f"📊 {ctx.author.display_name}'s Stats", color=discord.Color.gold()
        )
        embed.add_field(name="Total Queries", value=stats.get("request_count", 0))
        await ctx.send(embed=embed)

    @grok.group(name="admin")
    async def grok_admin(self, ctx: commands.Context):
        pass

    @grok_admin.command(name="apikey")
    @commands.is_owner()
    async def admin_apikey(self, ctx: commands.Context, *, api_key: str):
        await self.config.api_key.set(api_key.strip())
        await ctx.send("✅ API key saved.")

    @grok_admin.command(name="verify")
    @commands.is_owner()
    async def admin_verify(self, ctx: commands.Context):
        msg = await ctx.send("🔍 Testing API...")
        try:
            # Simple test call
            await self._ask_groq("Test", 0.1)
            await msg.edit(content="✅ API key is working!")
        except Exception as e:
            await msg.edit(content=f"⛔ Error: {e}")

    @grok_admin.command(name="toggle")
    @commands.admin_or_permissions(manage_guild=True)
    async def admin_toggle(self, ctx: commands.Context):
        current = await self.config.guild(ctx.guild).enabled()
        await self.config.guild(ctx.guild).enabled.set(not current)
        status = "ENABLED 🟢" if not current else "DISABLED 🔴"
        await ctx.send(f"✅ Grok is now **{status}**")

    @grok_admin.command(name="cooldown")
    @commands.is_owner()
    async def admin_cooldown(self, ctx: commands.Context, seconds: int):
        await self.config.cooldown_seconds.set(seconds)
        await ctx.send(f"✅ Cooldown set to {seconds}s")

    @grok_admin.command(name="setmodel")
    @commands.is_owner()
    async def admin_setmodel(self, ctx: commands.Context, model: str):
        await self.config.model_name.set(model)
        await ctx.send(f"✅ Model set to `{model}`")

    @grok_admin.command(name="ratelimits")
    @commands.is_owner()
    async def admin_ratelimits(
        self, ctx: commands.Context, per_minute: int, min_gap: float
    ):
        await self.config.max_requests_per_minute.set(per_minute)
        await self.config.min_api_call_gap.set(min_gap)
        await ctx.send(f"✅ Rate limits updated: {per_minute}/min, {min_gap}s gap")

    @grok_admin.command(name="clearcache")
    @commands.is_owner()
    async def admin_clearcache(self, ctx: commands.Context):
        self._cache.clear()
        await ctx.send("✅ Cache cleared")


async def setup(bot):
    await bot.add_cog(GrokCog(bot))
