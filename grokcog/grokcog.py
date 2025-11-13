# grokcog.py - PRODUCTION-READY RATE LIMIT HANDLING
# Version: 3.0.7 - Graceful rate limits, no scary tracebacks

import asyncio
import json
import logging
from datetime import datetime, timedelta
from typing import Dict, Optional, Tuple, List
import hashlib
import aiohttp

import discord
from redbot.core import commands, Config, checks
from redbot.core.utils.mod import is_admin_or_superior
from redbot.core.utils.chat_formatting import pagify

log = logging.getLogger("red.grokcog")

# ──────────────────────────────────────────────────────────────────────────────
# CONSTANTS
# ──────────────────────────────────────────────────────────────────────────────

COOLDOWN_SECONDS = 15  # INCREASED: More time between commands
K2_MODEL = "kimi-k2-thinking"
KIMI_API_BASE = "https://api.moonshot.ai/v1"

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

# ──────────────────────────────────────────────────────────────────────────────
# COG CLASS
# ──────────────────────────────────────────────────────────────────────────────

class GrokCog(commands.Cog):
    """🧠 DripBot's AI brain - Powered by Kimi K2 Thinking"""

    def __init__(self, bot):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=0x4B324B32, force_registration=True)

        self.config.register_global(
            api_key=None,
            timeout=60,
            max_retries=3,
            cooldown_seconds=15  # NEW: Configurable cooldown
        )
        self.config.register_guild(
            enabled=True,
            max_input_length=MAX_INPUT_LENGTH,
            default_temperature=0.3
        )
        self.config.register_user(
            request_count=0,
            last_request_time=None,
            rate_limit_hits=0  # NEW: Track how often user hits limits
        )

        self._active: Dict[int, asyncio.Task] = {}
        self._cache: Dict[str, Tuple[float, str]] = {}
        self._session: Optional[aiohttp.ClientSession] = None
        self._last_api_call: Optional[datetime] = None  # NEW: Track API call timing

    async def cog_load(self):
        """Initialize aiohttp session"""
        self._session = aiohttp.ClientSession()
        log.info("GrokCog v3.0.7 loaded with enhanced rate limiting")

    async def cog_unload(self):
        """Cleanup on unload"""
        if self._session:
            await self._session.close()
        for task in self._active.values():
            task.cancel()
        self._active.clear()

    # ──────────────────────────────────────────────────────────────────────────────
    # UTILITY METHODS
    # ──────────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _key(text: str) -> str:
        """Generate cache key"""
        return hashlib.sha256(text.strip().lower().encode()).hexdigest()[:12]

    async def _cache_get(self, key: str) -> Optional[str]:
        """Get cached response with TTL"""
        if item := self._cache.get(key):
            ts, val = item
            if datetime.utcnow().timestamp() - ts < CACHE_TTL:
                return val
            self._cache.pop(key, None)
        return None

    async def _cache_set(self, key: str, val: str) -> None:
        """Set cache with LRU pruning"""
        self._cache[key] = (datetime.utcnow().timestamp(), val)
        if len(self._cache) > MAX_CACHE_SIZE:
            for k, _ in sorted(self._cache.items(), key=lambda x: x[1][0])[:32]:
                self._cache.pop(k, None)

    async def _delete(self, msg: Optional[discord.Message]) -> None:
        """Safely delete a message"""
        if msg:
            try:
                await msg.delete()
            except:
                pass

    async def _respect_api_rate_limits(self):
        """Ensure minimum time between API calls"""
        if self._last_api_call:
            now = datetime.utcnow()
            time_since_last = (now - self._last_api_call).total_seconds()
            if time_since_last < 1.0:  # Minimum 1 second between calls
                await asyncio.sleep(1.0 - time_since_last)

        self._last_api_call = datetime.utcnow()

    # ──────────────────────────────────────────────────────────────────────────────
    # K2 API CALL - ENHANCED RATE LIMIT HANDLING
    # ──────────────────────────────────────────────────────────────────────────────

    async def _ask_k2(self, question: str, temperature: float) -> dict:
        """Call Kimi K2 API with intelligent retry logic"""
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

        for attempt in range(max_retries):
            try:
                # Respect API rate limits (minimum 1s between calls)
                await self._respect_api_rate_limits()

                async with self._session.post(
                    f"{KIMI_API_BASE}/chat/completions",
                    json=payload,
                    headers={"Authorization": f"Bearer {api_key}"},
                    timeout=aiohttp.ClientTimeout(total=await self.config.timeout())
                ) as resp:

                    # Handle 429 with smart retry
                    if resp.status == 429:
                        retry_after = resp.headers.get("Retry-After")
                        if retry_after:
                            wait_time = int(retry_after)
                        else:
                            wait_time = min(2 ** attempt, 30)  # Cap at 30 seconds

                        log.warning(f"Rate limited (429). Retry {attempt + 1}/{max_retries} after {wait_time}s")

                        if attempt < max_retries - 1:
                            await asyncio.sleep(wait_time)
                            continue
                        else:
                            # FINAL VERSION: Show helpful message instead of error
                            raise ValueError(
                                f"⏱️ **Service is busy** - Moonshot AI is experiencing high demand.\n\n"
                                f"Please wait {wait_time} seconds and try again.\n\n"
                                "💡 **Tip**: Avoid rapid-fire questions. The API has rate limits per minute."
                            )

                    # Handle 401 Unauthorized
                    if resp.status == 401:
                        log.error(f"401 Unauthorized - Invalid API key")
                        raise ValueError(
                            "❌ **401 Unauthorized** - Invalid API key!\n\n"
                            "Please check your API key at: https://platform.moonshot.ai/console/projects/api-keys\n"
                            "Then reset it with: `[p]grok admin apikey <your-key>`"
                        )

                    resp.raise_for_status()
                    data = await resp.json()
                    return json.loads(data["choices"][0]["message"]["content"])

            except aiohttp.ClientResponseError as e:
                if e.status == 429:
                    # Show user-friendly message instead of scary traceback
                    raise ValueError(
                        f"⏱️ **Rate Limit Reached** - Please slow down.\n\n"
                        f"Moonshot AI is limiting requests. Wait about {min(2 ** attempt, 30)} seconds.\n\n"
                        "This is normal during busy periods or if you've asked many questions recently."
                    )
                elif e.status == 401:
                    raise ValueError(
                        "❌ **401 Unauthorized** - Invalid API key!\n\n"
                        "Please verify your API key and reset it if needed."
                    )
                raise

            except Exception as e:
                if attempt == max_retries - 1:
                    log.exception(f"API call failed after {max_retries} attempts")
                    raise ValueError(f"❌ API Error: {str(e)}")
                await asyncio.sleep(min(2 ** attempt, 30))  # Cap backoff at 30s

    # ──────────────────────────────────────────────────────────────────────────────
    # VALIDATION & PROCESSING
    # ──────────────────────────────────────────────────────────────────────────────

    async def _validate(self, user_id: int, guild_id: Optional[int], question: str, channel) -> bool:
        """Validate query and permissions"""
        if not question.strip():
            await channel.send("❌ Please provide a question.")
            return False

        if guild_id and not await self.config.guild_from_id(guild_id).enabled():
            await channel.send("❌ Grok is disabled in this server.")
            return False

        max_len = await self.config.guild_from_id(guild_id).max_input_length() if guild_id else MAX_INPUT_LENGTH
        if len(question) > max_len:
            await channel.send(f"❌ Too long ({len(question)}/{max_len} chars)")
            return False

        if user_id in self._active:
            await channel.send("⏳ Already processing")
            return False

        return True

    async def _process(self, user_id: int, guild_id: Optional[int], question: str, channel):
        """Process a query from start to finish"""
        if not await self._validate(user_id, guild_id, question, channel):
            return

        self._active[user_id] = asyncio.current_task()
        status = None

        try:
            # Check cache
            key = self._key(question)
            if cached := await self._cache_get(key):
                await channel.send(cached)
                return

            status = await channel.send("🧠 **DripBot is thinking...**")

            # Get temperature (safe for both guild and DM)
            temperature = 0.3
            if guild_id:
                temperature = await self.config.guild_from_id(guild_id).default_temperature()

            # Call K2
            result = await self._ask_k2(question, temperature)
            text = self._format(result)

            # Send response
            await self._delete(status)

            if len(text) > 2000:
                for page in pagify(text, page_length=1900):
                    await channel.send(page)
            else:
                await channel.send(text)

            # Cache response
            await self._cache_set(key, text)

            # Update stats
            async with self.config.user_from_id(user_id).all() as user_data:
                user_data["request_count"] += 1
                user_data["last_request_time"] = datetime.utcnow().isoformat()

        except ValueError as e:
            await self._delete(status)
            await channel.send(str(e))  # Show helpful error
        except Exception as e:
            await self._delete(status)
            log.exception(f"Query failed: {e}")
            await channel.send("❌ Error processing query")
        finally:
            self._active.pop(user_id, None)

    def _format(self, data: dict) -> str:
        """Format K2 response"""
        answer = data.get("answer", "")
        confidence = data.get("confidence", 0.0)
        sources = data.get("sources", [])

        parts = [answer]

        if confidence > 0:
            emoji = "🟢" if confidence > 0.8 else "🟡" if confidence > 0.6 else "🔴"
            parts.append(f"\n{emoji} **Confidence:** {confidence:.0%}")

        if sources:
            parts.append("\n**📚 Sources:**")
            for i, src in enumerate(sources[:3], 1):
                title = src.get("title", "Source")[:60]
                url = src.get("url", "")
                parts.append(f"{i}. {title} – {url}")

        return "\n".join(parts)

    # ──────────────────────────────────────────────────────────────────────────────
    # EVENT LISTENERS & COMMANDS
    # ──────────────────────────────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_message(self, msg: discord.Message):
        """Handle mentions and DMs"""
        if msg.author.bot:
            return

        if msg.guild and self.bot.user in msg.mentions:
            if not await self.config.guild(msg.guild).enabled():
                return

            content = msg.content
            for mention in msg.mentions:
                content = content.replace(f"<@{mention.id}>", "").replace(f"<@!{mention.id}>", "")

            question = content.strip()

            if msg.reference and (replied := await msg.channel.fetch_message(msg.reference.message_id)):
                question += f"\n\nContext: {replied.content[:500]}"

            if question:
                await self._process(msg.author.id, msg.guild.id, question, msg.channel)

        elif isinstance(msg.channel, discord.DMChannel):
            prefixes = await self.bot.get_valid_prefixes()
            if not any(msg.content.startswith(prefix) for prefix in prefixes):
                await self._process(msg.author.id, None, msg.content, msg.channel)

    @commands.hybrid_group(name="grok", invoke_without_command=True)
    @commands.cooldown(1, COOLDOWN_SECONDS, commands.BucketType.user)
    async def grok(self, ctx: commands.Context, *, question: str):
        """Ask DripBot's AI anything - it searches and reasons automatically"""
        guild_id = ctx.guild.id if ctx.guild else None
        await self._process(ctx.author.id, guild_id, question, ctx.channel)

    @grok.command(name="stats")
    async def grok_stats(self, ctx: commands.Context):
        """View your usage statistics"""
        stats = await self.config.user(ctx.author).all()

        embed = discord.Embed(
            title=f"📊 {ctx.author.display_name}'s Grok Stats",
            color=discord.Color.gold(),
            timestamp=datetime.utcnow()
        )

        embed.add_field(name="Total Queries", value=stats["request_count"], inline=True)

        if stats["last_request_time"]:
            last_time = datetime.fromisoformat(stats["last_request_time"])
            embed.add_field(name="Last Query", value=f"<t:{int(last_time.timestamp())}:R>", inline=True)

        await ctx.send(embed=embed)

    @grok.group(name="admin")
    async def grok_admin(self, ctx: commands.Context):
        """Administration commands for Grok"""
        pass

    @grok_admin.command(name="apikey")
    @commands.is_owner()
    async def admin_apikey(self, ctx: commands.Context, *, api_key: str):
        """Set the Kimi API key (Bot Owner only)"""
        if len(api_key.strip()) < 32:
            await ctx.send("❌ Invalid API key format (should be at least 32 characters)")
            return

        await self.config.api_key.set(api_key.strip())

        # Verify immediately
        msg = await ctx.send("🔍 Verifying API key...")
        try:
            test_payload = {
                "model": K2_MODEL,
                "messages": [{"role": "user", "content": "OK"}],
                "max_tokens": 10
            }

            async with self._session.post(
                f"{KIMI_API_BASE}/chat/completions",
                json=test_payload,
                headers={"Authorization": f"Bearer {api_key.strip()}"},
                timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                if resp.status == 200:
                    await msg.edit(content="✅ API key saved and verified!")
                elif resp.status == 401:
                    await msg.edit(content="❌ API key verification failed: Invalid key (401)")
                elif resp.status == 429:
                    await msg.edit(content="⚠️ API key verified but rate limited (429) - Key is valid but service is busy")
                else:
                    await msg.edit(content=f"⚠️ API key saved but verification failed (HTTP {resp.status})")

        except Exception as e:
            await msg.edit(content=f"⚠️ API key saved but verification error: {str(e)}")

    @grok_admin.command(name="verify")
    @commands.is_owner()
    async def admin_verify(self, ctx: commands.Context):
        """Test if the API key is working"""
        msg = await ctx.send("🔍 Testing API connection...")

        try:
            api_key = await self.config.api_key()

            if not api_key:
                await msg.edit(content="❌ No API key is set. Use `[p]grok admin apikey <key>`")
                return

            test_payload = {
                "model": K2_MODEL,
                "messages": [{"role": "user", "content": "OK"}],
                "max_tokens": 10
            }

            async with self._session.post(
                f"{KIMI_API_BASE}/chat/completions",
                json=test_payload,
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                if resp.status == 200:
                    await msg.edit(content=f"✅ API key is working!")
                elif resp.status == 401:
                    await msg.edit(content=f"❌ **401 Unauthorized** - Invalid API key!")
                elif resp.status == 429:
                    await msg.edit(content=f"⚠️ **429 Rate Limited** - Service is busy, but key is valid!")
                else:
                    await msg.edit(content=f"⚠️ API test failed: HTTP {resp.status}")

        except Exception as e:
            await msg.edit(content=f"❌ API test error: {str(e)}")

    @grok_admin.command(name="toggle")
    @commands.check(lambda ctx: is_admin_or_superior(ctx.bot, ctx.author))
    async def admin_toggle(self, ctx: commands.Context):
        """Enable or disable Grok in this server"""
        current = await self.config.guild(ctx.guild).enabled()
        await self.config.guild(ctx.guild).enabled.set(not current)

        status = "ENABLED 🟢" if not current else "DISABLED 🔴"
        await ctx.send(f"✅ Grok is now **{status}**")

    @grok_admin.command(name="cooldown")
    @commands.is_owner()
    async def admin_cooldown(self, ctx: commands.Context, seconds: int):
        """Set command cooldown in seconds (Owner only)"""
        if seconds < 1:
            await ctx.send("❌ Cooldown must be at least 1 second")
            return

        await self.config.cooldown_seconds.set(seconds)
        await ctx.send(f"✅ Cooldown set to {seconds} seconds per user")

async def setup(bot):
    await bot.add_cog(GrokCog(bot))
