# grokcog.py - ULTRA-ENHANCED PRODUCTION VERSION
"""
GrokCog - Advanced AI assistant using Kimi K2 Thinking
Version: 4.0.0 - Production Ready
"""

import asyncio
import json
import logging
import hashlib
from datetime import datetime, timedelta
from typing import Dict, Optional, Tuple, Any, List, Literal
from dataclasses import dataclass, asdict

import discord
from redbot.core import commands, Config, checks
from redbot.core.utils.mod import is_admin_or_superior
from redbot.core.utils.chat_formatting import pagify

log = logging.getLogger("red.grokcog")

# ──────────────────────────────────────────────────────────────────────────────
# Constants & Configuration
# ──────────────────────────────────────────────────────────────────────────────

K2_MODEL = "kimi-k2-thinking"
K2_PROMPT = """You are DripBot's AI brain, powered by Kimi K2 with native search and reasoning.

RESPOND WITH VALID JSON ONLY:
{
  "answer": "Comprehensive answer with inline citations [1], [2]",
  "confidence": 0.95,
  "sources": [{"title": "Source Title", "url": "https://example.com"}],
  "reasoning_steps": ["Step 1", "Step 2"]
}

RULES:
- Search automatically for factual queries
- Cite sources using bracket notation
- Include reasoning steps for complex queries
- Confidence: 0.0-1.0 (be honest about uncertainty)
- Format: Markdown supported, max 4 paragraphs
"""

CACHE_TTL = 3600
MAX_CACHE_SIZE = 512
MAX_INPUT_LENGTH = 4000

@dataclass
class K2Response:
    answer: str
    confidence: float
    sources: List[Dict[str, str]]
    reasoning_steps: Optional[List[str]] = None

