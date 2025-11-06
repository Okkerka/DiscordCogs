import asyncio
import logging
from typing import Optional, Dict
from datetime import datetime, timedelta

import discord
from redbot.core import commands, Config, checks
from redbot.core.utils.chat_formatting import box, pagify
from openai import AsyncOpenAI, APIError, APITimeoutError, RateLimitError

log = logging.getLogger("red.grokcog")


class GrokCog(commands.Cog):
    """
    Production-ready Grok AI integration for Red-DiscordBot.
    Includes rate limiting, retry logic, error handling, and resource optimization.
    """

    __version__ = "1.0.0"
    __author__ = "YourName"

    def __init__(self, bot):
        self.bot = bot
        self.config = Config.get_conf(
            self, identifier=987654321098765, force_registration=True
        )

        # Default settings
        default_global = {
            "api_key": None,
            "model": "grok-beta",
            "max_tokens": 500,
            "timeout": 30,
            "max_retries": 3,
            "system_prompt": "You are Grok, a helpful and witty AI assistant.",
        }

        default_guild = {
            "enabled": True,
            "cooldown_rate": 1,
            "cooldown_per": 30,
            "max_context_length": 2000,
        }

        default_user = {
            "request_count": 0,
            "last_request": None,
            "rate_limited_until": None,
        }

        self.config.register_global(**default_global)
        self.config.register_guild(**default_guild)
        self.config.register_user(**default_user)

        self.client: Optional[AsyncOpenAI] = None
        self._client_lock = asyncio.Lock()
        self._active_requests: Dict[int, bool] = {}

    async def cog_load(self):
        """Initialize the OpenAI client on cog load."""
        await self._initialize_client()

    async def cog_unload(self):
        """Cleanup when cog is unloaded."""
        if self.client:
            await self.client.close()
            log.info("Grok client connection closed")

    async def _initialize_client(self) -> bool:
        """Initialize or reinitialize the OpenAI client with retry logic."""
        async with self._client_lock:
            api_key = await self.config.api_key()
            if not api_key:
                log.warning("Grok API key not configured")
                return False

            try:
                timeout_value = await self.config.timeout()
                max_retries = await self.config.max_retries()

                self.client = AsyncOpenAI(
                    api_key=api_key,
                    base_url="https://api.x.ai/v1",
                    timeout=timeout_value,
                    max_retries=max_retries,
                )
                log.info("Grok client initialized successfully")
                return True

            except Exception as e:
                log.error(f"Failed to initialize Grok client: {e}", exc_info=True)
                return False

    async def _check_rate_limit(self, user_id: int) -> Optional[str]:
        """Check if user is rate limited. Returns error message if limited, None otherwise."""
        user_data = await self.config.user_from_id(user_id).all()

        # Check if user has a rate limit penalty
        if user_data["rate_limited_until"]:
            limited_until = datetime.fromisoformat(user_data["rate_limited_until"])
            if datetime.now() < limited_until:
                remaining = (limited_until - datetime.now()).seconds
                return f"You are rate limited. Try again in {remaining} seconds."

        return None

    async def _apply_rate_limit_penalty(self, user_id: int, duration: int = 300):
        """Apply rate limit penalty to a user."""
        penalty_until = datetime.now() + timedelta(seconds=duration)
        await self.config.user_from_id(user_id).rate_limited_until.set(
            penalty_until.isoformat()
        )
        log.warning(f"Rate limit penalty applied to user {user_id} for {duration}s")

    async def _make_grok_request(
        self, prompt: str, user_id: int, max_tokens: Optional[int] = None
    ) -> str:
        """
        Make a request to Grok API with exponential backoff retry logic.
        Raises appropriate exceptions on failure.
        """
        if not self.client:
            if not await self._initialize_client():
                raise RuntimeError("Grok client not initialized. Set API key first.")

        model = await self.config.model()
        system_prompt = await self.config.system_prompt()
        if max_tokens is None:
            max_tokens = await self.config.max_tokens()

        # Track active request
        self._active_requests[user_id] = True

        try:
            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt},
            ]

            response = await self.client.chat.completions.create(
                model=model,
                messages=messages,
                max_tokens=max_tokens,
                temperature=0.7,
            )

            # Update user stats
            async with self.config.user_from_id(user_id).all() as user_data:
                user_data["request_count"] += 1
                user_data["last_request"] = datetime.now().isoformat()

            return response.choices[0].message.content.strip()

        except RateLimitError as e:
            log.error(f"Rate limit hit for user {user_id}: {e}")
            await self._apply_rate_limit_penalty(user_id, 600)
            raise commands.UserFeedbackCheckFailure(
                "API rate limit reached. You've been temporarily restricted. "
                "Please try again in 10 minutes."
            )

        except APITimeoutError as e:
            log.error(f"Timeout error for user {user_id}: {e}")
            raise commands.UserFeedbackCheckFailure(
                "Request timed out. The API might be experiencing high load. "
                "Please try again in a moment."
            )

        except APIError as e:
            log.error(f"API error for user {user_id}: {e}", exc_info=True)
            if e.status_code and 500 <= e.status_code < 600:
                raise commands.UserFeedbackCheckFailure(
                    "Grok API is experiencing server issues. Please try again later."
                )
            raise

        except Exception as e:
            log.error(f"Unexpected error for user {user_id}: {e}", exc_info=True)
            raise

        finally:
            # Clean up active request tracking
            self._active_requests.pop(user_id, None)

    @commands.group(name="grok", invoke_without_command=True)
    @commands.guild_only()
    @commands.cooldown(1, 30, commands.BucketType.user)
    async def grok(self, ctx: commands.Context, *, question: str):
        """
        Ask Grok AI a question.
        
        Usage: [p]grok <your question>
        Example: [p]grok What is the meaning of life?
        """
        # Check if cog is enabled in guild
        if not await self.config.guild(ctx.guild).enabled():
            return await ctx.send("Grok is currently disabled in this server.")

        # Validate input length
        max_length = await self.config.guild(ctx.guild).max_context_length()
        if len(question) > max_length:
            return await ctx.send(
                f"Question too long. Maximum length: {max_length} characters."
            )

        # Check manual rate limits
        rate_limit_msg = await self._check_rate_limit(ctx.author.id)
        if rate_limit_msg:
            return await ctx.send(rate_limit_msg)

        # Check for concurrent requests
        if ctx.author.id in self._active_requests:
            return await ctx.send(
                "You already have a request in progress. Please wait for it to complete."
            )

        async with ctx.typing():
            try:
                response = await self._make_grok_request(question, ctx.author.id)

                # Handle long responses with pagination
                if len(response) > 2000:
                    pages = [page for page in pagify(response, delims=["\n\n", "\n", " "])]
                    for page in pages:
                        await ctx.send(page)
                else:
                    await ctx.send(response)

                log.info(f"Successful Grok request by {ctx.author} in {ctx.guild}")

            except commands.UserFeedbackCheckFailure as e:
                await ctx.send(str(e))

            except Exception as e:
                log.error(f"Error in grok command: {e}", exc_info=True)
                await ctx.send(
                    "An unexpected error occurred. Please try again later or contact the bot owner."
                )

    @grok.command(name="stats")
    async def grok_stats(self, ctx: commands.Context):
        """View your Grok usage statistics."""
        user_data = await self.config.user(ctx.author).all()

        embed = discord.Embed(
            title="Your Grok Statistics",
            color=discord.Color.blue(),
            timestamp=datetime.now(),
        )

        embed.add_field(name="Total Requests", value=user_data["request_count"], inline=True)

        if user_data["last_request"]:
            last_req = datetime.fromisoformat(user_data["last_request"])
            embed.add_field(
                name="Last Request",
                value=f"<t:{int(last_req.timestamp())}:R>",
                inline=True,
            )

        if user_data["rate_limited_until"]:
            limited_until = datetime.fromisoformat(user_data["rate_limited_until"])
            if datetime.now() < limited_until:
                embed.add_field(
                    name="Rate Limited Until",
                    value=f"<t:{int(limited_until.timestamp())}:R>",
                    inline=False,
                )

        await ctx.send(embed=embed)

    @grok.command(name="version")
    async def grok_version(self, ctx: commands.Context):
        """Display Grok cog version information."""
        embed = discord.Embed(
            title="Grok Cog Information",
            description=f"Version: {self.__version__}\nAuthor: {self.__author__}",
            color=discord.Color.green(),
        )
        await ctx.send(embed=embed)

    # Admin commands
    @grok.group(name="set", invoke_without_command=True)
    @checks.admin_or_permissions(manage_guild=True)
    async def grok_set(self, ctx: commands.Context):
        """Configure Grok settings."""
        await ctx.send_help()

    @grok_set.command(name="apikey")
    @checks.is_owner()
    async def set_apikey(self, ctx: commands.Context, api_key: str):
        """
        Set the Grok API key (Owner only).
        
        Get your API key from: https://console.x.ai/
        Note: This message will be deleted for security.
        """
        try:
            await ctx.message.delete()
        except discord.HTTPException:
            pass

        await self.config.api_key.set(api_key)
        success = await self._initialize_client()

        if success:
            await ctx.send("✅ API key set and client initialized successfully.", delete_after=10)
            log.info(f"API key set by {ctx.author}")
        else:
            await ctx.send("⚠️ API key set but client initialization failed. Check logs.", delete_after=10)

    @grok_set.command(name="toggle")
    @checks.admin_or_permissions(manage_guild=True)
    async def set_toggle(self, ctx: commands.Context):
        """Toggle Grok on/off for this server."""
        current = await self.config.guild(ctx.guild).enabled()
        await self.config.guild(ctx.guild).enabled.set(not current)

        status = "enabled" if not current else "disabled"
        await ctx.send(f"✅ Grok has been {status} in this server.")

    @grok_set.command(name="cooldown")
    @checks.admin_or_permissions(manage_guild=True)
    async def set_cooldown(self, ctx: commands.Context, seconds: int):
        """
        Set the cooldown between requests (in seconds).
        
        Recommended: 30-60 seconds
        """
        if seconds < 10:
            return await ctx.send("Cooldown must be at least 10 seconds.")

        await self.config.guild(ctx.guild).cooldown_per.set(seconds)
        await ctx.send(f"✅ Cooldown set to {seconds} seconds.")

    @grok_set.command(name="maxtokens")
    @checks.is_owner()
    async def set_maxtokens(self, ctx: commands.Context, tokens: int):
        """
        Set maximum response tokens (Owner only).
        
        Lower values = shorter responses = less resource usage.
        Recommended: 300-1000
        """
        if not 50 <= tokens <= 4000:
            return await ctx.send("Tokens must be between 50 and 4000.")

        await self.config.max_tokens.set(tokens)
        await ctx.send(f"✅ Max tokens set to {tokens}.")

    @grok_set.command(name="timeout")
    @checks.is_owner()
    async def set_timeout(self, ctx: commands.Context, seconds: int):
        """
        Set API request timeout in seconds (Owner only).
        
        Recommended: 30-60 seconds
        """
        if not 10 <= seconds <= 120:
            return await ctx.send("Timeout must be between 10 and 120 seconds.")

        await self.config.timeout.set(seconds)
        await self._initialize_client()
        await ctx.send(f"✅ Timeout set to {seconds} seconds. Client reinitialized.")

    @grok_set.command(name="systemprompt")
    @checks.is_owner()
    async def set_systemprompt(self, ctx: commands.Context, *, prompt: str):
        """
        Set the system prompt for Grok (Owner only).
        
        This defines Grok's personality and behavior.
        """
        if len(prompt) > 500:
            return await ctx.send("System prompt too long (max 500 characters).")

        await self.config.system_prompt.set(prompt)
        await ctx.send(f"✅ System prompt updated to:\n{box(prompt)}")

    @grok_set.command(name="showsettings")
    @checks.admin_or_permissions(manage_guild=True)
    async def show_settings(self, ctx: commands.Context):
        """Display current Grok configuration."""
        global_config = await self.config.all()
        guild_config = await self.config.guild(ctx.guild).all()

        embed = discord.Embed(
            title="Grok Configuration",
            color=discord.Color.gold(),
            timestamp=datetime.now(),
        )

        # Global settings
        embed.add_field(
            name="🔧 Global Settings",
            value=(
                f"Model: `{global_config['model']}`\n"
                f"Max Tokens: `{global_config['max_tokens']}`\n"
                f"Timeout: `{global_config['timeout']}s`\n"
                f"Max Retries: `{global_config['max_retries']}`\n"
                f"API Key: `{'✅ Set' if global_config['api_key'] else '❌ Not Set'}`"
            ),
            inline=False,
        )

        # Guild settings
        embed.add_field(
            name="⚙️ Server Settings",
            value=(
                f"Enabled: `{guild_config['enabled']}`\n"
                f"Cooldown: `{guild_config['cooldown_per']}s`\n"
                f"Max Context: `{guild_config['max_context_length']} chars`"
            ),
            inline=False,
        )

        # System prompt preview
        system_prompt = global_config['system_prompt']
        preview = system_prompt[:100] + "..." if len(system_prompt) > 100 else system_prompt
        embed.add_field(
            name="💬 System Prompt",
            value=f"``````",
            inline=False,
        )

        await ctx.send(embed=embed)

    async def cog_command_error(self, ctx: commands.Context, error: Exception):
        """Cog-wide error handler."""
        # Don't handle errors that have already been handled
        if hasattr(ctx, "handled"):
            return

        if isinstance(error, commands.CommandOnCooldown):
            await ctx.send(
                f"⏱️ This command is on cooldown. Try again in {error.retry_after:.1f}s."
            )

        elif isinstance(error, commands.MissingPermissions):
            await ctx.send("❌ You don't have permission to use this command.")

        elif isinstance(error, commands.BotMissingPermissions):
            await ctx.send(f"❌ I need the following permissions: {', '.join(error.missing_permissions)}")

        elif isinstance(error, commands.BadArgument):
            await ctx.send(f"❌ Invalid argument: {error}")
            await ctx.send_help()

        elif isinstance(error, commands.CommandInvokeError):
            original = error.original

            if isinstance(original, commands.UserFeedbackCheckFailure):
                await ctx.send(str(original))

            elif isinstance(original, asyncio.TimeoutError):
                await ctx.send("⏱️ Request timed out. Please try again.")

            else:
                log.error(f"Command error in {ctx.command}: {original}", exc_info=original)
                await ctx.send(
                    "❌ An unexpected error occurred. The issue has been logged."
                )

        else:
            log.error(f"Unhandled error in {ctx.command}: {error}", exc_info=error)

        ctx.handled = True


async def setup(bot):
    """Setup function for Red-DiscordBot."""
    try:
        cog = GrokCog(bot)
        await bot.add_cog(cog)
        log.info("GrokCog loaded successfully")
    except Exception as e:
        log.error(f"Failed to load GrokCog: {e}", exc_info=True)
        raise RuntimeError("Failed to initialize GrokCog. Check your configuration.") from e
