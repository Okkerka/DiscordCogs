# ──────────────────────────────────────────────────────────────────────────────
# IMPORTS & CONSTANTS
# ──────────────────────────────────────────────────────────────────────────────

import asyncio
import hashlib
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

import aiohttp
import discord
from redbot.core import Config, app_commands, commands
from redbot.core.utils.chat_formatting import pagify

log = logging.getLogger("red.grokcog")

# FIXED: Use correct Moonshot AI model names
# K2_MODEL = "moonshot-v1-8k"  # Most common tier
# K2_MODEL = "moonshot-v1-32k"  # For longer context
K2_MODEL = "moonshot-v1-128k"  # If you have access

# FIXED: Simplified, more robust prompt without OpenAI-specific JSON mode
K2_PROMPT = """You are DripBot's AI assistant. When responding, ALWAYS provide your answer in this exact JSON format:

{
  "answer": "Your answer with citations [1], [2] where appropriate",
  "confidence": 0.95,
  "sources": [{"title": "Source Title", "url": "https://example.com"}]
}

If you cannot find reliable sources, set confidence lower and return an empty sources array. Do not include markdown code blocks around the JSON."""

# API Configuration
KIMI_API_BASE = "https://api.moonshot.ai/v1"
COOLDOWN_SECONDS = 20  # Increased for safety
CACHE_TTL = 3600
MAX_CACHE_SIZE = 256
MAX_INPUT_LENGTH = 4000
MIN_API_CALL_GAP = 3.0  # Increased gap
MAX_RETRIES = 5

# ──────────────────────────────────────────────────────────────────────────────
# COG CLASS
# ──────────────────────────────────────────────────────────────────────────────


