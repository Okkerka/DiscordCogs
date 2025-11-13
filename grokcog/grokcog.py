# ──────────────────────────────────────────────────────────────────────────────
# IMPORTS & CONSTANTS
# ──────────────────────────────────────────────────────────────────────────────

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional, Tuple, List
import hashlib
import aiohttp

import discord
from redbot.core import commands, Config, checks, app_commands
from redbot.core.utils.mod import is_admin_or_superior
from redbot.core.utils.chat_formatting import pagify

log = logging.getLogger("red.grokcog")

# FIXED: Removed trailing space
KIMI_API_BASE = "https://api.moonshot.ai/v1"

# Enhanced configuration
COOLDOWN_SECONDS = 15
K2_MODEL = "kimi-k2-thinking"
K2_PROMPT = """You are DripBot's AI brain, powered by Kimi K2 with native search and reasoning.

RESPOND WITH VALID JSON:
{
  "answer": "Your answer with [1], [2] citations",
  "confidence": 0.95,
  "sources": [{"title": "Page Title", "url": "https://example.com"}]
}"""

CACHE_TTL = 3600
MAX_CACHE_SIZE = 256
MAX_INPUT_LENGTH = 4000

# NEW: Rate limit configuration
MIN_API_CALL_GAP = 2.0  # Increased gap between calls
RATE_LIMIT_WINDOW = 60  # 60 second window
MAX_REQUESTS_PER_MINUTE = 20  # Adjust based on your API tier
MAX_RETRIES = 5  # Increased retries

# ──────────────────────────────────────────────────────────────────────────────
# COG CLASS
# ──────────────────────────────────────────────────────────────────────────────

