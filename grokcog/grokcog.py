import asyncio
import logging
from typing import Optional, Dict
from datetime import datetime, timedelta

import discord
import httpx
from redbot.core import commands, Config, checks
from redbot.core.utils.chat_formatting import pagify

log = logging.getLogger("red.grokcog")


class GrokCog(commands.Cog):
    """Production-ready Grok AI integration for Red-DiscordBot."""

    __version__ = "1.0.0"
    __author__ = "YourName"

    def __init__(self, bot):
        self.bot = bot
        self.config = Config.get_conf(
            self, identifier=987654321098765, force_registration=True
        )

        # Global settings (bot-wide)
        default_global = {
            "api_key": None,
            "model": "grok-beta",
            "max_tokens": 500,
            "timeout": 30,
            "max_retries": 3,
            "system_prompt": "You are Grok, a helpful and witty AI assistant.",
        }

        # Guild settings (per-server)
        default_guild = {
            "enabled": True,
            "cooldown_seconds": 30,
            "max_input_length": 2000,
        }

        # User settings (per-user stats)
        default_user = {
            "request_count": 0,
            "last_request_time": None,
            "rate_limited_until": None,
        }

        self.config.register_global(**default_global)
        self.config.register_guild(**default_guild)
        self.config.register_user(**default_user)

        # In-memory tracking
        self._active_requests: Dict[int, asyncio.Task] = {}
        self.base_url = "https://api.x.ai/v1"
        self._client_session: Optional[httpx.AsyncClient] = None

    async def cog_load(self):
        """Initialize persistent client session."""
        try:
            self._client_session = httpx.AsyncClient(
                timeout=30.0,
                limits=httpx.Limits(max_connections=5, max_keepalive_connections=2),
            )
            api_key = await self.config.api_key()
            if api_key:
                log.info("GrokCog loaded successfully with API key")
            else:
                log.warning("GrokCog loaded but API key not set. Use [p]grok set apikey")
        except Exception as e:
            log.error(f"Failed to initialize GrokCog: {e}", exc_info=True)

    async def cog_unload(self):
        """Cleanup client session and cancel active requests."""
        # Cancel all active requests
        for user_id, task in self._active_requests.items():
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self._active_requests.clear()

        # Close HTTP client
        if self._client_session:
            await self._client_session.aclose()
            log.info("GrokCog unloaded, client session closed")

    async def _validate_api_key(self) -> bool:
        """Validate that API key is configured."""
        api_key = await self.config.api_key()
        return bool(api_key)

    async def _check_user_rate_limit(self, user_id: int) -> Optional[str]:
        """Check if user is rate limited. Returns error message if limited."""
        user_config = await self.config.user_from_id(user_id).all()

        # Check penalty-based rate limit
        if user_config["rate_limited_until"]:
            limited_until = datetime.fromisoformat(user_config["rate_limited_until"])
            if datetime.now() < limited_until:
                remaining = int((limited_until - datetime.now()).total_seconds())
                return f"⏱️ Rate limited. Retry in {remaining}s."

        return None

    async def _apply_penalty(self, user_id: int, duration: int = 600):
        """Apply rate limit penalty (default 10 minutes)."""
        penalty_until = (datetime.now() + timedelta(seconds=duration)).isoformat()
        await self.config.user_from_id(user_id).rate_limited_until.set(penalty_until)
        log.warning(f"Rate limit penalty applied to user {user_id} for {duration}s")

    async def _make_api_request(
        self, prompt: str, user_id: int, max_tokens: Optional[int] = None
    ) -> str:
        """Make request to Grok API with exponential backoff retry."""
        # Validate config
        api_key = await self.config.api_key()
        if not api_key:
            raise RuntimeError("API key not configured. Contact bot owner.")

        model = await self.config.model()
        system_prompt = await self.config.system_prompt()
        timeout = await self.config.timeout()
        max_retries = await self.config.max_retries()

        if max_tokens is None:
            max_tokens = await self.config.max_tokens()

        # Prepare request payload
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt},
        ]

        payload = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": 0.7,
        }

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

        # Retry loop with exponential backoff
        for attempt in range(max_retries):
            try:
                if not self._client_session:
                    raise RuntimeError("HTTP client not initialized")

                response = await self._client_session.post(
                    f"{self.base_url}/chat/completions",
                    json=payload,
                    headers=headers,
                    timeout=timeout,
                )

                # Handle rate limit (429)
                if response.status_code == 429:
                    log.warning(f"Rate limit (429) for user {user_id}")
                    await self._apply_penalty(user_id, 600)
                    raise commands.UserFeedbackCheckFailure(
                        "❌ API rate limit hit. Penalty applied (10 min)."
                    )

                # Handle server errors (5xx) with retry
                if response.status_code >= 500:
                    if attempt < max_retries - 1:
                        backoff = 2 ** attempt
                        log.warning(
                            f"Server error {response.status_code} for user {user_id}, "
                            f"retry {attempt + 1}/{max_retries} in {backoff}s"
                        )
                        await asyncio.sleep(backoff)
                        continue

                # Handle client errors (4xx, non-429)
                if response.status_code >= 400:
                    try:
                        error_data = response.json()
                        error_msg = error_data.get("error", {}).get("message", "Unknown")
                    except Exception:
                        error_msg = response.text[:200]

                    log.error(f"API error {response.status_code}: {error_msg}")
                    raise commands.UserFeedbackCheckFailure(f"❌ API error: {error_msg}")

                # Success
                result = response.json()
                content = result["choices"][0]["message"]["content"].strip()

                # Update user stats
                async with self.config.user_from_id(user_id).all() as user_data:
                    user_data["request_count"] += 1
                    user_data["last_request_time"] = datetime.now().isoformat()

                return content

            except httpx.TimeoutException:
                if attempt < max_retries - 1:
                    backoff = 2 ** attempt
                    log.warning(f"Timeout for user {user_id}, retry in {backoff}s")
                    await asyncio.sleep(backoff)
                    continue
                else:
                    raise commands.UserFeedbackCheckFailure(
                        "⏱️ Request timed out. API unresponsive. Try again later."
                    )

            except httpx.HTTPError as e:
                if attempt < max_retries - 1:
                    backoff = 2 ** attempt
                    log.warning(f"HTTP error for user {user_id}: {e}, retry in {backoff}s")
                    await asyncio.sleep(backoff)
                    continue
                else:
                    log.error(f"HTTP error after retries: {e}")
                    raise commands.UserFeedbackCheckFailure(
                        "❌ Network error. Check your connection and try again."
                    )

            except commands.UserFeedbackCheckFailure:
                raise

            except Exception as e:
                log.error(f"Unexpected error for user {user_id}: {e}", exc_info=True)
                raise commands.UserFeedbackCheckFailure(
                    "❌ Unexpected error. Contact bot owner."
                )

        # If we exit the loop without returning, all retries failed
        raise commands.UserFeedbackCheckFailure(
            f"❌ All {max_retries} retries failed. Try again later."
        )

    @commands.group(name="grok", invoke_without_command=True)
    @commands.guild_only()
    @commands.cooldown(1, 30, commands.BucketType.user)
    async def grok(self, ctx: commands.Context, *, question: str):
        """Ask Grok AI a question. Example: [p]grok What is Python?"""
        # Check if enabled in guild
        if not await self.config.guild(ctx.guild).enabled():
            return await ctx.send("❌ Grok is disabled in this server.")

        # Check input length
        max_length = await self.config.guild(ctx.guild).max_input_length()
        if len(question) > max_length:
            return await ctx.send(
                f"❌ Question too long ({len(question)}/{max_length} chars)."
            )

        # Check input is not empty
        if not question.strip():
            return await ctx.send("❌ Question cannot be empty.")

        # Check user rate limit
        rate_msg = await self._check_user_rate_limit(ctx.author.id)
        if rate_msg:
            return await ctx.send(rate_msg)

        # Check if user already has active request
        if ctx.author.id in self._active_requests:
            task = self._active_requests[ctx.author.id]
            if not task.done():
                return await ctx.send("❌ You already have a request in progress.")
            else:
                del self._active_requests[ctx.author.id]

        # Validate API key
        if not await self._validate_api_key():
            return await ctx.send("❌ Bot owner hasn't configured API key yet.")

        async with ctx.typing():
            task = asyncio.current_task()
            self._active_requests[ctx.author.id] = task

            try:
                response = await self._make_api_request(question, ctx.author.id)

                # Split long responses
                if len(response) > 2000:
                    pages = list(pagify(response, delims=["\n\n", "\n", " "], page_length=2000))
                    for i, page in enumerate(pages, 1):
                        prefix = f"[{i}/{len(pages)}] " if len(pages) > 1 else ""
                        await ctx.send(prefix + page)
                else:
                    await ctx.send(response)

                log.info(f"Grok request succeeded for {ctx.author} in {ctx.guild.name}")

            except commands.UserFeedbackCheckFailure as e:
                await ctx.send(str(e))

            except asyncio.CancelledError:
                await ctx.send("❌ Request was cancelled.")
                log.info(f"Grok request cancelled for {ctx.author}")

            except Exception as e:
                log.error(f"Grok error for {ctx.author}: {e}", exc_info=True)
                await ctx.send(
                    "❌ An unexpected error occurred. Check logs or contact bot owner."
                )

            finally:
                self._active_requests.pop(ctx.author.id, None)

    @grok.command(name="stats")
    async def grok_stats(self, ctx: commands.Context):
        """View your Grok usage statistics."""
        user_data = await self.config.user(ctx.author).all()

        embed = discord.Embed(
            title="📊 Your Grok Statistics",
            color=discord.Color.blue(),
            timestamp=datetime.now(),
        )

        embed.add_field(name="Requests", value=str(user_data["request_count"]), inline=True)

        if user_data["last_request_time"]:
            last_time = datetime.fromisoformat(user_data["last_request_time"])
            embed.add_field(
                name="Last Request",
                value=f"<t:{int(last_time.timestamp())}:R>",
                inline=True,
            )
        else:
            embed.add_field(name="Last Request", value="Never", inline=True)

        if user_data["rate_limited_until"]:
            limited_until = datetime.fromisoformat(user_data["rate_limited_until"])
            if datetime.now() < limited_until:
                embed.add_field(
                    name="🔒 Rate Limited Until",
                    value=f"<t:{int(limited_until.timestamp())}:R>",
                    inline=False,
                )

        await ctx.send(embed=embed)

    @grok.group(name="set", invoke_without_command=True)
    @checks.admin_or_permissions(manage_guild=True)
    async def grok_set(self, ctx: commands.Context):
        """Configure Grok settings."""
        await ctx.send_help()

    @grok_set.command(name="apikey")
    @checks.is_owner()
    async def set_apikey(self, ctx: commands.Context, api_key: str):
        """Set Grok API key (Owner only). Get from https://console.x.ai/"""
        try:
            await ctx.message.delete()
        except discord.HTTPException:
            pass

        await self.config.api_key.set(api_key)
        await ctx.send("✅ API key set. Use `[p]grok What's 2+2?` to test.", delete_after=15)
        log.info(f"API key set by {ctx.author}")

    @grok_set.command(name="toggle")
    @checks.admin_or_permissions(manage_guild=True)
    async def set_toggle(self, ctx: commands.Context):
        """Toggle Grok on/off for this server."""
        current = await self.config.guild(ctx.guild).enabled()
        await self.config.guild(ctx.guild).enabled.set(not current)
        status = "✅ enabled" if not current else "❌ disabled"
        await ctx.send(f"Grok is now {status}.")

    @grok_set.command(name="maxtokens")
    @checks.is_owner()
    async def set_maxtokens(self, ctx: commands.Context, tokens: int):
        """Set max response tokens (50-4000). Lower = faster/cheaper."""
        if not 50 <= tokens <= 4000:
            return await ctx.send("❌ Must be 50-4000 tokens.")
        await self.config.max_tokens.set(tokens)
        await ctx.send(f"✅ Max tokens set to {tokens}.")

    @grok_set.command(name="timeout")
    @checks.is_owner()
    async def set_timeout(self, ctx: commands.Context, seconds: int):
        """Set API timeout (10-120 seconds)."""
        if not 10 <= seconds <= 120:
            return await ctx.send("❌ Must be 10-120 seconds.")
        await self.config.timeout.set(seconds)
        await ctx.send(f"✅ Timeout set to {seconds}s.")

    @grok_set.command(name="settings")
    @checks.admin_or_permissions(manage_guild=True)
    async def show_settings(self, ctx: commands.Context):
        """Display current configuration."""
        global_cfg = await self.config.all()
        guild_cfg = await self.config.guild(ctx.guild).all()

        embed = discord.Embed(
            title="⚙️ Grok Configuration",
            color=discord.Color.gold(),
        )

        embed.add_field(
            name="🔧 Global",
            value=(
                f"Model: `{global_cfg['model']}`\n"
                f"Max Tokens: `{global_cfg['max_tokens']}`\n"
                f"Timeout: `{global_cfg['timeout']}s`\n"
                f"Retries: `{global_cfg['max_retries']}`\n"
                f"API Key: `{'✅ Set' if global_cfg['api_key'] else '❌ Not Set'}`"
            ),
            inline=False,
        )

        embed.add_field(
            name="⚙️ This Server",
            value=(
                f"Enabled: `{guild_cfg['enabled']}`\n"
                f"Cooldown: `{guild_cfg['cooldown_seconds']}s`\n"
                f"Max Input: `{guild_cfg['max_input_length']} chars`"
            ),
            inline=False,
        )

        await ctx.send(embed=embed)

    async def cog_command_error(self, ctx: commands.Context, error: Exception):
        """Cog-wide error handler."""
        if hasattr(ctx, "_error_handled"):
            return

        if isinstance(error, commands.CommandOnCooldown):
            await ctx.send(f"⏱️ On cooldown. Try again in {error.retry_after:.1f}s")

        elif isinstance(error, commands.MissingPermissions):
            await ctx.send("❌ You don't have permission.")

        elif isinstance(error, commands.BotMissingPermissions):
            await ctx.send(f"❌ I need: {', '.join(error.missing_permissions)}")

        elif isinstance(error, commands.BadArgument):
            await ctx.send(f"❌ Invalid argument: {error}")

        elif isinstance(error, commands.CommandInvokeError):
            original = error.original
            if isinstance(original, commands.UserFeedbackCheckFailure):
                await ctx.send(str(original))
            else:
                log.error(f"Command error: {original}", exc_info=original)
                await ctx.send("❌ An error occurred. Check logs.")

        else:
            log.error(f"Unhandled error: {error}", exc_info=error)

        ctx._error_handled = True


async def setup(bot):
    """Red-DiscordBot setup function."""
    try:
        await bot.add_cog(GrokCog(bot))
        log.info("GrokCog setup completed")
    except Exception as e:
        log.error(f"Failed to load GrokCog: {e}", exc_info=True)
        raise
