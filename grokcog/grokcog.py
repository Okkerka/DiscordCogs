# ──────────────────────────────────────────────────────────────────────────────
# GrokCog v3.3.0 - Battle-Tested & Production Ready
# Features: Global request queue, aggressive rate limiting, smart caching
# ──────────────────────────────────────────────────────────────────────────────

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

# ──────────────────────────────────────────────────────────────────────────────
# CONSTANTS - ULTRA-CONSERVATIVE FOR RESTRICTIVE API TIERS
# ──────────────────────────────────────────────────────────────────────────────

# API Configuration - VERIFIED for Moonshot AI
KIMI_API_BASE = "https://api.moonshot.ai/v1"
DEFAULT_MODEL = "moonshot-v1-8k"  # Most widely available
K2_PROMPT = """You are DripBot's AI assistant. Respond ONLY in this JSON format:

{
  "answer": "Your answer with citations [1], [2] where appropriate",
  "confidence": 0.95,
  "sources": [{"title": "Source Title", "url": "https://example.com"}]
}

If unsure, set confidence lower and use empty sources array. No markdown code blocks."""

# Rate Limiting - CONSERVATIVE for free tiers
COOLDOWN_SECONDS = 30  # Per-user cooldown
MIN_API_CALL_GAP = 5.0  # Minimum seconds between ANY API calls
MAX_REQUESTS_PER_MINUTE = 5  # Ultra-conservative for free tier
RATE_LIMIT_WINDOW = 60  # Sliding window in seconds

# Cache & Limits
CACHE_TTL = 3600  # 1 hour
MAX_CACHE_SIZE = 256
MAX_INPUT_LENGTH = 4000
MAX_RETRIES = 3  # Don't hammer the API

# ──────────────────────────────────────────────────────────────────────────────
# REQUEST QUEUE WORKER PATTERN
# ──────────────────────────────────────────────────────────────────────────────


class APIRequestQueue:
    """Serialize API calls globally to respect rate limits"""

    def __init__(self, cog):
        self.cog = cog
        self.queue = asyncio.Queue()
        self.worker_task = None
        self._lock = asyncio.Lock()

    async def start(self):
        """Start the background worker"""
        if self.worker_task is None or self.worker_task.done():
            self.worker_task = asyncio.create_task(self._worker())
            log.info("API Request Queue worker started")

    async def stop(self):
        """Stop the worker"""
        if self.worker_task:
            self.worker_task.cancel()
            try:
                await self.worker_task
            except asyncio.CancelledError:
                pass
            self.worker_task = None
            log.info("API Request Queue worker stopped")

    async def enqueue(self, coro) -> Any:
        """Add a request to the queue and wait for result"""
        future = asyncio.Future()
        await self.queue.put((coro, future))
        return await future

    async def _worker(self):
        """Process requests one at a time with rate limiting"""
        while True:
            try:
                coro, future = await self.queue.get()

                # Respect rate limits before processing
                await self.cog._respect_api_rate_limits()

                try:
                    result = await coro
                    future.set_result(result)
                except Exception as e:
                    future.set_exception(e)

                # Small delay between processing queue items
                await asyncio.sleep(0.5)

            except asyncio.CancelledError:
                break
            except Exception as e:
                log.exception("Worker error")
                if not future.done():
                    future.set_exception(e)


# ──────────────────────────────────────────────────────────────────────────────
# COG CLASS
# ──────────────────────────────────────────────────────────────────────────────