class GrokCog(commands.Cog):
    """🧠 DripBot's AI brain - Powered by Kimi K2 Thinking"""

    def __init__(self, bot):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=0x4B324B32, force_registration=True)

        # FIXED: Proper config registration with better defaults
        self.config.register_global(
            api_key=None,
            timeout=60,
            max_retries=MAX_RETRIES,
            cooldown_seconds=COOLDOWN_SECONDS,
            min_api_call_gap=MIN_API_CALL_GAP,
            max_requests_per_minute=MAX_REQUESTS_PER_MINUTE,
        )

        self.config.register_guild(
            enabled=True,
            max_input_length=MAX_INPUT_LENGTH,
            default_temperature=0.3
        )

        self.config.register_user(
            request_count=0,
            last_request_time=None,
            rate_limit_hits=0
        )

        self._active: Dict[int, asyncio.Task] = {}
        self._cache: Dict[str, Tuple[float, str]] = {}
        self._session: Optional[aiohttp.ClientSession] = None
        self._last_api_call: Optional[datetime] = None

        # NEW: Global rate limit tracking
        self._request_times: List[float] = []

        # NEW: Bot ready state tracking
        self._ready = asyncio.Event()

    async def cog_load(self):
        """Initialize aiohttp session and cleanup old data"""
        self._session = aiohttp.ClientSession()

        # Cleanup old user data (older than 30 days)
        await self._cleanup_old_user_data()

        self._ready.set()
        log.info("GrokCog v3.1.0 loaded with enhanced rate limiting and error handling")

    async def cog_unload(self):
        """Cleanup on unload"""
        self._ready.clear()

        if self._session:
            await self._session.close()

        # Cancel all active tasks
        for task in list(self._active.values()):
            task.cancel()

        # Wait for tasks to complete
        if self._active:
            await asyncio.wait(self._active.values(), timeout=5)

        self._active.clear()
        log.info("GrokCog unloaded successfully")

    # ──────────────────────────────────────────────────────────────────────────────
    # UTILITY METHODS - FIXED
    # ──────────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _key(text: str) -> str:
        """Generate cache key - FIXED: Use full hash to avoid collisions"""
        return hashlib.sha256(text.strip().lower().encode()).hexdigest()

    def _cache_get(self, key: str) -> Optional[str]:
        """Get cached response with TTL - FIXED: Not async"""
        if item := self._cache.get(key):
            ts, val = item
            if datetime.now(timezone.utc).timestamp() - ts < CACHE_TTL:
                return val
            self._cache.pop(key, None)
        return None

    def _cache_set(self, key: str, val: str) -> None:
        """Set cache with LRU pruning - FIXED: Not async"""
        self._cache[key] = (datetime.now(timezone.utc).timestamp(), val)
        if len(self._cache) > MAX_CACHE_SIZE:
            # Remove oldest 32 entries
            for k, _ in sorted(self._cache.items(), key=lambda x: x[1][0])[:32]:
                self._cache.pop(k, None)

    async def _delete(self, msg: Optional[discord.Message]) -> None:
        """Safely delete a message - FIXED: Better error handling"""
        if not msg:
            return

        try:
            await msg.delete()
        except discord.NotFound:
            log.debug(f"Message {msg.id} already deleted")
        except discord.Forbidden:
            log.warning(f"Missing permissions to delete message {msg.id}")
        except discord.HTTPException as e:
            log.error(f"Failed to delete message {msg.id}: {e}")

    async def _cleanup_old_user_data(self):
        """Remove user data older than 30 days - NEW"""
        cutoff = datetime.now(timezone.utc) - timedelta(days=30)
        all_users = await self.config.all_users()

        cleaned = 0
        for user_id, data in all_users.items():
            if data["last_request_time"]:
                last_time = datetime.fromisoformat(data["last_request_time"])
                if last_time < cutoff:
                    await self.config.user_from_id(user_id).clear()
                    cleaned += 1

        if cleaned:
            log.info(f"Cleaned up data for {cleaned} inactive users")

    # ──────────────────────────────────────────────────────────────────────────────
    # RATE LIMITING - ENHANCED
    # ──────────────────────────────────────────────────────────────────────────────

    async def _respect_api_rate_limits(self):
        """Enhanced global rate limiting with per-minute tracking"""
        await self._ready.wait()

        now = datetime.now(timezone.utc).timestamp()

        # Clean old requests outside the window
        self._request_times = [
            t for t in self._request_times
            if now - t < RATE_LIMIT_WINDOW
        ]

        # Enforce per-minute limit
        max_per_minute = await self.config.max_requests_per_minute()
        if len(self._request_times) >= max_per_minute:
            wait_time = RATE_LIMIT_WINDOW - (now - self._request_times[0])
            log.warning(f"Global rate limit reached. Waiting {wait_time:.1f}s")
            await asyncio.sleep(wait_time)
            return await self._respect_api_rate_limits()  # Recheck after waiting

        # Enforce minimum gap between calls
        if self._last_api_call:
            time_since_last = now - self._last_api_call.timestamp()
            min_gap = await self.config.min_api_call_gap()
            if time_since_last < min_gap:
                await asyncio.sleep(min_gap - time_since_last)

        self._last_api_call = datetime.now(timezone.utc)
        self._request_times.append(now)

    # ──────────────────────────────────────────────────────────────────────────────
    # K2 API CALL - COMPLETELY REWRITTEN
    # ──────────────────────────────────────────────────────────────────────────────

    async def _ask_k2(self, question: str, temperature: float) -> dict:
        """Call Kimi K2 API with comprehensive error handling"""
        api_key = await self.config.api_key()
        if not api_key:
            raise ValueError(
                "❌ **API key not configured!**\n\n"
                "Please set your Kimi API key using:\n"
                "`[p]grok admin apikey <your-key-here>`\n\n"
                "Get your API key from: https://platform.moonshot.ai/console/projects/api-keys"
            )

        payload = {
            "model": K2_MODEL,
            "messages": [
                {"role": "system", "content": K2_PROMPT},
                {"role": "user", "content": question}
            ],
            "temperature": temperature,
            "max_tokens": 2000,
            "tools": [{"type": "builtin", "name": "search"}],
            "response_format": {"type": "json_object"}
        }

        max_retries = await self.config.max_retries()
        api_key_str = api_key.strip()

        for attempt in range(max_retries):
            try:
                # Respect global rate limits
                await self._respect_api_rate_limits()

                # FIXED: Separate connect and read timeouts
                timeout = aiohttp.ClientTimeout(
                    connect=10,
                    total=await self.config.timeout()
                )

                async with self._session.post(
                    f"{KIMI_API_BASE}/chat/completions",
                    json=payload,
                    headers={"Authorization": f"Bearer {api_key_str}"},
                    timeout=timeout
                ) as resp:

                    # Handle specific status codes
                    if resp.status == 429:
                        retry_after = resp.headers.get("Retry-After")
                        if retry_after:
                            wait_time = int(retry_after)
                        else:
                            # Exponential backoff with jitter
                            wait_time = min(2  ** attempt + (hash(question) % 3), 60)

                        log.warning(
                            f"Rate limited (429). Attempt {attempt + 1}/{max_retries}. "
                            f"Waiting {wait_time}s"
                        )

                        if attempt < max_retries - 1:
                            await asyncio.sleep(wait_time)
                            continue
                        else:
                            raise ValueError(
                                f"⏱️  ** Service is busy **  - Moonshot AI is experiencing high demand.\n\n"
                                f"Please wait {wait_time} seconds and try again.\n\n"
                                "💡 **Tip**: The API limits requests per minute. Avoid rapid-fire questions."
                            )

                    elif resp.status == 401:
                        log.error("401 Unauthorized - Invalid API key")
                        raise ValueError(
                            "❌ **401 Unauthorized** - Invalid API key!\n\n"
                            "Please check your key and reset it with `[p]grok admin apikey <key>`"
                        )

                    elif resp.status == 503:
                        log.warning(f"Service unavailable (503). Attempt {attempt + 1}/{max_retries}")
                        if attempt < max_retries - 1:
                            await asyncio.sleep(10)
                            continue
                        else:
                            raise ValueError(
                                "⚠️ **Service temporarily unavailable** - Please try again later."
                            )

                    elif resp.status >= 500:
                        log.error(f"Server error {resp.status}")
                        if attempt < max_retries - 1:
                            await asyncio.sleep(min(2 ** attempt, 30))
                            continue

                    resp.raise_for_status()
                    data = await resp.json()

                    # FIXED: Validate response format
                    if not data.get("choices"):
                        raise ValueError("Invalid response format from API")

                    content = data["choices"][0]["message"]["content"]
                    if not content:
                        raise ValueError("Empty response from AI service")

                    # FIXED: Better JSON parsing error handling
                    try:
                        return json.loads(content)
                    except json.JSONDecodeError as e:
                        log.error(f"Invalid JSON in response: {content[:200]}")
                        raise ValueError(
                            "❌ Invalid response from AI service - Malformed data received"
                        )

            except aiohttp.ClientResponseError as e:
                if e.status == 429:
                    raise ValueError(
                        "⏱️ **Rate Limit Reached** - Please slow down your requests.\n\n"
                        "The service is experiencing high demand. Wait a minute before trying again."
                    )
                elif e.status == 401:
                    raise ValueError("❌ **401 Unauthorized** - Invalid API key!")
                else:
                    log.error(f"API error {e.status}: {e.message}")
                    if attempt == max_retries - 1:
                        raise ValueError(f"❌ API Error: {e.message}")

            except asyncio.TimeoutError:
                log.warning(f"Request timeout. Attempt {attempt + 1}/{max_retries}")
                if attempt == max_retries - 1:
                    raise ValueError("⏱️ **Request timed out** - The service is taking too long to respond.")
                await asyncio.sleep(min(2 ** attempt, 15))

            except Exception as e:
                log.exception(f"Unexpected error on attempt {attempt + 1}")
                if attempt == max_retries - 1:
                    raise ValueError(f"❌ Unexpected error: {str(e)}")

                wait_time = min(2  ** attempt, 30)
                log.warning(f"Retrying in {wait_time}s")
                await asyncio.sleep(wait_time)

        # Should never reach here, but just in case
        raise ValueError("❌ Failed to get response after all retries")

    # ──────────────────────────────────────────────────────────────────────────────
    # VALIDATION & PROCESSING - ENHANCED
    # ──────────────────────────────────────────────────────────────────────────────

    async def _validate(self, user_id: int, guild_id: Optional[int], question: str, channel) -> bool:
        """Validate query and permissions - FIXED: Better checks"""
        if not question or not question.strip():
            await channel.send("❌ Please provide a question.")
            return False

        # Check if cog is ready
        if not self._ready.is_set():
            await channel.send("⚠️ Bot is still starting up. Please wait a moment.")
            return False

        if guild_id:
            guild_config = self.config.guild_from_id(guild_id)
            if not await guild_config.enabled():
                await channel.send("❌ Grok is disabled in this server.")
                return False

            max_len = await guild_config.max_input_length()
        else:
            max_len = MAX_INPUT_LENGTH

        if len(question) > max_len:
            await channel.send(f"❌ Input too long ({len(question)}/{max_len} characters)")
            return False

        # Check if user already has an active request
        if user_id in self._active:
            await channel.send("⏳ You already have a request being processed. Please wait.")
            return False

        return True

    async def _process(self, user_id: int, guild_id: Optional[int], question: str, channel):
        """Process a query from start to finish - FIXED: Better error handling"""
        if not await self._validate(user_id, guild_id, question, channel):
            return

        task = asyncio.current_task()
        self._active[user_id] = task

        status_msg = None

        try:
            # Check cache first
            key = self._key(question)
            if cached := self._cache_get(key):
                await channel.send(cached)
                return

            # Show typing indicator - NEW
            async with channel.typing():
                # Get temperature
                if guild_id:
                    temperature = await self.config.guild_from_id(guild_id).default_temperature()
                else:
                    temperature = 0.3

                # Call K2 API
                result = await self._ask_k2(question, temperature)
                text = self._format(result)

            # Send response
            if len(text) > 2000:
                pages = list(pagify(text, page_length=1900))
                for i, page in enumerate(pages, 1):
                    if i == 1:
                        await channel.send(page)
                    else:
                        await channel.send(f"*(continued)*\n{page}")
            else:
                await channel.send(text)

            # Cache successful response
            self._cache_set(key, text)

            # Update user stats - FIXED: Use timestamp instead of string
            async with self.config.user_from_id(user_id).all() as user_data:
                user_data["request_count"] = user_data.get("request_count", 0) + 1
                user_data["last_request_time"] = datetime.now(timezone.utc).timestamp()

        except ValueError as e:
            # User-friendly errors
            await channel.send(str(e))
        except Exception as e:
            # Unexpected errors
            log.exception(f"Query failed for user {user_id}: {e}")
            await channel.send("❌ An unexpected error occurred while processing your request.")
        finally:
            # Clean up status message
            await self._delete(status_msg)
            self._active.pop(user_id, None)

    def _format(self, data: dict) -> str:
        """Format K2 response - FIXED: Safer formatting"""
        if not isinstance(data, dict):
            log.error(f"Invalid data type for formatting: {type(data)}")
            return "❌ Invalid response format"

        answer = data.get("answer", "")
        confidence = data.get("confidence", 0.0)
        sources = data.get("sources", [])

        if not answer:
            return "❌ No answer received from AI service"

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
    # EVENT LISTENERS - FIXED
    # ──────────────────────────────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_message(self, msg: discord.Message):
        """Handle mentions and DMs - FIXED: Better checks"""
        if msg.author.bot or not self._ready.is_set():
            return

        # Check if cog has API key configured
        if not await self.config.api_key():
            return

        # Guild mentions
        if msg.guild and self.bot.user in msg.mentions:
            guild_config = self.config.guild(msg.guild)
            if not await guild_config.enabled():
                return

            # Extract question from mention
            content = msg.content
            for mention in msg.mentions:
                content = content.replace(f"<@{mention.id}>", "").replace(f"<@!{mention.id}>", "")

            question = content.strip()

            # Add context from replied message
            if msg.reference and (replied := msg.reference.resolved):
                if isinstance(replied, discord.Message):
                    question += f"\n\nContext: {replied.content[:500]}"

            if question:
                await self._process(msg.author.id, msg.guild.id, question, msg.channel)

        # DM handling
        elif isinstance(msg.channel, discord.DMChannel):
            # Check if message starts with a command prefix
            prefixes = await self.bot.get_valid_prefixes()
            if any(msg.content.startswith(prefix) for prefix in prefixes):
                return  # Let command processor handle commands

            # Only process non-command DMs
            await self._process(msg.author.id, None, msg.content, msg.channel)

    # ──────────────────────────────────────────────────────────────────────────────
    # COMMANDS - FIXED WITH MODERN RED CHECKS
    # ──────────────────────────────────────────────────────────────────────────────

    @commands.hybrid_group(name="grok", invoke_without_command=True)
    @commands.cooldown(1, COOLDOWN_SECONDS, commands.BucketType.user)
    async def grok(self, ctx: commands.Context, *, question: str):
        """Ask DripBot's AI anything - it searches and reasons automatically"""
        if not ctx.interaction:
            await ctx.typing()

        await self._process(ctx.author.id, ctx.guild.id if ctx.guild else None, question, ctx.channel)

    @grok.command(name="stats")
    async def grok_stats(self, ctx: commands.Context):
        """View your usage statistics - FIXED: Better formatting"""
        stats = await self.config.user(ctx.author).all()

        embed = discord.Embed(
            title=f"📊 {ctx.author.display_name}'s Grok Stats",
            color=discord.Color.gold(),
            timestamp=datetime.now(timezone.utc)
        )

        embed.add_field(
            name="Total Queries",
            value=stats.get("request_count", 0),
            inline=True
        )

        if stats.get("last_request_time"):
            embed.add_field(
                name="Last Query",
                value=f"<t:{int(stats['last_request_time'])}:R>",
                inline=True
            )

        await ctx.send(embed=embed)

    @grok.group(name="admin")
    @commands.guild_only()
    async def grok_admin(self, ctx: commands.Context):
        """Administration commands for Grok"""
        pass

    @grok_admin.command(name="apikey")
    @commands.is_owner()
    async def admin_apikey(self, ctx: commands.Context, *, api_key: str):
        """Set the Kimi API key (Bot Owner only) - FIXED: Respects rate limits"""
        if len(api_key.strip()) < 32:
            await ctx.send("❌ Invalid API key format (should be at least 32 characters)")
            return

        api_key = api_key.strip()
        await self.config.api_key.set(api_key)

        msg = await ctx.send("🔍 Verifying API key...")

        try:
            # FIXED: Respect rate limits even for verification
            await self._respect_api_rate_limits()

            test_payload = {
                "model": K2_MODEL,
                "messages": [{"role": "user", "content": "Test connection"}],
                "max_tokens": 10
            }

            timeout = aiohttp.ClientTimeout(connect=10, total=15)

            async with self._session.post(
                f"{KIMI_API_BASE}/chat/completions",
                json=test_payload,
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=timeout
            ) as resp:
                if resp.status == 200:
                    await msg.edit(content="✅ API key saved and verified!")
                elif resp.status == 401:
                    await msg.edit(content="❌ **401 Unauthorized** - Invalid API key!")
                    await self.config.api_key.clear()
                elif resp.status == 429:
                    await msg.edit(
                        content="⚠️ **429 Rate Limited** - Key is valid but service is busy. Try again later."
                    )
                else:
                    await msg.edit(content=f"⚠️ Unexpected response: HTTP {resp.status}")

        except Exception as e:
            log.exception("API key verification failed")
            await msg.edit(content=f"❌ Verification failed: {str(e)}")
            await self.config.api_key.clear()

    @grok_admin.command(name="verify")
    @commands.is_owner()
    async def admin_verify(self, ctx: commands.Context):
        """Test if the API key is working - FIXED: Respects rate limits"""
        if not ctx.interaction:
            await ctx.typing()

        msg = await ctx.send("🔍 Testing API connection...")

        try:
            await self._respect_api_rate_limits()

            api_key = await self.config.api_key()
            if not api_key:
                await msg.edit(content="❌ No API key is set. Use `[p]grok admin apikey <key>`")
                return

            test_payload = {
                "model": K2_MODEL,
                "messages": [{"role": "user", "content": "OK"}],
                "max_tokens": 10
            }

            timeout = aiohttp.ClientTimeout(connect=10, total=15)

            async with self._session.post(
                f"{KIMI_API_BASE}/chat/completions",
                json=test_payload,
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=timeout
            ) as resp:
                status_map = {
                    200: "✅ API key is working!",
                    401: "❌ **401 Unauthorized** - Invalid API key!",
                    429: "⚠️ **429 Rate Limited** - Service is busy, but key is valid!",
                }
                await msg.edit(content=status_map.get(resp.status, f"⚠️ HTTP {resp.status}"))

        except Exception as e:
            log.exception("API verification failed")
            await msg.edit(content=f"❌ Error: {str(e)}")

    @grok_admin.command(name="toggle")
    @commands.admin_or_permissions(manage_guild=True)
    async def admin_toggle(self, ctx: commands.Context):
        """Enable or disable Grok in this server - FIXED: Modern check"""
        current = await self.config.guild(ctx.guild).enabled()
        await self.config.guild(ctx.guild).enabled.set(not current)

        status = "ENABLED 🟢" if not current else "DISABLED 🔴"
        await ctx.send(f"✅ Grok is now **{status}** in this server")

    @grok_admin.command(name="cooldown")
    @commands.is_owner()
    async def admin_cooldown(self, ctx: commands.Context, seconds: int):
        """Set command cooldown in seconds (Owner only)"""
        if seconds < 1:
            await ctx.send("❌ Cooldown must be at least 1 second")
            return

        await self.config.cooldown_seconds.set(seconds)

        # Update the cooldown on the command
        self.grok._buckets._cooldown = commands.Cooldown(1, seconds, commands.BucketType.user)

        await ctx.send(f"✅ Cooldown set to {seconds} seconds per user")

    @grok_admin.command(name="ratelimitdebug")
    @commands.is_owner()
    async def admin_ratelimitdebug(self, ctx: commands.Context):
        """Debug current rate limit status (Owner only) - NEW"""
        now = datetime.now(timezone.utc).timestamp()
        recent_reqs = len([
            t for t in self._request_times
            if now - t < RATE_LIMIT_WINDOW
        ])

        max_per_minute = await self.config.max_requests_per_minute()

        embed = discord.Embed(
            title="Rate Limit Debug",
            color=discord.Color.blue(),
            timestamp=datetime.now(timezone.utc)
        )

        embed.add_field(
            name="Requests (last 60s)",
            value=f"{recent_reqs}/{max_per_minute}",
            inline=True
        )
        embed.add_field(name="Active tasks", value=len(self._active), inline=True)
        embed.add_field(name="Cache size", value=len(self._cache), inline=True)

        if self._last_api_call:
            time_since = now - self._last_api_call.timestamp()
            embed.add_field(
                name="Time since last API call",
                value=f"{time_since:.2f}s",
                inline=True
            )

        embed.add_field(
            name="Min API gap",
            value=f"{await self.config.min_api_call_gap()}s",
            inline=True
        )

        await ctx.send(embed=embed)

    @grok_admin.command(name="config")
    @commands.is_owner()
    async def admin_config(self, ctx: commands.Context):
        """Show current global configuration - NEW"""
        config = await self.config.all()

        embed = discord.Embed(
            title="GrokCog Global Configuration",
            color=discord.Color.green(),
            timestamp=datetime.now(timezone.utc)
        )

        embed.add_field(name="API Key Set", value="Yes" if config["api_key"] else "No", inline=True)
        embed.add_field(name="Timeout", value=f"{config['timeout']}s", inline=True)
        embed.add_field(name="Max Retries", value=config["max_retries"], inline=True)
        embed.add_field(name="Cooldown", value=f"{config['cooldown_seconds']}s", inline=True)
        embed.add_field(name="Min API Gap", value=f"{config['min_api_call_gap']}s", inline=True)
        embed.add_field(name="Max Req/Min", value=config["max_requests_per_minute"], inline=True)

        await ctx.send(embed=embed)

    @grok_admin.command(name="clearcache")
    @commands.is_owner()
    async def admin_clearcache(self, ctx: commands.Context):
        """Clear the response cache - NEW"""
        self._cache.clear()
        await ctx.send("✅ Cache cleared")

    # ──────────────────────────────────────────────────────────────────────────────
    # ERROR HANDLERS - NEW
    # ──────────────────────────────────────────────────────────────────────────────

    @grok.error
    async def grok_error(self, ctx: commands.Context, error):
        """Handle command errors"""
        if isinstance(error, commands.CommandOnCooldown):
            await ctx.send(
                f"⏱️ Please wait {error.retry_after:.1f} seconds before asking again.",
                ephemeral=True
            )
        elif isinstance(error, commands.NoPrivateMessage):
            await ctx.send("❌ This command cannot be used in DMs.", ephemeral=True)
        else:
            log.exception(f"Error in grok command: {error}")
            await ctx.send("❌ An error occurred while processing your request.", ephemeral=True)

    @grok_admin.error
    async def admin_error(self, ctx: commands.Context, error):
        """Handle admin command permission errors"""
        if isinstance(error, commands.NotOwner):
            await ctx.send("❌ This command is only available to the bot owner.", ephemeral=True)
        elif isinstance(error, commands.MissingPermissions):
            await ctx.send("❌ You need Manage Server permission to use this command.", ephemeral=True)

# ──────────────────────────────────────────────────────────────────────────────
# SETUP
# ──────────────────────────────────────────────────────────────────────────────

async def setup(bot):
    cog = GrokCog(bot)
    await bot.add_cog(cog)
