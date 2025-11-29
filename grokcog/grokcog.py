import asyncio
import hashlib
import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import aiohttp
import discord
from redbot.core import Config, commands
from redbot.core.utils.chat_formatting import pagify

log = logging.getLogger("red.grokcog")

GROQ_API_BASE = "https://api.groq.com/openai/v1"
DEFAULT_MODEL = "moonshotai/kimi-k2-instruct-0905"
K2_PROMPT = """You are a fact-based AI assistant that ONLY provides information you can verify or cite from credible sources.

CRITICAL RULES:
1. NEVER make up information, facts, statistics, or sources
2. If you don't have verified information, say "I don't have verified information about this"
3. ONLY cite sources you actually have access to - never fabricate URLs or titles
4. Distinguish clearly between verified facts and general knowledge
5. When uncertain, explicitly state your uncertainty level

Response Format (valid JSON only, no markdown):
{
  "answer": "Your factual answer here.\n\nUse verified information only. If making claims, cite sources [1]. If you cannot verify something, say so explicitly.\n\nDo NOT make up facts, statistics, or sources.",
  "confidence": 0.85,
  "sources": [
    {"title": "Real source title only", "url": "https://actual-verifiable-url.com"}
  ]
}

Confidence Guidelines:
- 0.9-1.0: Directly cited from verified sources
- 0.7-0.89: Well-established facts from general knowledge
- 0.5-0.69: General knowledge with some uncertainty
- Below 0.5: Speculation or uncertain information

When You Cannot Verify:
- State: "I don't have verified information about [topic]"
- Explain what you DO know that's related
- Suggest what type of source would have this information
- NEVER guess or make assumptions presented as facts

Source Citation Rules:
- Only include sources in the "sources" array if you actually have them
- If you don't have sources, use empty array: "sources": []
- Never fabricate URLs, publication names, or dates
- Better to have no sources than fake sources"""