class GrokCog(commands.Cog):
    """🧠 DripBot's AI brain - Powered by Kimi K2 Thinking"""

    def __init__(self, bot):
        self.bot = bot
        self.config = Config.get_conf(
            self, identifier=0x4B324B32, force_registration=True
        )

        self.config.register_global(
            api_key=None,
            timeout=60,
            max_retries=MAX_RETRIES,
            cooldown_seconds=COOLDOWN_SECONDS,
            min_api_call_gap=MIN_API_CALL_GAP,
            model_name=K2_MODEL,
            debug_mode=False,  # NEW: Enable verbose logging
        )

        self.config.register_guild(
            enabled=True, max_input_length=MAX_INPUT_LENGTH, default_temperature=0.3
        )

        self.config.register_user(
            request_count=0, last_request_time=None, rate_limit_hits=0
        )

        self._active: Dict[int, asyncio.Task] = {}
        self._cache: Dict[str, Tuple[float, str]] = {}
        self._session: Optional[aiohttp.ClientSession] = None
        self._last_api_call: Optional[datetime] = None
        self._request_times: List[float] = []
        self._ready = asyncio.Event()

    async def cog_load(self):
        """Initialize aiohttp session"""
        self._session = aiohttp.ClientSession()
        self._ready.set()
        log.info("GrokCog v3.2.0 loaded with diagnostic capabilities")

    async def cog_unload(self):
        """Cleanup on unload"""
        self._ready.clear()
        if self._session:
            await self._session.close()
        for task in list(self._active.values()):
            task.cancel()
        if self._active:
            await asyncio.wait(self._active.values(), timeout=5)
        self._active.clear()

    # ──────────────────────────────────────────────────────────────────────────────
    # UTILITY METHODS
    # ──────────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _key(text: str) -> str:
        return hashlib.sha256(text.strip().lower().encode()).hexdigest()

    def _cache_get(self, key: str) -> Optional[str]:
        if item := self._cache.get(key):
            ts, val = item
            if datetime.now(timezone.utc).timestamp() - ts < CACHE_TTL:
                return val
            self._cache.pop(key, None)
        return None

    def _cache_set(self, key: str, val: str) -> None:
        self._cache[key] = (datetime.now(timezone.utc).timestamp(), val)
        if len(self._cache) > MAX_CACHE_SIZE:
            for k, _ in sorted(self._cache.items(), key=lambda x: x[1][0])[:32]:
                self._cache.pop(k, None)

    async def _delete(self, msg: Optional[discord.Message]) -> None:
        if not msg:
            return
        try:
            await msg.delete()
        except:
            pass

    async def _respect_api_rate_limits(self):
        """Global rate limiting"""
        await self._ready.wait()
        now = datetime.now(timezone.utc).timestamp()

        self._request_times = [t for t in self._request_times if now - t < 60]

        if len(self._request_times) >= 20:  # Conservative limit
            wait_time = 60 - (now - self._request_times[0])
            log.warning(f"Global rate limit reached. Waiting {wait_time:.1f}s")
            await asyncio.sleep(wait_time)
            return await self._respect_api_rate_limits()

        if self._last_api_call:
            time_since_last = now - self._last_api_call.timestamp()
            min_gap = await self.config.min_api_call_gap()
            if time_since_last < min_gap:
                await asyncio.sleep(min_gap - time_since_last)

        self._last_api_call = datetime.now(timezone.utc)
        self._request_times.append(now)

    # ──────────────────────────────────────────────────────────────────────────────
    # API CALL - DIAGNOSTIC VERSION
    # ──────────────────────────────────────────────────────────────────────────────

    async def _ask_k2(self, question: str, temperature: float) -> dict:
        """Call Kimi K2 API with extensive logging"""
        api_key = await self.config.api_key()
        debug_mode = await self.config.debug_mode()

        if not api_key:
            raise ValueError(
                "❌ API key not configured! Use `[p]grok admin apikey <key>`"
            )

        model_name = await self.config.model_name()

        # FIXED: Simplified payload without OpenAI-specific parameters
        payload = {
            "model": model_name,
            "messages": [
                {"role": "system", "content": K2_PROMPT},
                {"role": "user", "content": question},
            ],
            "temperature": temperature,
            "max_tokens": 2000,
            # REMOVED: "tools" and "response_format" which may not be supported
        }

        if debug_mode:
            log.info(
                f"API Request: model={model_name}, temp={temperature}, tokens=2000"
            )
            log.info(f"Payload: {json.dumps(payload, ensure_ascii=False)[:500]}...")

        max_retries = await self.config.max_retries()

        for attempt in range(max_retries):
            try:
                await self._respect_api_rate_limits()

                timeout = aiohttp.ClientTimeout(
                    connect=10, total=await self.config.timeout()
                )

                if debug_mode:
                    log.info(f"API Call Attempt {attempt + 1}/{max_retries}")

                async with self._session.post(
                    f"{KIMI_API_BASE}/chat/completions",
                    json=payload,
                    headers={"Authorization": f"Bearer {api_key.strip()}"},
                    timeout=timeout,
                ) as resp:
                    # Log response details
                    if debug_mode:
                        log.info(f"API Response: HTTP {resp.status}")
                        log.info(f"Headers: {dict(resp.headers)}")

                    # Handle specific status codes
                    if resp.status == 429:
                        retry_after = resp.headers.get("Retry-After")
                        wait_time = (
                            int(retry_after) if retry_after else min(2**attempt, 30)
                        )

                        log.warning(f"Rate limited (429). Retry-After: {wait_time}s")

                        if attempt < max_retries - 1:
                            await asyncio.sleep(wait_time)
                            continue
                        else:
                            raise ValueError(
                                f"⏱️ **Rate limit reached** - Please wait {wait_time}s before trying again.\n\n"
                                "**Tip**: If this happens often, your API key tier may be too restrictive. "
                                "Consider upgrading or reducing usage."
                            )

                    elif resp.status == 401:
                        log.error("401 Unauthorized - Invalid API key")
                        raise ValueError("❌ **401 Unauthorized** - Invalid API key!")

                    elif resp.status == 403:
                        log.error(
                            "403 Forbidden - API key may not have access to this model"
                        )
                        raise ValueError(
                            "❌ **403 Forbidden** - Your API key doesn't have access to this model.\n"
                            f"Current model: `{model_name}`\n"
                            "Try: `[p]grok admin setmodel moonshot-v1-8k`"
                        )

                    resp.raise_for_status()
                    data = await resp.json()

                    if debug_mode:
                        log.info(
                            f"Response data: {json.dumps(data, ensure_ascii=False)[:500]}..."
                        )

                    # FIXED: Robust JSON extraction
                    content = (
                        data.get("choices", [{}])[0]
                        .get("message", {})
                        .get("content", "")
                    )

                    if not content:
                        raise ValueError("Empty response from AI service")

                    # Try to parse JSON from the response
                    return self._extract_json(content)

            except aiohttp.ClientError as e:
                log.error(f"Client error (attempt {attempt + 1}): {e}")
                if attempt == max_retries - 1:
                    raise ValueError(f"❌ Connection failed: {str(e)}")
                await asyncio.sleep(min(2**attempt, 15))

            except json.JSONDecodeError as e:
                log.error(f"JSON decode error: {e}")
                raise ValueError("❌ Invalid response format from AI service")

            except Exception as e:
                log.exception(f"Unexpected error on attempt {attempt + 1}")
                if attempt == max_retries - 1:
                    raise
                await asyncio.sleep(min(2**attempt, 15))

        raise ValueError("❌ Failed after all retries")

    def _extract_json(self, content: str) -> dict:
        """FIXED: Robust JSON extraction from various response formats"""
        # Try direct JSON parse first
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            pass

        # Try to extract JSON from markdown code blocks
        json_match = re.search(r"```(?:json)?\n(.*?)\n```", content, re.DOTALL)
        if json_match:
            try:
                return json.loads(json_match.group(1))
            except json.JSONDecodeError:
                pass

        # If all else fails, wrap the content in expected format
        log.warning(
            f"Could not extract JSON, using fallback format. Content: {content[:100]}..."
        )

        return {"answer": content, "confidence": 0.5, "sources": []}

    # ──────────────────────────────────────────────────────────────────────────────
    # PROCESSING & FORMATTING
    # ──────────────────────────────────────────────────────────────────────────────

    async def _process(
        self, user_id: int, guild_id: Optional[int], question: str, channel
    ):
        """Process a query"""
        if not await self._validate(user_id, guild_id, question, channel):
            return

        task = asyncio.current_task()
        self._active[user_id] = task

        try:
            key = self._key(question)
            if cached := self._cache_get(key):
                await channel.send(cached)
                return

            # Get temperature
            temperature = 0.3
            if guild_id:
                temperature = await self.config.guild_from_id(
                    guild_id
                ).default_temperature()

            # Call API
            result = await self._ask_k2(question, temperature)
            text = self._format(result)

            # Send response
            if len(text) > 2000:
                pages = list(pagify(text, page_length=1900))
                for page in pages:
                    await channel.send(page)
            else:
                await channel.send(text)

            # Cache it
            self._cache_set(key, text)

            # Update stats
            async with self.config.user_from_id(user_id).all() as user_data:
                user_data["request_count"] = user_data.get("request_count", 0) + 1
                user_data["last_request_time"] = datetime.now(timezone.utc).timestamp()

        except ValueError as e:
            await channel.send(str(e))
        except Exception as e:
            log.exception(f"Query failed for user {user_id}")
            await channel.send("❌ Unexpected error processing your request")

    def _format(self, data: dict) -> str:
        """Format response - FIXED: Safer formatting"""
        if not isinstance(data, dict):
            return f"❌ Invalid response type: {type(data)}"

        answer = data.get("answer", "")
        if not answer:
            return "❌ No answer received"

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
    # COMMANDS
    # ──────────────────────────────────────────────────────────────────────────────

    @commands.hybrid_group(name="grok", invoke_without_command=True)
    @commands.cooldown(1, COOLDOWN_SECONDS, commands.BucketType.user)
    async def grok(self, ctx: commands.Context, *, question: str):
        """Ask DripBot's AI anything"""
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

        await ctx.send(embed=embed)

    @grok.group(name="admin")
    @commands.guild_only()
    async def grok_admin(self, ctx: commands.Context):
        """Administration commands for Grok"""
        pass

    @grok_admin.command(name="apikey")
    @commands.is_owner()
    async def admin_apikey(self, ctx: commands.Context, *, api_key: str):
        """Set the Kimi API key (Owner only)"""
        api_key = api_key.strip()

        if len(api_key) < 32:
            await ctx.send("❌ Invalid API key format")
            return

        await self.config.api_key.set(api_key)
        await ctx.send("✅ API key saved. Use `[p]grok admin verify` to test it.")

    @grok_admin.command(name="verify")
    @commands.is_owner()
    async def admin_verify(self, ctx: commands.Context):
        """Test if the API key is working"""
        msg = await ctx.send("🔍 Testing API connection...")

        try:
            await self._respect_api_rate_limits()

            # Test with a very simple request
            test_payload = {
                "model": await self.config.model_name(),
                "messages": [{"role": "user", "content": "Hello"}],
                "max_tokens": 10,
                "temperature": 0,
            }

            api_key = await self.config.api_key()

            async with self._session.post(
                f"{KIMI_API_BASE}/chat/completions",
                json=test_payload,
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                status_msg = {
                    200: "✅ API key is working!",
                    401: "❌ **401 Unauthorized** - Invalid API key!",
                    403: "❌ **403 Forbidden** - No access to this model",
                    429: "⚠️ **429 Rate Limited** - Service is busy, but key is valid!",
                }

                message = status_msg.get(resp.status, f"⚠️ HTTP {resp.status}")

                if resp.status == 200:
                    data = await resp.json()
                    model_used = data.get("model", "unknown")
                    message += f"\nModel: `{model_used}`"

                await msg.edit(content=message)

        except Exception as e:
            log.exception("API verification failed")
            await msg.edit(content=f"❌ Error: {str(e)}")

    @grok_admin.command(name="toggle")
    @commands.admin_or_permissions(manage_guild=True)
    async def admin_toggle(self, ctx: commands.Context):
        """Enable or disable Grok in this server"""
        current = await self.config.guild(ctx.guild).enabled()
        await self.config.guild(ctx.guild).enabled.set(not current)

        status = "ENABLED 🟢" if not current else "DISABLED 🔴"
        await ctx.send(f"✅ Grok is now **{status}** in this server")

    @grok_admin.command(name="setmodel")
    @commands.is_owner()
    async def admin_setmodel(self, ctx: commands.Context, model: str):
        """Set the AI model (moonshot-v1-8k, 32k, or 128k)"""
        valid_models = ["moonshot-v1-8k", "moonshot-v1-32k", "moonshot-v1-128k"]

        if model not in valid_models:
            await ctx.send(f"❌ Invalid model. Choose from: {', '.join(valid_models)}")
            return

        await self.config.model_name.set(model)
        await ctx.send(f"✅ Model set to `{model}`")

    @grok_admin.command(name="debug")
    @commands.is_owner()
    async def admin_debug(self, ctx: commands.Context, enabled: bool):
        """Enable/disable debug logging (Owner only)"""
        await self.config.debug_mode.set(enabled)
        await ctx.send(f"✅ Debug mode {'enabled' if enabled else 'disabled'}")

    @grok_admin.command(name="diagnose")
    @commands.is_owner()
    async def admin_diagnose(self, ctx: commands.Context):
        """Run comprehensive diagnostics - NEW"""
        embed = discord.Embed(title="GrokCog Diagnostics", color=discord.Color.blue())

        # Check API key
        api_key = await self.config.api_key()
        embed.add_field(
            name="API Key Set", value="Yes" if api_key else "No", inline=True
        )

        # Check model
        model = await self.config.model_name()
        embed.add_field(name="Model", value=f"`{model}`", inline=True)

        # Check rate limits
        recent_reqs = len(
            [
                t
                for t in self._request_times
                if datetime.now(timezone.utc).timestamp() - t < 60
            ]
        )
        embed.add_field(name="Recent Requests", value=f"{recent_reqs}/60s", inline=True)

        # Check active tasks
        embed.add_field(name="Active Tasks", value=len(self._active), inline=True)

        # Check cache
        embed.add_field(name="Cache Size", value=len(self._cache), inline=True)

        # Check last API call
        if self._last_api_call:
            time_since = (
                datetime.now(timezone.utc) - self._last_api_call
            ).total_seconds()
            embed.add_field(
                name="Last API Call", value=f"{time_since:.1f}s ago", inline=True
            )

        await ctx.send(embed=embed)


async def setup(bot):
    await bot.add_cog(GrokCog(bot))