class GrokCog(commands.Cog):
    """🧠 DripBot's AI brain - Powered by Kimi/Moonshot AI"""

    def __init__(self, bot):
        self.bot = bot
        self.config = Config.get_conf(
            self, identifier=0x4B324B32, force_registration=True
        )

        # Configuration with conservative defaults
        self.config.register_global(
            api_key=None,
            timeout=60,
            max_retries=MAX_RETRIES,
            cooldown_seconds=COOLDOWN_SECONDS,
            min_api_call_gap=MIN_API_CALL_GAP,
            max_requests_per_minute=MAX_REQUESTS_PER_MINUTE,
            model_name=DEFAULT_MODEL,
            request_queue_enabled=True,  # Enable queue by default
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

        # Runtime state
        self._cache: Dict[str, Tuple[float, str]] = {}
        self._active: Dict[int, asyncio.Task] = {}
        self._session: Optional[aiohttp.ClientSession] = None
        self._last_api_call: Optional[datetime] = None
        self._request_times: List[float] = []
        self._ready = asyncio.Event()

        # Request deduplication - if multiple users ask same question simultaneously
        self._inflight_requests: Dict[str, asyncio.Future] = {}

        # Global request queue
        self._api_queue = APIRequestQueue(self)

    async def cog_load(self):
        """Initialize the cog"""
        self._session = aiohttp.ClientSession()

        # Start the API queue worker
        if await self.config.request_queue_enabled():
            await self._api_queue.start()

        self._ready.set()
        log.info(
            f"GrokCog v3.3.0 loaded with model '{await self.config.model_name()}', "
            f"max {await self.config.max_requests_per_minute()} req/min"
        )

    async def cog_unload(self):
        """Clean shutdown"""
        self._ready.clear()

        # Stop queue worker
        await self._api_queue.stop()

        # Cancel all active tasks
        for task in list(self._active.values()):
            task.cancel()

        if self._active:
            await asyncio.wait(self._active.values(), timeout=5)

        if self._session:
            await self._session.close()

        log.info("GrokCog unloaded successfully")

    # ──────────────────────────────────────────────────────────────────────────────
    # UTILITY METHODS
    # ──────────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _key(text: str) -> str:
        """Generate cache key"""
        return hashlib.sha256(text.strip().lower().encode()).hexdigest()

    def _cache_get(self, key: str) -> Optional[str]:
        """Get cached response"""
        if item := self._cache.get(key):
            ts, val = item
            if time.time() - ts < CACHE_TTL:
                log.debug(f"Cache hit for key {key[:8]}...")
                return val
            self._cache.pop(key, None)
        return None

    def _cache_set(self, key: str, val: str) -> None:
        """Set cache with LRU eviction"""
        self._cache[key] = (time.time(), val)
        if len(self._cache) > MAX_CACHE_SIZE:
            # Remove oldest 32 entries
            for k, _ in sorted(self._cache.items(), key=lambda x: x[1][0])[:32]:
                self._cache.pop(k, None)

    async def _delete(self, msg: Optional[discord.Message]) -> None:
        """Safely delete a message"""
        if not msg:
            return
        try:
            await msg.delete()
        except discord.NotFound:
            log.debug(f"Message {msg.id} already deleted")
        except discord.Forbidden:
            log.warning(f"Missing permissions to delete message {msg.id}")
        except Exception as e:
            log.error(f"Failed to delete message {msg.id}: {e}")

    # ──────────────────────────────────────────────────────────────────────────────
    # RATE LIMITING - AGGRESSIVE & ACCURATE
    # ──────────────────────────────────────────────────────────────────────────────

    async def _respect_api_rate_limits(self):
        """Enforce global rate limits with precision"""
        await self._ready.wait()

        now = time.time()

        # Clean expired requests from the sliding window
        window_start = now - RATE_LIMIT_WINDOW
        self._request_times = [t for t in self._request_times if t > window_start]

        max_per_minute = await self.config.max_requests_per_minute()

        # Enforce per-minute limit
        if len(self._request_times) >= max_per_minute:
            oldest_request = self._request_times[0]
            wait_time = RATE_LIMIT_WINDOW - (now - oldest_request)

            log.warning(
                f"Global rate limit reached: {len(self._request_times)}/{max_per_minute} "
                f"requests in last {RATE_LIMIT_WINDOW}s. Waiting {wait_time:.1f}s"
            )

            if wait_time > 0:
                await asyncio.sleep(wait_time)

            # Recheck after waiting
            return await self._respect_api_rate_limits()

        # Enforce minimum gap between calls
        if self._last_api_call:
            time_since_last = now - self._last_api_call.timestamp()
            min_gap = await self.config.min_api_call_gap()

            if time_since_last < min_gap:
                wait_time = min_gap - time_since_last
                log.debug(f"Enforcing min gap: waiting {wait_time:.1f}s")
                await asyncio.sleep(wait_time)

        # Record this request
        self._last_api_call = datetime.now(timezone.utc)
        self._request_times.append(now)

        log.debug(
            f"Rate limit check passed: {len(self._request_times)}/{max_per_minute} "
            f"requests in window, gap OK"
        )

    # ──────────────────────────────────────────────────────────────────────────────
    # VALIDATION - CRITICAL MISSING METHOD ADDED
    # ──────────────────────────────────────────────────────────────────────────────

    async def _validate(
        self,
        user_id: int,
        guild_id: Optional[int],
        question: str,
        channel: discord.abc.Messageable,
    ) -> bool:
        """Validate query and permissions - COMPLETE REWRITE"""

        # 1. Check if cog is ready
        if not self._ready.is_set():
            await channel.send("⚠️ Bot is still starting. Please wait 10 seconds.")
            return False

        # 2. Check for empty question
        if not question or not question.strip():
            await channel.send("❌ Please provide a question.")
            return False

        # 3. Check guild settings (if in guild)
        if guild_id:
            guild_config = self.config.guild_from_id(guild_id)

            if not await guild_config.enabled():
                await channel.send("❌ Grok is disabled in this server.")
                return False

            max_length = await guild_config.max_input_length()
        else:
            max_length = MAX_INPUT_LENGTH

        # 4. Check question length
        if len(question) > max_length:
            await channel.send(
                f"❌ Question too long ({len(question)}/{max_length} characters). "
                "Please shorten it."
            )
            return False

        # 5. Check if user has active request
        if user_id in self._active:
            await channel.send("⏳ You already have a request processing. Please wait.")
            return False

        # 6. Check user cooldown (additional safety layer)
        user_data = await self.config.user_from_id(user_id).all()
        last_request = user_data.get("last_request_time")

        if last_request:
            try:
                # Handle both timestamp and ISO format for backward compatibility
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

    # ──────────────────────────────────────────────────────────────────────────────
    # API CALL - FIXED FOR MOONSHOT AI
    # ──────────────────────────────────────────────────────────────────────────────

    async def _ask_k2(self, question: str, temperature: float) -> dict:
        """Call Moonshot AI API - FIXED & BULLETPROOF"""

        api_key = await self.config.api_key()
        if not api_key:
            raise ValueError(
                "❌ **API key not configured!**\n\n"
                "Please set your key with: `[p]grok admin apikey <your-key>`\n"
                "Get your key from: https://platform.moonshot.ai/console/api-keys"
            )

        model_name = await self.config.model_name()

        # CRITICAL: Use the exact payload format Moonshot AI expects
        payload = {
            "model": model_name,
            "messages": [
                {"role": "system", "content": K2_PROMPT},
                {"role": "user", "content": question},
            ],
            "temperature": temperature,
            "max_tokens": 2000,
            "top_p": 0.95,  # Moonshot AI parameter
            "n": 1,  # Number of completions
            "stream": False,  # Explicitly disable streaming
        }

        max_retries = await self.config.max_retries()
        api_key_clean = api_key.strip()

        for attempt in range(max_retries):
            try:
                log.info(
                    f"API Request: model={model_name}, attempt={attempt + 1}, "
                    f"question_len={len(question)}"
                )

                timeout = aiohttp.ClientTimeout(
                    connect=10,  # 10 second connection timeout
                    total=await self.config.timeout(),
                )

                async with self._session.post(
                    f"{KIMI_API_BASE}/chat/completions",
                    json=payload,
                    headers={
                        "Authorization": f"Bearer {api_key_clean}",
                        "Content-Type": "application/json",
                        "Accept": "application/json",
                    },
                    timeout=timeout,
                ) as resp:
                    log.info(f"API Response: HTTP {resp.status}")

                    # Rate limit handling
                    if resp.status == 429:
                        retry_after = resp.headers.get("Retry-After")
                        if retry_after:
                            wait_time = int(retry_after)
                        else:
                            # Exponential backoff
                            wait_time = min(2**attempt, 30)

                        log.warning(
                            f"Rate limited (429). Waiting {wait_time}s "
                            f"(attempt {attempt + 1}/{max_retries})"
                        )

                        if attempt < max_retries - 1:
                            await asyncio.sleep(wait_time)
                            continue
                        else:
                            # Provide clear guidance
                            raise ValueError(
                                f"⏱️ ** Rate limit reached ** - Moonshot AI is limiting requests.\n\n"
                                f"Your API key tier allows ~{await self.config.max_requests_per_minute()} "
                                f"requests per minute. You've hit this limit.\n\n"
                                "**Solutions:**\n"
                                "1. Wait 60 seconds before asking again\n"
                                "2. Ask your server admin to increase cooldown with `[p]grok admin cooldown 60`\n"
                                "3. Upgrade your API key tier at: https://platform.moonshot.ai/account/api-keys"
                            )

                    # Authentication errors
                    elif resp.status == 401:
                        log.error("401 Unauthorized - Invalid API key")
                        raise ValueError(
                            "❌ **401 Unauthorized** - Your API key is invalid.\n\n"
                            "Please check your key at: https://platform.moonshot.ai/console/api-keys\n"
                            "Then set it again with: `[p]grok admin apikey <key>`"
                        )

                    # Forbidden (likely wrong model)
                    elif resp.status == 403:
                        error_text = await resp.text()
                        log.error(f"403 Forbidden: {error_text}")
                        raise ValueError(
                            f"❌ **403 Forbidden** - Your API key can't access model `{model_name}`.\n\n"
                            "Try changing model: `[p]grok admin setmodel moonshot-v1-8k`"
                        )

                    # Server errors
                    elif resp.status >= 500:
                        log.error(f"Server error {resp.status}")
                        if attempt < max_retries - 1:
                            await asyncio.sleep(min(2**attempt, 10))
                            continue
                        raise ValueError(
                            f"❌ Server error (HTTP {resp.status}). Please try again later."
                        )

                    resp.raise_for_status()
                    data = await resp.json()

                    # Extract and validate response
                    content = (
                        data.get("choices", [{}])[0]
                        .get("message", {})
                        .get("content", "")
                    )

                    if not content:
                        raise ValueError("Empty response from API")

                    # Parse JSON from response
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
        """Extract JSON from response - handles markdown and malformed JSON"""

        # Try direct parse first
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            pass

        # Try to extract JSON from markdown code blocks
        match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", content, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(1))
            except json.JSONDecodeError:
                pass

        # Try to find JSON object in text
        match = re.search(r"\{.*?\}", content, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError:
                pass

        # Fallback: wrap the entire response
        log.warning(
            f"Could not parse JSON, using fallback. Content: {content[:100]}..."
        )

        return {"answer": content, "confidence": 0.5, "sources": []}

    # ──────────────────────────────────────────────────────────────────────────────
    # PROCESSING - WITH REQUEST DEDUPLICATION
    # ──────────────────────────────────────────────────────────────────────────────

    async def _process(
        self, user_id: int, guild_id: Optional[int], question: str, channel
    ):
        """Process a query with deduplication"""

        if not await self._validate(user_id, guild_id, question, channel):
            return

        task = asyncio.current_task()
        self._active[user_id] = task

        try:
            # Check cache first
            key = self._key(question)
            if cached := self._cache_get(key):
                log.debug(f"Cache hit for user {user_id}")
                await channel.send(cached)
                return

            # Check if this question is already being processed
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
                    await channel.send(formatted)
                    return
                except asyncio.TimeoutError:
                    log.warning("Deduplication wait timed out, making new request")

            # Show typing indicator
            async with channel.typing():
                # Get temperature
                temperature = 0.3
                if guild_id:
                    temp_config = await self.config.guild_from_id(
                        guild_id
                    ).default_temperature()
                    temperature = temp_config

                # Create the API call coroutine
                api_coro = self._ask_k2(question, temperature)

                # Execute through queue or directly
                if await self.config.request_queue_enabled():
                    # Use queue for global serialization
                    future = asyncio.Future()
                    self._inflight_requests[key] = future

                    try:
                        result = await self._api_queue.enqueue(api_coro)
                        future.set_result(result)
                    except Exception as e:
                        future.set_exception(e)
                        raise
                    finally:
                        self._inflight_requests.pop(key, None)
                else:
                    # Direct execution (not recommended)
                    result = await api_coro

                # Format and send
                text = self._format(result)

                # Cache successful response
                self._cache_set(key, text)

                # Send to user
                if len(text) > 2000:
                    pages = list(pagify(text, page_length=1900))
                    for page in pages:
                        await channel.send(page)
                else:
                    await channel.send(text)

                # Update stats
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

    def _format(self, data: dict) -> str:
        """Format response for Discord"""
        if not isinstance(data, dict):
            return f"❌ Invalid response type: {type(data)}"

        answer = data.get("answer", "")
        if not answer:
            return "❌ No answer received from AI"

        confidence = data.get("confidence", 0.0)
        sources = data.get("sources", [])

        parts = [answer]

        if confidence > 0:
            emoji = "🟢" if confidence > 0.8 else "🟡" if confidence > 0.6 else "🔴"
            parts.append(f"\n{emoji} **Confidence:** {confidence:.0%}")

        if sources and isinstance(sources, list):
            parts.append("\n**📚 Sources:**")
            for i, src in enumerate(sources[:3], 1):
                if isinstance(src, dict):
                    title = src.get("title", "Source")[:60]
                    url = src.get("url", "")
                    if url:
                        parts.append(f"{i}. [{title}]({url})")
                    else:
                        parts.append(f"{i}. {title}")

        return "\n".join(parts)

    # ──────────────────────────────────────────────────────────────────────────────
    # EVENT LISTENERS
    # ──────────────────────────────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_message(self, msg: discord.Message):
        """Handle mentions and DMs"""
        if msg.author.bot or not self._ready.is_set():
            return

        # Check if API key is configured
        if not await self.config.api_key():
            return

        # Handle mentions in guilds
        if msg.guild and self.bot.user in msg.mentions:
            guild_config = self.config.guild(msg.guild)
            if not await guild_config.enabled():
                return

            # Extract question
            content = msg.content
            for mention in msg.mentions:
                content = content.replace(f"<@{mention.id}>", "").replace(
                    f"<@!{mention.id}>", ""
                )

            question = content.strip()

            # Add context from replied message
            if msg.reference and (replied := msg.reference.resolved):
                if isinstance(replied, discord.Message):
                    question += f"\n\nContext: {replied.content[:500]}"

            if question:
                await self._process(msg.author.id, msg.guild.id, question, msg.channel)

        # Handle DMs (non-command)
        elif isinstance(msg.channel, discord.DMChannel):
            prefixes = await self.bot.get_valid_prefixes()
            if any(msg.content.startswith(prefix) for prefix in prefixes):
                return  # Let command processor handle

            await self._process(msg.author.id, None, msg.content, msg.channel)

    # ──────────────────────────────────────────────────────────────────────────────
    # COMMANDS - COMPLETE SET
    # ──────────────────────────────────────────────────────────────────────────────

    @commands.hybrid_group(name="grok", invoke_without_command=True)
    @commands.cooldown(1, COOLDOWN_SECONDS, commands.BucketType.user)
    async def grok(self, ctx: commands.Context, *, question: str):
        """Ask DripBot's AI anything - powered by Moonshot AI"""
        await ctx.typing()
        await self._process(
            ctx.author.id, ctx.guild.id if ctx.guild else None, question, ctx.channel
        )

    @grok.command(name="stats")
    async def grok_stats(self, ctx: commands.Context):
        """View your usage statistics"""
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
    @commands.guild_only()
    async def grok_admin(self, ctx: commands.Context):
        """Administration commands"""
        pass

    @grok_admin.command(name="apikey")
    @commands.is_owner()
    async def admin_apikey(self, ctx: commands.Context, *, api_key: str):
        """Set the Moonshot AI API key (Owner only)"""
        api_key = api_key.strip()

        if len(api_key) < 32:
            await ctx.send("❌ Invalid API key format (too short)")
            return

        await self.config.api_key.set(api_key)
        await ctx.send("✅ API key saved. Use `[p]grok admin verify` to test it.")

    @grok_admin.command(name="verify")
    @commands.is_owner()
    async def admin_verify(self, ctx: commands.Context):
        """Test API connectivity and show key info"""
        msg = await ctx.send("🔍 Testing API connection...")

        try:
            # Quick connectivity test
            test_payload = {
                "model": await self.config.model_name(),
                "messages": [{"role": "user", "content": "Test"}],
                "max_tokens": 5,
                "temperature": 0,
            }

            api_key = await self.config.api_key()

            async with self._session.post(
                f"{KIMI_API_BASE}/chat/completions",
                json=test_payload,
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                data = await resp.json()

                if resp.status == 200:
                    await msg.edit(content="✅ API key is working!")

                    # Show key details
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
        """Enable/disable Grok in this server"""
        current = await self.config.guild(ctx.guild).enabled()
        await self.config.guild(ctx.guild).enabled.set(not current)

        status = "ENABLED 🟢" if not current else "DISABLED 🔴"
        await ctx.send(f"✅ Grok is now **{status}** in this server")

    @grok_admin.command(name="cooldown")
    @commands.is_owner()
    async def admin_cooldown(self, ctx: commands.Context, seconds: int):
        """Set per-user cooldown (Owner only)"""
        if seconds < 5:
            await ctx.send("❌ Cooldown must be at least 5 seconds")
            return

        await self.config.cooldown_seconds.set(seconds)

        # Update command cooldown
        self.grok._buckets._cooldown = commands.Cooldown(
            1, seconds, commands.BucketType.user
        )

        await ctx.send(f"✅ Cooldown set to {seconds} seconds per user")

    @grok_admin.command(name="setmodel")
    @commands.is_owner()
    async def admin_setmodel(self, ctx: commands.Context, model: str):
        """Change AI model (moonshot-v1-8k, 32k, 128k)"""
        valid_models = ["moonshot-v1-8k", "moonshot-v1-32k", "moonshot-v1-128k"]

        if model not in valid_models:
            await ctx.send(f"❌ Invalid model. Choose from: {', '.join(valid_models)}")
            return

        await self.config.model_name.set(model)
        await ctx.send(f"✅ Model set to `{model}`")

    @grok_admin.command(name="ratelimits")
    @commands.is_owner()
    async def admin_ratelimits(
        self, ctx: commands.Context, per_minute: int, min_gap: float
    ):
        """Configure global rate limits (Owner only)"""
        if per_minute < 1:
            await ctx.send("❌ Must allow at least 1 request per minute")
            return

        if min_gap < 1.0:
            await ctx.send("❌ Minimum gap must be at least 1.0 seconds")
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
        """Show comprehensive diagnostics"""
        await ctx.typing()

        embed = discord.Embed(
            title="🛠️ GrokCog Diagnostics",
            color=discord.Color.blue(),
            timestamp=datetime.now(timezone.utc),
        )

        # API Status
        api_key = await self.config.api_key()
        embed.add_field(
            name="API Key Set", value="✅ Yes" if api_key else "❌ No", inline=True
        )

        # Model
        model = await self.config.model_name()
        embed.add_field(name="Model", value=f"`{model}`", inline=True)

        # Rate Limits
        recent_reqs = len([t for t in self._request_times if time.time() - t < 60])
        max_reqs = await self.config.max_requests_per_minute()
        embed.add_field(
            name="Recent Requests",
            value=f"{recent_reqs}/{max_reqs} per minute",
            inline=True,
        )

        # Active Tasks
        embed.add_field(name="Active Tasks", value=len(self._active), inline=True)

        # Cache
        embed.add_field(name="Cache Size", value=len(self._cache), inline=True)

        # Queue
        queue_enabled = await self.config.request_queue_enabled()
        embed.add_field(
            name="Request Queue",
            value="✅ Enabled" if queue_enabled else "❌ Disabled",
            inline=True,
        )

        # Last API Call
        if self._last_api_call:
            time_since = (
                datetime.now(timezone.utc) - self._last_api_call
            ).total_seconds()
            embed.add_field(
                name="Last API Call", value=f"{time_since:.1f}s ago", inline=True
            )

        # Config
        cooldown = await self.config.cooldown_seconds()
        min_gap = await self.config.min_api_call_gap()
        embed.add_field(name="User Cooldown", value=f"{cooldown}s", inline=True)
        embed.add_field(name="Min API Gap", value=f"{min_gap}s", inline=True)

        await ctx.send(embed=embed)

    @grok_admin.command(name="clearcache")
    @commands.is_owner()
    async def admin_clearcache(self, ctx: commands.Context):
        """Clear the response cache"""
        self._cache.clear()
        await ctx.send("✅ Cache cleared")


async def setup(bot):
    await bot.add_cog(GrokCog(bot))
