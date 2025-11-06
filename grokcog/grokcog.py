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
    """Grok AI integration for Red-DiscordBot."""

    __version__ = "1.0.0"

    def __init__(self, bot):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=987654321098765, force_registration=True)

        self.config.register_global(
            api_key=None,
            model="grok-beta",
            max_tokens=500,
            timeout=30,
            max_retries=3,
            system_prompt="You are Grok, a helpful and witty AI assistant.",
        )
        self.config.register_guild(enabled=True, cooldown_seconds=30, max_input_length=2000)
        self.config.register_user(request_count=0, last_request_time=None, rate_limited_until=None)

        self._active_requests: Dict[int, asyncio.Task] = {}

    async def cog_unload(self):
        for task in self._active_requests.values():
            if not task.done():
                task.cancel()
        self._active_requests.clear()

    async def _check_rate_limit(self, user_id: int) -> Optional[str]:
        user_cfg = await self.config.user_from_id(user_id).all()
        if user_cfg["rate_limited_until"]:
            until = datetime.fromisoformat(user_cfg["rate_limited_until"])
            if datetime.now() < until:
                remaining = int((until - datetime.now()).total_seconds())
                return f"⏱️ Rate limited. Retry in {remaining}s."
        return None

    async def _apply_penalty(self, user_id: int):
        until = (datetime.now() + timedelta(seconds=600)).isoformat()
        await self.config.user_from_id(user_id).rate_limited_until.set(until)

    @staticmethod
    def _call_grok_sync(api_key: str, model: str, system_prompt: str, prompt: str, max_tokens: int, timeout: int, max_retries: int) -> str:
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt},
            ],
            "max_tokens": max_tokens,
            "temperature": 0.7,
        }

        for attempt in range(max_retries):
            try:
                log.debug(f"Attempt {attempt + 1}/{max_retries} to call Grok API")
                req = Request(
                    "https://api.x.ai/v1/chat/completions",
                    data=json.dumps(payload).encode("utf-8"),
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                    method="POST",
                )
                
                resp = urlopen(req, timeout=timeout)
                result = json.loads(resp.read().decode("utf-8"))
                content = result["choices"][0]["message"]["content"].strip()
                log.debug(f"Grok API success: {len(content)} chars")
                return content

            except HTTPError as e:
                log.warning(f"HTTP {e.code}: {e.reason}")
                
                if e.code == 429:
                    log.error("Rate limited by API")
                    raise commands.UserFeedbackCheckFailure("❌ Rate limited (10 min)")
                
                if e.code >= 500 and attempt < max_retries - 1:
                    log.warning(f"Server error, retrying...")
                    continue
                
                if e.code >= 400:
                    try:
                        err_data = e.read().decode("utf-8")
                        err = json.loads(err_data)
                        msg = err.get("error", {}).get("message", err_data[:100])
                    except:
                        msg = f"HTTP {e.code}"
                    log.error(f"API error: {msg}")
                    raise commands.UserFeedbackCheckFailure(f"❌ {msg}")

            except URLError as e:
                log.warning(f"URLError: {e.reason}")
                if attempt < max_retries - 1:
                    continue
                raise commands.UserFeedbackCheckFailure("❌ Network error")

            except commands.UserFeedbackCheckFailure:
                raise
            
            except Exception as e:
                log.error(f"Unexpected error (attempt {attempt + 1}): {type(e).__name__}: {e}")
                if attempt < max_retries - 1:
                    continue
                raise commands.UserFeedbackCheckFailure(f"❌ {type(e).__name__}: {str(e)[:100]}")

        raise commands.UserFeedbackCheckFailure("❌ Max retries exceeded")

    async def _process(self, user_id: int, guild_id: int, question: str, ctx):
        guild_cfg = await self.config.guild_from_id(guild_id).all()
        if not guild_cfg["enabled"]:
            return await ctx.send("❌ Disabled")

        if len(question) > guild_cfg["max_input_length"]:
            return await ctx.send("❌ Too long")

        if not question.strip():
            return await ctx.send("❌ Empty")

        if msg := await self._check_rate_limit(user_id):
            return await ctx.send(msg)

        if user_id in self._active_requests and not self._active_requests[user_id].done():
            return await ctx.send("❌ Already processing")

        api_key = await self.config.api_key()
        if not api_key:
            return await ctx.send("❌ No API key")

        self._active_requests[user_id] = asyncio.current_task()

        try:
            log.debug(f"Processing request from user {user_id}: {question[:50]}")
            response = await asyncio.to_thread(
                self._call_grok_sync,
                api_key,
                await self.config.model(),
                await self.config.system_prompt(),
                question,
                await self.config.max_tokens(),
                await self.config.timeout(),
                await self.config.max_retries(),
            )
            
            for page in pagify(response, page_length=2000):
                await ctx.send(page)
            
            async with self.config.user_from_id(user_id).all() as cfg:
                cfg["request_count"] += 1
                cfg["last_request_time"] = datetime.now().isoformat()

        except commands.UserFeedbackCheckFailure as e:
            await ctx.send(str(e))
        except Exception as e:
            log.error(f"Error: {type(e).__name__}: {e}", exc_info=True)
            await ctx.send(f"❌ {type(e).__name__}: {str(e)[:200]}")
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
        """Ask Grok."""
        await self._process(ctx.author.id, ctx.guild.id, question, ctx)

    @grok.command(name="stats")
    async def stats(self, ctx: commands.Context):
        """Stats."""
        cfg = await self.config.user(ctx.author).all()
        embed = discord.Embed(title="Stats", color=discord.Color.blue())
        embed.add_field(name="Requests", value=cfg["request_count"])
        if cfg["last_request_time"]:
            ts = int(datetime.fromisoformat(cfg["last_request_time"]).timestamp())
            embed.add_field(name="Last", value=f"<t:{ts}:R>")
        if cfg["rate_limited_until"]:
            until = datetime.fromisoformat(cfg["rate_limited_until"])
            if datetime.now() < until:
                embed.add_field(name="🔒 Locked", value=f"<t:{int(until.timestamp())}:R>")
        await ctx.send(embed=embed)

    @grok.group(name="set")
    @checks.admin_or_permissions(manage_guild=True)
    async def grok_set(self, ctx):
        pass

    @grok_set.command(name="apikey")
    @checks.is_owner()
    async def apikey(self, ctx: commands.Context, key: str):
        """Set API key from https://console.x.ai/"""
        await ctx.message.delete()
        await self.config.api_key.set(key)
        await ctx.send("✅ API key set", delete_after=15)

    @grok_set.command(name="toggle")
    @checks.admin_or_permissions(manage_guild=True)
    async def toggle(self, ctx: commands.Context):
        """Toggle on/off."""
        cur = await self.config.guild(ctx.guild).enabled()
        await self.config.guild(ctx.guild).enabled.set(not cur)
        await ctx.send(f"Grok {'✅ on' if not cur else '❌ off'}")

    @grok_set.command(name="maxtokens")
    @checks.is_owner()
    async def maxtokens(self, ctx: commands.Context, n: int):
        """Max tokens (50-4000)."""
        if not 50 <= n <= 4000:
            return await ctx.send("❌ 50-4000")
        await self.config.max_tokens.set(n)
        await ctx.send(f"✅ {n}")

    @grok_set.command(name="timeout")
    @checks.is_owner()
    async def timeout_cmd(self, ctx: commands.Context, n: int):
        """Timeout (10-120s)."""
        if not 10 <= n <= 120:
            return await ctx.send("❌ 10-120")
        await self.config.timeout.set(n)
        await ctx.send(f"✅ {n}s")

    @grok_set.command(name="show")
    @checks.admin_or_permissions(manage_guild=True)
    async def show_settings(self, ctx: commands.Context):
        """Show config."""
        g = await self.config.all()
        guild = await self.config.guild(ctx.guild).all()
        embed = discord.Embed(title="Config", color=discord.Color.gold())
        embed.add_field(
            name="Global",
            value=f"Model: {g['model']}\nTokens: {g['max_tokens']}\nTimeout: {g['timeout']}s\nKey: {'✅' if g['api_key'] else '❌'}",
        )
        embed.add_field(
            name="Server",
            value=f"Enabled: {guild['enabled']}\nMax input: {guild['max_input_length']}",
        )
        await ctx.send(embed=embed)

    async def cog_command_error(self, ctx, err):
        if hasattr(ctx, "_handled"):
            return
        if isinstance(err, commands.CommandOnCooldown):
            await ctx.send(f"⏱️ {err.retry_after:.1f}s")
        elif isinstance(err, commands.CommandInvokeError) and isinstance(
            err.original, commands.UserFeedbackCheckFailure
        ):
            await ctx.send(str(err.original))
        else:
            log.error(f"Error: {err}", exc_info=err)
        ctx._handled = True


async def setup(bot):
    await bot.add_cog(GrokCog(bot))