class GrokCog(commands.Cog):
    """🧠 DripBot's AI brain - Advanced reasoning with Kimi K2"""

    __version__ = "4.0.0"

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=0x4B324B32, force_registration=True)

        self.config.register_global(
            api_key=None,
            timeout=60,
            max_retries=3,
            enable_caching=True,
            log_queries=False
        )

        self.config.register_guild(
            enabled=True,
            max_input_length=MAX_INPUT_LENGTH,
            require_admin_mentions=False,
            default_temperature=0.3
        )

        self.config.register_user(
            request_count=0,
            total_tokens_used=0,
            last_request_time=None,
            is_blacklisted=False
        )

        self._active_requests: Dict[int, asyncio.Task] = {}
        self._cache: Dict[str, Tuple[float, str]] = {}
        self._session: Optional[aiohttp.ClientSession] = None
        self._lock = asyncio.Lock()

    async def cog_load(self) -> None:
        self._session = aiohttp.ClientSession(
            headers={"User-Agent": f"Red-DiscordBot-GrokCog/{self.__version__}"}
        )
        log.info(f"GrokCog v{self.__version__} loaded")

    async def cog_unload(self) -> None:
        if self._session:
            await self._session.close()
        for task in self._active_requests.values():
            task.cancel()
        self._active_requests.clear()

    # ──────────────────────────────────────────────────────────────────────────────
    # Core Utilities
    # ──────────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _generate_cache_key(text: str) -> str:
        return hashlib.sha256(text.strip().lower().encode()).hexdigest()[:16]

    async def _get_cached(self, key: str) -> Optional[str]:
        if not await self.config.enable_caching():
            return None
        async with self._lock:
            if item := self._cache.get(key):
                ts, val = item
                if datetime.utcnow().timestamp() - ts < CACHE_TTL:
                    return val
                self._cache.pop(key, None)
        return None

    async def _set_cached(self, key: str, val: str) -> None:
        if not await self.config.enable_caching():
            return
        async with self._lock:
            self._cache[key] = (datetime.utcnow().timestamp(), val)
            if len(self._cache) > MAX_CACHE_SIZE:
                for k, _ in sorted(self._cache.items(), key=lambda x: x[1][0])[:64]:
                    self._cache.pop(k, None)

    async def _delete_safely(self, msg: Optional[discord.Message]) -> None:
        if msg:
            try:
                await msg.delete()
            except:
                pass

    # ──────────────────────────────────────────────────────────────────────────────
    # Kimi K2 API
    # ──────────────────────────────────────────────────────────────────────────────

    async def _call_k2(self, question: str, temperature: float) -> K2Response:
        api_key = await self.config.api_key()
        if not api_key:
            raise ValueError("API key not set. Use `[p]grok admin apikey`")

        payload = {
            "model": K2_MODEL,
            "messages": [
                {"role": "system", "content": K2_PROMPT},
                {"role": "user", "content": f"Query: {question}\nTime: {datetime.utcnow().isoformat()}"}
            ],
            "temperature": temperature,
            "max_tokens": 2000,
            "tools": [{"type": "builtin", "name": "search"}],
            "response_format": {"type": "json_object"}
        }

        for attempt in range(await self.config.max_retries()):
            try:
                async with self._session.post(
                    "https://api.moonshot.cn/v1/chat/completions",
                    json=payload,
                    headers={"Authorization": f"Bearer {api_key}"},
                    timeout=aiohttp.ClientTimeout(total=await self.config.timeout())
                ) as resp:
                    if resp.status == 429:
                        await asyncio.sleep(int(resp.headers.get("Retry-After", 5)))
                        continue
                    resp.raise_for_status()
                    data = await resp.json()
                    content = json.loads(data["choices"][0]["message"]["content"])
                    return K2Response(**content)
            except Exception as e:
                if attempt == await self.config.max_retries() - 1:
                    raise
                await asyncio.sleep(2 ** attempt)

    # ──────────────────────────────────────────────────────────────────────────────
    # Query Processing
    # ──────────────────────────────────────────────────────────────────────────────

    async def _validate(self, user_id: int, guild_id: Optional[int], question: str, channel) -> bool:
        if not question.strip():
            await channel.send("❌ Please provide a question.")
            return False

        if guild_id and not await self.config.guild_from_id(guild_id).enabled():
            await channel.send("❌ Grok is disabled in this server.")
            return False

        if await self.config.user_from_id(user_id).is_blacklisted():
            await channel.send("❌ You are blacklisted.")
            return False

        if len(question) > await self.config.guild_from_id(guild_id).max_input_length():
            await channel.send(f"❌ Too long ({len(question)} chars)")
            return False

        if user_id in self._active_requests:
            await channel.send("⏳ Already processing")
            return False

        return True

    async def process_query(self, user_id: int, guild_id: Optional[int], question: str, channel):
        if not await self._validate(user_id, guild_id, question, channel):
            return

        self._active_requests[user_id] = asyncio.current_task()
        status = None

        try:
            key = self._generate_cache_key(question)
            if cached := await self._get_cached(key):
                await channel.send(cached)
                return

            status = await channel.send("🧠 **DripBot is thinking...**")

            temp = await self.config.guild_from_id(guild_id).default_temperature() if guild_id else 0.3
            response = await self._call_k2(question, temp)

            formatted = self._format_response(response)
            await self._delete_safely(status)

            if len(formatted) > 2000:
                for page in pagify(formatted, page_length=1900):
                    await channel.send(page)
            else:
                await channel.send(formatted)

            await self._set_cached(key, formatted)

            async with self.config.user_from_id(user_id).all() as user_data:
                user_data["request_count"] += 1
                user_data["last_request_time"] = datetime.utcnow().isoformat()

            if await self.config.log_queries():
                log.info(f"Query from {user_id}: {question[:50]}...")

        except ValueError as e:
            await self._delete_safely(status)
            await channel.send(f"⚠️ {str(e)}")
        except Exception as e:
            await self._delete_safely(status)
            log.exception(f"Query failed: {e}")
            await channel.send("❌ Error processing query")
        finally:
            self._active_requests.pop(user_id, None)

    def _format_response(self, response: K2Response) -> str:
        parts = [response.answer]

        if response.confidence > 0:
            emoji = "🟢" if response.confidence > 0.8 else "🟡" if response.confidence > 0.6 else "🔴"
            parts.append(f"\n{emoji} ** Confidence:** {response.confidence:.0%}")

        if response.reasoning_steps:
            parts.append("\n** 🤔 Reasoning:**")
            parts.extend(f"• {step}" for step in response.reasoning_steps[:3])

        if response.sources:
            parts.append("\n**📚 Sources:**")
            for idx, src in enumerate(response.sources[:3], 1):
                title = src.get("title", "Untitled")[:80]
                url = src.get("url", "")
                parts.append(f"{idx}. **{title}** – {url}")

        return "\n".join(parts)

    # ──────────────────────────────────────────────────────────────────────────────
    # Event Listeners & Commands
    # ──────────────────────────────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_message(self, msg: discord.Message):
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
                await self.process_query(msg.author.id, msg.guild.id, question, msg.channel)

        elif isinstance(msg.channel, discord.DMChannel):
            if not msg.content.startswith(tuple(await self.bot.get_valid_prefixes())):
                await self.process_query(msg.author.id, None, msg.content, msg.channel)

    @commands.hybrid_group(name="grok", invoke_without_command=True)
    @commands.cooldown(1, COOLDOWN_SECONDS, commands.BucketType.user)
    async def grok(self, ctx: commands.Context, *, question: str):
        """Ask DripBot's AI anything - it searches and reasons automatically"""
        await self.process_query(ctx.author.id, ctx.guild.id if ctx.guild else None, question, ctx.channel)

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
        embed.add_field(name="Tokens Used", value=f"{stats['total_tokens_used']:,}", inline=True)

        if stats["last_request_time"]:
            last_time = datetime.fromisoformat(stats["last_request_time"])
            embed.add_field(name="Last Query", value=f"<t:{int(last_time.timestamp())}:R>", inline=True)

        if stats["is_blacklisted"]:
            embed.add_field(name="⚠️ Status", value="Blacklisted", inline=False)

        await ctx.send(embed=embed)

    @grok.command(name="help")
    async def grok_help(self, ctx: commands.Context):
        """Show detailed help and usage examples"""
        embed = discord.Embed(
            title="🧠 DripBot's Grok - Help Guide",
            description="Advanced AI assistant with built-in search and reasoning",
            color=discord.Color.blue()
        )

        embed.add_field(
            name="🚀 Quick Start",
            value=(
                "• `@DripBot your question here`\n"
                "• DM me directly with your question\n"
                "• `[p]grok your question`\n"
                "• Reply to a message and mention me"
            ),
            inline=False
        )

        embed.add_field(
            name="💡 Features",
            value=(
                "✅ Real-time web search\n"
                "✅ Automatic source citations\n"
                "✅ Confidence scoring\n"
                "✅ Multi-turn conversation context\n"
                "✅ Intelligent caching\n"
                "✅ Rate limiting\n"
                "✅ Error recovery"
            ),
            inline=False
        )

        embed.add_field(
            name="⚙️ Admin Commands",
            value=(
                "`[p]grok admin apikey <key>` - Set API key\n"
                "`[p]grok admin toggle` - Enable/disable\n"
                "`[p]grok admin temperature <0.0-2.0>` - Adjust creativity\n"
                "`[p]grok admin settings` - View all settings"
            ),
            inline=False
        )

        embed.set_footer(text="Powered by Moonshot AI's Kimi K2")
        await ctx.send(embed=embed, ephemeral=True)

    @grok.group(name="admin")
    @commands.check(is_admin_or_superior)
    async def grok_admin(self, ctx: commands.Context):
        """Administration commands for Grok"""
        pass

    @grok_admin.command(name="apikey")
    @commands.is_owner()
    async def admin_apikey(self, ctx: commands.Context, *, api_key: str):
        """Set the Kimi API key (Bot Owner only)"""
        if len(api_key.strip()) < 32:
            await ctx.send("❌ Invalid API key format. Key should be at least 32 characters.")
            return

        await self.config.api_key.set(api_key.strip())

        # Verify key works
        verification_msg = await ctx.send("🔍 Verifying API key...")
        try:
            test_payload = {
                "model": K2_MODEL,
                "messages": [{"role": "user", "content": "Respond with OK"}],
                "max_tokens": 10
            }

            async with self._session.post(
                "https://api.moonshot.cn/v1/chat/completions",
                json=test_payload,
                headers={"Authorization": f"Bearer {api_key.strip()}"},
                timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                if resp.status == 200:
                    await verification_msg.edit(content="✅ API key saved and verified!")
                else:
                    await verification_msg.edit(content=f"⚠️ API key saved but verification failed (HTTP {resp.status})")
        except Exception as e:
            await verification_msg.edit(content=f"⚠️ API key saved but verification error: {str(e)}")

    @grok_admin.command(name="toggle")
    @commands.check(is_admin_or_superior)
    async def admin_toggle(self, ctx: commands.Context):
        """Enable or disable Grok in this server"""
        current = await self.config.guild(ctx.guild).enabled()
        await self.config.guild(ctx.guild).enabled.set(not current)

        status = "ENABLED 🟢" if not current else "DISABLED 🔴"
        await ctx.send(f"✅ Grok is now **{status}** in **{ctx.guild.name}**.")

    @grok_admin.command(name="temperature")
    @commands.check(is_admin_or_superior)
    async def admin_temperature(self, ctx: commands.Context, value: float):
        """Set default temperature (0.0 = focused, 2.0 = creative)"""
        if not 0.0 <= value <= 2.0:
            await ctx.send("❌ Temperature must be between 0.0 and 2.0")
            return

        await self.config.guild(ctx.guild).default_temperature.set(value)

        description = "focused & deterministic" if value < 0.5 else "balanced" if value < 1.0 else "creative & random"
        await ctx.send(f"✅ Temperature set to `{value}` ({description}) in this server.")

    @grok_admin.command(name="settings")
    @commands.check(is_admin_or_superior)
    async def admin_settings(self, ctx: commands.Context):
        """View all Grok settings for this server"""
        guild_cfg = await self.config.guild(ctx.guild).all()
        global_cfg = await self.config.all()

        embed = discord.Embed(
            title=f"⚙️ Grok Settings – {ctx.guild.name}",
            color=discord.Color.blue(),
            timestamp=datetime.utcnow()
        )

        embed.add_field(
            name="Server Configuration",
            value=(
                f"Status: {'🟢 Enabled' if guild_cfg['enabled'] else '🔴 Disabled'}\n"
                f"Max Input: `{guild_cfg['max_input_length']}` characters\n"
                f"Temperature: `{guild_cfg['default_temperature']}`\n"
                f"Admin Mentions Only: {'✅' if guild_cfg['require_admin_mentions'] else '❌'}"
            ),
            inline=False
        )

        embed.add_field(
            name="Global Configuration",
            value=(
                f"Caching: {'✅ Enabled' if global_cfg['enable_caching'] else '❌ Disabled'}\n"
                f"Query Logging: {'✅ Enabled' if global_cfg['log_queries'] else '❌ Disabled'}\n"
                f"API Timeout: `{global_cfg['timeout']}` seconds\n"
                f"Max Retries: `{global_cfg['max_retries']}`"
            ),
            inline=False
        )

        await ctx.send(embed=embed)

    @grok_admin.command(name="blacklist")
    @commands.is_owner()
    async def admin_blacklist(self, ctx: commands.Context, user: discord.User, *, reason: str = None):
        """Blacklist a user from using Grok (Owner only)"""
        await self.config.user(user).is_blacklisted.set(True)
        await ctx.send(f"✅ **{user}** has been blacklisted from using Grok.")
        if reason:
            log.info(f"User {user.id} blacklisted. Reason: {reason}")

    @grok_admin.command(name="unblacklist")
    @commands.is_owner()
    async def admin_unblacklist(self, ctx: commands.Context, user: discord.User):
        """Remove user from Grok blacklist (Owner only)"""
        await self.config.user(user).is_blacklisted.set(False)
        await ctx.send(f"✅ **{user}** has been removed from the blacklist.")

    # ──────────────────────────────────────────────────────────────────────────────
    # Error Handling
    # ──────────────────────────────────────────────────────────────────────────────

    async def cog_command_error(self, ctx: commands.Context, error: commands.CommandError):
        """Global error handler for this cog"""
        if isinstance(error, commands.CommandOnCooldown):
            await ctx.send(
                f"⏱️ **Cooldown active** – Wait `{error.retry_after:.1f}` seconds",
                ephemeral=True
            )
        elif isinstance(error, commands.CheckFailure):
            await ctx.send(f"❌ **Permission Denied** – {error}", ephemeral=True)
        elif isinstance(error, commands.BadArgument):
            await ctx.send(f"⚠️ **Invalid Argument** – {error}", ephemeral=True)
        else:
            log.exception(f"Unhandled error in {ctx.command}: {error}")
            await ctx.send("❌ **Internal Error** – Check bot logs", ephemeral=True)

async def setup(bot: commands.Bot):
    await bot.add_cog(GrokCog(bot))
