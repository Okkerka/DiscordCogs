import asyncio
import json
import logging
from typing import Optional, Dict
from datetime import datetime, timedelta
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

import discord
from redbot.core import commands, Config, checks
from redbot.core.utils.chat_formatting import pagify

log = logging.getLogger("red.grokcog")


class GrokCog(commands.Cog):
    """Production-ready fact-checker with LLaMA 3.3 70B."""

    __version__ = "1.0.0"

    def __init__(self, bot):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=987654321098765, force_registration=True)

        self.config.register_global(
            api_key=None,
            model="llama-3.3-70b-versatile",
            max_tokens=500,
            timeout=30,
            max_retries=3,
        )
        self.config.register_guild(enabled=True, max_input_length=2000)
        self.config.register_user(request_count=0, last_request_time=None)

        self._active_requests: Dict[int, asyncio.Task] = {}

    async def cog_unload(self):
        for task in self._active_requests.values():
            if not task.done():
                task.cancel()
        self._active_requests.clear()

    @staticmethod
    def _web_search(query: str) -> str:
        try:
            from duckduckgo_search import DDGS
            results = DDGS().text(query, max_results=5)
            if not results:
                return "No search results found."
            
            formatted = "SEARCH RESULTS:\n"
            for i, r in enumerate(results, 1):
                formatted += f"\n{i}. {r['title']}\n   {r['body'][:300]}\n"
            return formatted
        except ImportError:
            return "[ERROR] Run: pip install duckduckgo-search"
        except Exception as e:
            log.error(f"Search error: {e}")
            return f"[Search failed]"

    @staticmethod
    def _fact_check_sync(api_key: str, model: str, claim: str, search_data: str, timeout: int, max_retries: int) -> str:
        system_prompt = """You are a fact-checker. Based on the search results provided, determine if the claim is TRUE, FALSE, or UNCLEAR.

Format your response as:
VERDICT: [TRUE/FALSE/UNCLEAR]
REASON: [Brief explanation citing the search results]"""

        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"CLAIM: {claim}\n\n{search_data}"},
            ],
            "max_tokens": 300,
            "temperature": 0.3,
        }

        for attempt in range(max_retries):
            try:
                req = Request(
                    "https://api.groq.com/openai/v1/chat/completions",
                    data=json.dumps(payload).encode("utf-8"),
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                    method="POST",
                )
                
                resp = urlopen(req, timeout=timeout)
                result = json.loads(resp.read().decode("utf-8"))
                return result["choices"][0]["message"]["content"].strip()

            except HTTPError as e:
                if e.code == 429:
                    raise commands.UserFeedbackCheckFailure("❌ Rate limited by API")
                if e.code == 401:
                    raise commands.UserFeedbackCheckFailure("❌ Invalid API key")
                if e.code >= 500 and attempt < max_retries - 1:
                    continue
                if e.code >= 400:
                    raise commands.UserFeedbackCheckFailure(f"❌ API error {e.code}")

            except URLError:
                if attempt < max_retries - 1:
                    continue
                raise commands.UserFeedbackCheckFailure("❌ Network error")

            except commands.UserFeedbackCheckFailure:
                raise
            except Exception as e:
                log.error(f"API error: {e}")
                if attempt < max_retries - 1:
                    continue
                raise commands.UserFeedbackCheckFailure("❌ API error")

        raise commands.UserFeedbackCheckFailure("❌ Max retries")

    async def _process(self, user_id: int, guild_id: int, question: str, ctx):
        guild_cfg = await self.config.guild_from_id(guild_id).all()
        if not guild_cfg["enabled"]:
            return await ctx.send("❌ Disabled")

        if len(question) > guild_cfg["max_input_length"]:
            return await ctx.send("❌ Too long")

        if not question.strip():
            return await ctx.send("❌ Empty")

        if user_id in self._active_requests and not self._active_requests[user_id].done():
            return await ctx.send("❌ Already processing")

        api_key = await self.config.api_key()
        if not api_key:
            return await ctx.send("❌ No API key (set in DMs: `>grok set apikey KEY`)")

        self._active_requests[user_id] = asyncio.current_task()
        search_msg = None

        try:
            search_msg = await ctx.send("🔍 Searching...")
            search_data = await asyncio.to_thread(self._web_search, question)
            await search_msg.edit(content="📊 Fact-checking...")

            response = await asyncio.to_thread(
                self._fact_check_sync,
                api_key,
                await self.config.model(),
                question,
                search_data,
                await self.config.timeout(),
                await self.config.max_retries(),
            )
            
            await search_msg.delete()
            await ctx.send(response)
            
            async with self.config.user_from_id(user_id).all() as cfg:
                cfg["request_count"] += 1
                cfg["last_request_time"] = datetime.now().isoformat()

        except commands.UserFeedbackCheckFailure as e:
            if search_msg:
                await search_msg.delete()
            await ctx.send(str(e))
        except asyncio.CancelledError:
            if search_msg:
                await search_msg.delete()
        except Exception as e:
            if search_msg:
                await search_msg.delete()
            log.error(f"Error: {e}", exc_info=True)
            await ctx.send("❌ Error")
        finally:
            self._active_requests.pop(user_id, None)

    @commands.Cog.listener()
    async def on_message(self, msg: discord.Message):
        if msg.author.bot or not msg.guild or self.bot.user not in msg.mentions:
            return
        q = msg.content
        for u in msg.mentions:
            q = q.replace(f"<@{u.id}>", "").replace(f"<@!{u.id}>", "")
        if q := q.strip():
            await self._process(msg.author.id, msg.guild.id, q, msg)

    @commands.group(name="grok", invoke_without_command=True)
    @commands.guild_only()
    @commands.cooldown(1, 30, commands.BucketType.user)
    async def grok(self, ctx: commands.Context, *, question: str):
        """Fact-check a claim."""
        await self._process(ctx.author.id, ctx.guild.id, question, ctx)

    @grok.command(name="stats")
    @commands.guild_only()
    async def stats(self, ctx: commands.Context):
        """Your fact-check stats."""
        cfg = await self.config.user(ctx.author).all()
        embed = discord.Embed(title="Stats", color=discord.Color.blue())
        embed.add_field(name="Checks", value=cfg["request_count"])
        if cfg["last_request_time"]:
            ts = int(datetime.fromisoformat(cfg["last_request_time"]).timestamp())
            embed.add_field(name="Last", value=f"<t:{ts}:R>")
        await ctx.send(embed=embed)

    @grok.group(name="set")
    async def grok_set(self, ctx):
        """Settings."""
        pass

    @grok_set.command(name="apikey")
    async def apikey(self, ctx: commands.Context, key: str):
        """Set API key. Get from https://console.groq.com/"""
        if not await self.bot.is_owner(ctx.author):
            return await ctx.send("❌ Owner only")
        
        if not key or len(key) < 10:
            return await ctx.send("❌ Invalid key")
        
        await self.config.api_key.set(key)
        await ctx.send("✅ API key saved")

    @grok_set.command(name="toggle")
    @checks.admin_or_permissions(manage_guild=True)
    @commands.guild_only()
    async def toggle(self, ctx: commands.Context):
        """Toggle on/off."""
        cur = await self.config.guild(ctx.guild).enabled()
        await self.config.guild(ctx.guild).enabled.set(not cur)
        await ctx.send(f"{'✅ On' if not cur else '❌ Off'}")

    @grok_set.command(name="maxtokens")
    async def maxtokens(self, ctx: commands.Context, n: int):
        """Max tokens (50-4000)."""
        if not await self.bot.is_owner(ctx.author):
            return await ctx.send("❌ Owner only")
        
        if not 50 <= n <= 4000:
            return await ctx.send("❌ 50-4000")
        
        await self.config.max_tokens.set(n)
        await ctx.send(f"✅ {n}")

    @grok_set.command(name="timeout")
    async def timeout_cmd(self, ctx: commands.Context, n: int):
        """Timeout (10-120s)."""
        if not await self.bot.is_owner(ctx.author):
            return await ctx.send("❌ Owner only")
        
        if not 10 <= n <= 120:
            return await ctx.send("❌ 10-120")
        
        await self.config.timeout.set(n)
        await ctx.send(f"✅ {n}s")

    @grok_set.command(name="show")
    @checks.admin_or_permissions(manage_guild=True)
    @commands.guild_only()
    async def show_settings(self, ctx: commands.Context):
        """Show config."""
        g = await self.config.all()
        guild = await self.config.guild(ctx.guild).all()
        embed = discord.Embed(title="Config", color=discord.Color.gold())
        embed.add_field(
            name="Global",
            value=f"Model: {g['model']}\nTokens: {g['max_tokens']}\nTimeout: {g['timeout']}s\nKey: {'✅' if g['api_key'] else '❌'}",
        )
        embed.add_field(name="Server", value=f"Enabled: {guild['enabled']}")
        await ctx.send(embed=embed)

    async def cog_command_error(self, ctx, err):
        if hasattr(ctx, "_handled"):
            return
        if isinstance(err, commands.CommandOnCooldown):
            await ctx.send(f"⏱️ {err.retry_after:.1f}s")
        else:
            log.error(f"Error: {err}", exc_info=err)
        ctx._handled = True


async def setup(bot):
    await bot.add_cog(GrokCog(bot))