COOLDOWN_SECONDS = 30
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
        self.queue = asyncio.Queue()
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
                    result = await coro
                    if not future.done():
                        future.set_result(result)
                except Exception as e:
                    if not future.done():
                        future.set_exception(e)
                await asyncio.sleep(0.5)
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.exception("Worker error")
                if "future" in locals() and not future.done():
                    future.set_exception(e)


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
        log.info(
            f"GrokCog loaded with model '{await self.config.model_name()}', "
            f"max {await self.config.max_requests_per_minute()} req/min"
        )

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
                log.debug(f"Cache hit for key {key[:8]}...")
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
        now = time.time()
        window_start = now - RATE_LIMIT_WINDOW
        self._request_times = [t for t in self._request_times if t > window_start]
        max_per_minute = await self.config.max_requests_per_minute()
        if len(self._request_times) >= max_per_minute:
            oldest_request = self._request_times[0]
            wait_time = RATE_LIMIT_WINDOW - (now - oldest_request)
            log.warning(
                f"Global rate limit reached: {len(self._request_times)}/{max_per_minute} "
                f"requests in last {RATE_LIMIT_WINDOW}s. Waiting {wait_time:.1f}s"
            )
            if wait_time > 0:
                await asyncio.sleep(wait_time)
                return await self._respect_api_rate_limits()
        if self._last_api_call:
            time_since_last = now - self._last_api_call.timestamp()
            min_gap = await self.config.min_api_call_gap()
            if time_since_last < min_gap:
                wait_time = min_gap - time_since_last
                log.debug(f"Enforcing min gap: waiting {wait_time:.1f}s")
                await asyncio.sleep(wait_time)
        self._last_api_call = datetime.now(timezone.utc)
        self._request_times.append(now)

    async def _validate(
        self,
        user_id: int,
        guild_id: Optional[int],
        question: str,
        channel: discord.abc.Messageable,
    ) -> bool:
        if not self._ready.is_set():
            await channel.send("⚠️ Bot is still starting. Please wait 10 seconds.")
            return False
        if not question or not question.strip():
            await channel.send("❌ Please provide a question.")
            return False
        if guild_id:
            guild_config = self.config.guild_from_id(guild_id)
            if not await guild_config.enabled():
                await channel.send("❌ Grok is disabled in this server.")
                return False
            max_length = await guild_config.max_input_length()
        else:
            max_length = MAX_INPUT_LENGTH
        if len(question) > max_length:
            await channel.send(
                f"❌ Question too long ({len(question)}/{max_length} characters). "
                "Please shorten it."
            )
            return False
        if user_id in self._active:
            await channel.send("⏳ You already have a request processing. Please wait.")
            return False
        user_data = await self.config.user_from_id(user_id).all()
        last_request = user_data.get("last_request_time")
        if last_request:
            try:
                if isinstance(last_request, (int, float)):
                    last_time = datetime.fromtimestamp(last_request, timezone.utc)
                else:
                    last_time = datetime.fromisoformat(last_request)
                time_since = (datetime.now(timezone.utc) - last_time).total_seconds()
                cooldown = await self.config.cooldown_seconds()
                if time_since < cooldown:
                    remaining = cooldown - time_since
                    await channel.send(
                        f"⏱️ Please wait {remaining:.1f} more seconds before asking again."
                    )
                    return False
            except Exception as e:
                log.warning(f"Error checking user cooldown: {e}")
        return True

    async def _ask_groq(self, question: str, temperature: float) -> dict:
        api_key = await self.config.api_key()
        if not api_key:
            raise ValueError(
                "❌ **Groq API key not configured!**\n\n"
                "Please set your key with: `[p]grok admin apikey <key>`\n"
                "Get your free key from: https://console.groq.com/keys"
            )
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
                log.info(
                    f"Groq API Request: model={model_name}, attempt={attempt + 1}, "
                    f"question_len={len(question)}"
                )
                timeout = aiohttp.ClientTimeout(
                    connect=10,
                    total=await self.config.timeout(),
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
                    log.info(f"Groq API Response: HTTP {resp.status}")
                    if resp.status == 429:
                        retry_after = resp.headers.get("Retry-After")
                        wait_time = (
                            int(retry_after) if retry_after else min(2**attempt, 30)
                        )
                        log.warning(
                            f"Rate limited (429). Waiting {wait_time}s "
                            f"(attempt {attempt + 1}/{max_retries})"
                        )
                        if attempt < max_retries - 1:
                            await asyncio.sleep(wait_time)
                            continue
                        else:
                            raise ValueError(
                                f"⏱️ **Rate limit reached**\n\n"
                                f"Groq allows {await self.config.max_requests_per_minute()} "
                                f"requests per minute.\n\n"
                                "**Solutions:**\n"
                                "1. Wait 60 seconds before asking again\n"
                                "2. Ask your server admin to increase cooldown with `[p]grok admin cooldown 60`\n"
                                "3. Upgrade at: https://console.groq.com/settings/limits"
                            )
                    elif resp.status == 401:
                        log.error("401 Unauthorized - Invalid API key")
                        raise ValueError(
                            "❌ **401 Unauthorized** - Your Groq API key is invalid.\n\n"
                            "Get a new key at: https://console.groq.com/keys\n"
                            "Then set it with: `[p]grok admin apikey <key>`"
                        )
                    elif resp.status == 400:
                        error_data = await resp.json()
                        error_msg = error_data.get("error", {}).get(
                            "message", "Unknown error"
                        )
                        log.error(f"400 Bad Request: {error_msg}")
                        raise ValueError(f"❌ **Bad Request**: {error_msg}")
                    elif resp.status >= 500:
                        log.error(f"Server error {resp.status}")
                        if attempt < max_retries - 1:
                            await asyncio.sleep(min(2**attempt, 10))
                            continue
                        raise ValueError(
                            f"❌ Groq server error (HTTP {resp.status}). Please try again later."
                        )
                    resp.raise_for_status()
                    data = await resp.json()
                    content = (
                        data.get("choices", [{}])[0]
                        .get("message", {})
                        .get("content", "")
                    )
                    if not content:
                        raise ValueError("Empty response from Groq API")
                    return self._extract_json(content)
            except aiohttp.ClientError as e:
                log.error(f"Connection error: {e}")
                if attempt == max_retries - 1:
                    raise ValueError(f"❌ Connection failed: {str(e)}")
                await asyncio.sleep(min(2**attempt, 15))
            except Exception as e:
                log.exception(f"Unexpected error on attempt {attempt + 1}")
                if attempt == max_retries - 1:
                    raise
                await asyncio.sleep(min(2**attempt, 15))
        raise ValueError("❌ Failed after all retry attempts")

    def _extract_json(self, content: str) -> dict:
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            pass
        match = re.search(r"``````", content, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(1))
            except json.JSONDecodeError:
                pass
        match = re.search(r"\{.*?\}", content, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError:
                pass
        log.warning(
            f"Could not parse JSON, using fallback. Content: {content[:100]}..."
        )
        return {"answer": content, "confidence": 0.5, "sources": []}

    async def _process(
        self, user_id: int, guild_id: Optional[int], question: str, channel
    ):
        if not await self._validate(user_id, guild_id, question, channel):
            return
        task = asyncio.current_task()
        self._active[user_id] = task
        key = self._key(question)
        try:
            if cached := self._cache_get(key):
                log.debug(f"Cache hit for user {user_id}")
                await channel.send(embed=cached)
                return
            if key in self._inflight_requests:
                log.info(
                    f"Request deduplication: user {user_id} waiting for existing request"
                )
                try:
                    result = await asyncio.wait_for(
                        self._inflight_requests[key], timeout=30
                    )
                    formatted = self._format(result)
                    self._cache_set(key, formatted)
                    await channel.send(embed=formatted)
                    return
                except asyncio.TimeoutError:
                    log.warning("Deduplication wait timed out, making new request")
            async with channel.typing():
                temperature = 0.3
                if guild_id:
                    temp_config = await self.config.guild_from_id(
                        guild_id
                    ).default_temperature()
                    temperature = temp_config
                api_coro = self._ask_groq(question, temperature)
                if await self.config.request_queue_enabled():
                    future = asyncio.Future()
                    self._inflight_requests[key] = future
                    try:
                        result = await self._api_queue.enqueue(api_coro)
                        if not future.done():
                            future.set_result(result)
                    except Exception as e:
                        if not future.done():
                            future.set_exception(e)
                        raise
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
            log.exception(f"Query failed for user {user_id}")
            await channel.send(
                str(e) if isinstance(e, ValueError) else "❌ Unexpected error"
            )
        finally:
            self._active.pop(user_id, None)
            self._inflight_requests.pop(key, None)

    def _format(self, data: dict) -> discord.Embed:
        if not isinstance(data, dict):
            embed = discord.Embed(
                title="❌ Error",
                description=f"Invalid response type: {type(data)}",
                color=discord.Color.red(),
            )
            return embed

        answer = data.get("answer", "")
        if not answer:
            embed = discord.Embed(
                title="❌ No Response",
                description="No answer received from AI",
                color=discord.Color.red(),
            )
            return embed

        confidence = data.get("confidence", 0.0)
        sources = data.get("sources", [])

        if confidence >= 0.9:
            color = discord.Color.green()
            conf_emoji = ""
        elif confidence >= 0.7:
            color = discord.Color.blue()
            conf_emoji = ""
        elif confidence >= 0.5:
            color = discord.Color.gold()
            conf_emoji = ""
        else:
            color = discord.Color.orange()
            conf_emoji = ""

        embed = discord.Embed(
            title="DripBot's Response",
            description=answer[:4096],
            color=color,
            timestamp=datetime.now(timezone.utc),
        )

        embed.add_field(
            name=f"{conf_emoji} Confidence", value=f"{confidence:.0%}", inline=True
        )

        if sources and isinstance(sources, list):
            source_text = ""
            for i, src in enumerate(sources[:5], 1):
                if isinstance(src, dict):
                    title = src.get("title", "Source")[:100]
                    url = src.get("url", "")
                    if url:
                        source_text += f"{i}. [{title}]({url})\n"
                    else:
                        source_text += f"{i}. {title}\n"
            if source_text:
                embed.add_field(name="Sources", value=source_text[:1024], inline=False)

        embed.set_footer(
            text="Powered by 2 Romanian kids • Retardation only (fact checks)"
        )

        return embed

    @commands.Cog.listener()
    async def on_message(self, msg: discord.Message):
        if msg.author.bot or not self._ready.is_set():
            return
        if not await self.config.api_key():
            return
        if msg.guild and self.bot.user in msg.mentions:
            guild_config = self.config.guild(msg.guild)
            if not await guild_config.enabled():
                return
            content = msg.content
            for mention in msg.mentions:
                content = content.replace(f"<@{mention.id}>", "").replace(
                    f"<@!{mention.id}>", ""
                )
            question = content.strip()
            if msg.reference and (replied := msg.reference.resolved):
                if isinstance(replied, discord.Message):
                    question += f"\n\nContext: {replied.content[:500]}"
            if question:
                await self._process(msg.author.id, msg.guild.id, question, msg.channel)
        elif isinstance(msg.channel, discord.DMChannel):
            prefixes = await self.bot.get_valid_prefixes()
            if any(msg.content.startswith(prefix) for prefix in prefixes):
                return
            await self._process(msg.author.id, None, msg.content, msg.channel)

    @commands.hybrid_group(name="grok", invoke_without_command=True)
    @commands.cooldown(1, COOLDOWN_SECONDS, commands.BucketType.user)
    async def grok(self, ctx: commands.Context, *, question: str):
        await ctx.typing()
        await self._process(
            ctx.author.id, ctx.guild.id if ctx.guild else None, question, ctx.channel
        )

    @grok.command(name="stats")
    async def grok_stats(self, ctx: commands.Context):
        stats = await self.config.user(ctx.author).all()
        embed = discord.Embed(
            title=f"📊 {ctx.author.display_name}'s Grok Stats",
            color=discord.Color.gold(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(
            name="Total Queries", value=stats.get("request_count", 0), inline=True
        )
        if stats.get("last_request_time"):
            embed.add_field(
                name="Last Query",
                value=f"<t:{int(stats['last_request_time'])}:R>",
                inline=True,
            )
        embed.add_field(
            name="Rate Limit Hits", value=stats.get("rate_limit_hits", 0), inline=True
        )
        await ctx.send(embed=embed)

    @grok.group(name="admin")
    async def grok_admin(self, ctx: commands.Context):
        pass

    @grok_admin.command(name="apikey")
    @commands.is_owner()
    async def admin_apikey(self, ctx: commands.Context, *, api_key: str):
        api_key = api_key.strip()
        if len(api_key) < 32:
            await ctx.send("❌ Invalid API key format (too short)")
            return
        await self.config.api_key.set(api_key)
        await ctx.send("✅ API key saved. Use `[p]grok admin verify` to test it.")

    @grok_admin.command(name="verify")
    @commands.is_owner()
    async def admin_verify(self, ctx: commands.Context):
        msg = await ctx.send("🔍 Testing API connection...")
        try:
            test_payload = {
                "model": await self.config.model_name(),
                "messages": [{"role": "user", "content": "Test"}],
                "max_completion_tokens": 5,
                "temperature": 0,
            }
            api_key = await self.config.api_key()
            async with self._session.post(
                f"{GROQ_API_BASE}/chat/completions",
                json=test_payload,
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                data = await resp.json()
                if resp.status == 200:
                    await msg.edit(content="✅ API key is working!")
                    embed = discord.Embed(
                        title="API Key Information",
                        color=discord.Color.green(),
                        timestamp=datetime.now(timezone.utc),
                    )
                    embed.add_field(
                        name="Model Access",
                        value=f"`{data.get('model', 'unknown')}`",
                        inline=True,
                    )
                    usage = data.get("usage", {})
                    embed.add_field(
                        name="Tokens Used",
                        value=f"{usage.get('total_tokens', 'unknown')}",
                        inline=True,
                    )
                    await ctx.send(embed=embed)
                else:
                    error_msg = {
                        401: "❌ Invalid API key",
                        403: "❌ No access to this model",
                        429: "⚠️ Rate limited (key is valid but busy)",
                    }
                    await msg.edit(
                        content=error_msg.get(resp.status, f"⚠️ HTTP {resp.status}")
                    )
        except Exception as e:
            log.exception("API verification failed")
            await msg.edit(content=f"❌ Error: {str(e)}")

    @grok_admin.command(name="toggle")
    @commands.admin_or_permissions(manage_guild=True)
    async def admin_toggle(self, ctx: commands.Context):
        current = await self.config.guild(ctx.guild).enabled()
        await self.config.guild(ctx.guild).enabled.set(not current)
        status = "ENABLED 🟢" if not current else "DISABLED 🔴"
        await ctx.send(f"✅ Grok is now **{status}** in this server")

    @grok_admin.command(name="cooldown")
    @commands.is_owner()
    async def admin_cooldown(self, ctx: commands.Context, seconds: int):
        if seconds < 5:
            await ctx.send("❌ Cooldown must be at least 5 seconds")
            return
        await self.config.cooldown_seconds.set(seconds)
        self.grok._buckets._cooldown = commands.Cooldown(
            1, seconds, commands.BucketType.user
        )
        await ctx.send(f"✅ Cooldown set to {seconds} seconds per user")

    @grok_admin.command(name="setmodel")
    @commands.is_owner()
    async def admin_setmodel(self, ctx: commands.Context, model: str):
        valid_models = [
            "moonshotai/kimi-k2-instruct-0905",
            "llama-3.3-70b-versatile",
            "llama-3.1-70b-versatile",
            "deepseek-r1-distill-llama-70b",
        ]
        if model not in valid_models:
            await ctx.send(
                f"❌ Invalid model. Choose from:\n"
                + "\n".join(f"• `{m}`" for m in valid_models)
            )
            return
        await self.config.model_name.set(model)
        await ctx.send(f"✅ Model set to `{model}`")

    @grok_admin.command(name="ratelimits")
    @commands.is_owner()
    async def admin_ratelimits(
        self, ctx: commands.Context, per_minute: int, min_gap: float
    ):
        if per_minute < 1:
            await ctx.send("❌ Must allow at least 1 request per minute")
            return
        if min_gap < 0.1:
            await ctx.send("❌ Minimum gap must be at least 0.1 seconds")
            return
        await self.config.max_requests_per_minute.set(per_minute)
        await self.config.min_api_call_gap.set(min_gap)
        await ctx.send(
            f"✅ Rate limits updated:\n"
            f"• Max requests: {per_minute}/minute\n"
            f"• Min gap between calls: {min_gap}s"
        )

    @grok_admin.command(name="diagnose")
    @commands.is_owner()
    async def admin_diagnose(self, ctx: commands.Context):
        await ctx.typing()
        embed = discord.Embed(
            title="🛠️ GrokCog Diagnostics",
            color=discord.Color.blue(),
            timestamp=datetime.now(timezone.utc),
        )
        api_key = await self.config.api_key()
        embed.add_field(
            name="API Key Set", value="✅ Yes" if api_key else "❌ No", inline=True
        )
        model = await self.config.model_name()
        embed.add_field(name="Model", value=f"`{model}`", inline=True)
        recent_reqs = len([t for t in self._request_times if time.time() - t < 60])
        max_reqs = await self.config.max_requests_per_minute()
        embed.add_field(
            name="Recent Requests",
            value=f"{recent_reqs}/{max_reqs} per minute",
            inline=True,
        )
        embed.add_field(name="Active Tasks", value=len(self._active), inline=True)
        embed.add_field(name="Cache Size", value=len(self._cache), inline=True)
        queue_enabled = await self.config.request_queue_enabled()
        embed.add_field(
            name="Request Queue",
            value="✅ Enabled" if queue_enabled else "❌ Disabled",
            inline=True,
        )
        if self._last_api_call:
            time_since = (
                datetime.now(timezone.utc) - self._last_api_call
            ).total_seconds()
            embed.add_field(
                name="Last API Call", value=f"{time_since:.1f}s ago", inline=True
            )
        cooldown = await self.config.cooldown_seconds()
        min_gap = await self.config.min_api_call_gap()
        embed.add_field(name="User Cooldown", value=f"{cooldown}s", inline=True)
        embed.add_field(name="Min API Gap", value=f"{min_gap}s", inline=True)
        await ctx.send(embed=embed)

    @grok_admin.command(name="clearcache")
    @commands.is_owner()
    async def admin_clearcache(self, ctx: commands.Context):
        self._cache.clear()
        await ctx.send("✅ Cache cleared")


async def setup(bot):
    await bot.add_cog(GrokCog(bot))
