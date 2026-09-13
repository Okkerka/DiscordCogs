"""Context-aware Groq assistant for Red Discord Bot."""

import asyncio
import hashlib
import json
import logging
import math
import re
import time
from collections import deque
from collections.abc import Callable, Coroutine
from datetime import datetime, timezone
from typing import Literal
from weakref import WeakSet

import aiohttp
import discord
from redbot.core import Config, commands
from redbot.core.bot import Red

from .helpers import (
    DEFAULT_MODEL,
    MAX_INPUT_LENGTH,
    SEARCH_MODEL,
    SEARCH_PATTERN,
    SYSTEM_PROMPT,
    Answer,
    AnswerPages,
    ProviderError,
    answer_pages,
    extract_json,
    response_answer,
)

log = logging.getLogger("red.grokcog")
GROQ_API_BASE = "https://api.groq.com/openai/v1"
MAX_PENDING = 128


class GrokCog(commands.Cog):
    """Answer questions, mentions and replies with optional web evidence."""

    def __init__(self, bot: Red):
        self.bot = bot
        self.config = Config.get_conf(
            self, identifier=0x4B324B32, force_registration=True
        )
        self.config.register_global(
            api_key=None,
            timeout=120,
            max_retries=3,
            cooldown_seconds=5,
            min_api_call_gap=0.5,
            max_requests_per_minute=60,
            model_name=DEFAULT_MODEL,
            request_queue_enabled=True,
        )
        self.config.register_guild(
            enabled=True, max_input_length=MAX_INPUT_LENGTH, default_temperature=0.3
        )
        self.config.register_user(
            request_count=0, last_request_time=None, rate_limit_hits=0
        )
        self._session: aiohttp.ClientSession | None = None
        self._ready = asyncio.Event()
        self._slots = asyncio.Semaphore(3)
        self._rate_lock = asyncio.Lock()
        self._request_times: deque[float] = deque()
        self._last_api_call = 0.0
        self._active: dict[int, asyncio.Task] = {}
        self._operations: set[asyncio.Task] = set()
        self._cooldowns: dict[int, float] = {}
        self._inflight_requests: dict[str, asyncio.Task[Answer]] = {}
        self._waiters: dict[str, int] = {}
        self._cache: dict[str, tuple[float, Answer]] = {}
        self._cache_epoch = 0
        self._models_cache: tuple[float, list[str]] = (0, [])
        self._model_lock = asyncio.Lock()
        self._views: WeakSet[AnswerPages] = WeakSet()

    async def cog_load(self) -> None:
        self._session = aiohttp.ClientSession()
        self._ready.set()

    async def cog_unload(self) -> None:
        self._ready.clear()
        tasks = (
            set(self._active.values())
            | set(self._inflight_requests.values())
            | self._operations
        )
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for view in self._views:
            view.stop()
        self._views.clear()
        self._cache.clear()
        if self._session and not self._session.closed:
            await self._session.close()

    async def red_delete_data_for_user(self, *, requester: str, user_id: int) -> None:
        """Delete statistics and discard transient question/answer data."""
        task = self._active.get(user_id)
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await self.config.user_from_id(user_id).clear()
        self._cooldowns.pop(user_id, None)
        self._clear_cache()
        for view in list(self._views):
            if view.owner == user_id:
                view.stop()
                self._views.discard(view)

    def _clear_cache(self) -> None:
        self._cache_epoch += 1
        self._cache.clear()

    @staticmethod
    def _key(text: str) -> str:
        return hashlib.sha256(text.strip().encode()).hexdigest()

    @staticmethod
    def _needs_search(question: str) -> bool:
        return bool(SEARCH_PATTERN.search(question))

    _extract_json = staticmethod(extract_json)

    def _format(self, data: Answer) -> discord.Embed:
        return answer_pages(data)[0]

    async def _respect_api_rate_limits(self) -> None:
        # Reserve a slot for every attempt, including retries and model discovery.
        async with self._rate_lock:
            per_minute = max(1, int(await self.config.max_requests_per_minute()))
            gap = float(await self.config.min_api_call_gap())
            gap = max(0.0, gap) if math.isfinite(gap) else 0.5
            while True:
                now = time.monotonic()
                while self._request_times and self._request_times[0] <= now - 60:
                    self._request_times.popleft()
                delay = max(0, self._last_api_call + gap - now)
                if len(self._request_times) >= per_minute:
                    delay = max(delay, self._request_times[0] + 60 - now)
                if delay <= 0:
                    self._last_api_call = now
                    self._request_times.append(now)
                    return
                await asyncio.sleep(delay)

    async def _request_json(
        self, method: str, path: str, payload: dict | None = None
    ) -> dict:
        api_key = await self.config.api_key()
        if not isinstance(api_key, str) or not api_key.strip():
            raise ProviderError(
                "Groq API key is not configured. Ask the owner to run grok admin apikey privately."
            )
        if not self._session or self._session.closed:
            raise ProviderError(
                "The assistant is restarting. Please try again shortly."
            )
        attempts = min(5, max(1, int(await self.config.max_retries())))
        timeout = min(120, max(5, float(await self.config.timeout())))
        for attempt in range(attempts):
            retry_delay = min(2**attempt, 10)
            await self._respect_api_rate_limits()
            try:
                async with self._session.request(
                    method,
                    f"{GROQ_API_BASE}{path}",
                    json=payload,
                    headers={
                        "Authorization": f"Bearer {api_key.strip()}",
                        "Groq-Model-Version": "latest",
                    },
                    timeout=aiohttp.ClientTimeout(total=timeout, connect=10),
                ) as response:
                    if response.status == 429 or response.status >= 500:
                        try:
                            delay = float(
                                response.headers.get("Retry-After", retry_delay)
                            )
                            if math.isfinite(delay):
                                retry_delay = min(30, max(0, delay))
                        except (TypeError, ValueError):
                            pass
                        failure = (
                            "Groq is busy or rate-limited. Please try again shortly."
                        )
                    elif response.status != 200:
                        messages = {
                            401: "Groq rejected the API key. The bot owner needs to update it.",
                            403: "This Groq key does not have permission for the requested model.",
                            400: "Groq rejected this request. Check the configured model and its supported options.",
                            404: "The configured Groq model is unavailable. Use grok models and ask the owner to update it.",
                        }
                        raise ProviderError(
                            messages.get(
                                response.status,
                                f"Groq request failed (HTTP {response.status}).",
                            )
                        )
                    else:
                        # A read(n) can return a partial chunk; consume until EOF with a hard cap.
                        raw = bytearray()
                        async for chunk in response.content.iter_chunked(65536):
                            raw.extend(chunk)
                            if len(raw) > 2_000_000:
                                raise ProviderError(
                                    "Groq returned an oversized response."
                                )
                        try:
                            data = json.loads(raw)
                        except (ValueError, UnicodeDecodeError) as exc:
                            raise ProviderError(
                                "Groq returned an unreadable response."
                            ) from exc
                        if not isinstance(data, dict):
                            raise ProviderError("Groq returned an unexpected response.")
                        return data
            except (aiohttp.ClientError, asyncio.TimeoutError):
                failure = "Could not reach Groq in time. Please try again shortly."
            if attempt + 1 == attempts:
                raise ProviderError(failure)
            await asyncio.sleep(retry_delay)
        raise ProviderError("Groq request failed.")

    async def _ask_groq(
        self,
        question: str,
        temperature: float,
        *,
        search: bool = False,
        model: str | None = None,
    ) -> Answer:
        selected = SEARCH_MODEL if search else (model or await self.config.model_name())
        prompt = (
            SYSTEM_PROMPT
            + "\nCurrent UTC date: "
            + datetime.now(timezone.utc).date().isoformat()
        )
        if search:
            prompt += "\nSearch the web before answering this request. Cite retrieved evidence."
        payload = {
            "model": selected,
            "messages": [
                {"role": "system", "content": prompt},
                {"role": "user", "content": question},
            ],
            "temperature": temperature,
            "max_completion_tokens": 4096,
            "stream": False,
        }
        if selected in {"groq/compound", "groq/compound-mini"}:
            payload["compound_custom"] = {
                "tools": {"enabled_tools": ["web_search", "visit_website"]}
            }
        data = await self._request_json("POST", "/chat/completions", payload)
        return response_answer(data, selected, search)

    async def _build_context_query(
        self, message: discord.Message, base_question: str
    ) -> str:
        reference = message.reference
        if not reference:
            if re.fullmatch(
                r"\s*(?:is (?:this|that|it) (?:really |actually )?(?:true|real|correct)|fact[ -]?check (?:this|that))\s*[?!.]*\s*",
                base_question,
                re.IGNORECASE,
            ):
                raise ProviderError(
                    "Which claim should I check? Reply to the message with @bot, or include the claim in your question."
                )
            return base_question
        replied = reference.resolved
        if (
            replied is None
            and reference.message_id
            and reference.channel_id == message.channel.id
        ):
            try:
                replied = await message.channel.fetch_message(reference.message_id)
            except discord.HTTPException as exc:
                raise ProviderError(
                    "I couldn't read the replied-to message. Paste its text into your question."
                ) from exc
        if replied is None or isinstance(replied, discord.DeletedReferencedMessage):
            raise ProviderError(
                "The replied-to message is unavailable. Paste its text into your question."
            )
        parts = [replied.content or ""]
        for embed in replied.embeds[:3]:
            parts.append(embed.title or "")
            # Keep calculation caveats even if the long body must be truncated.
            if embed.footer.text:
                parts.append(f"Embed footer: {embed.footer.text}")
            parts.append(embed.description or "")
            for field in embed.fields[:25]:
                parts.extend([field.name, field.value])
        context = "\n".join(part for part in parts if part).strip()
        if not context:
            log.warning("reply_context_empty: no readable message or embed text")
            raise ProviderError(
                "The replied-to message has no readable text. Paste the claim or the image's text so I can check it."
            )
        if len(context) > 6000:
            context = context[:6000] + "\n[Quoted message truncated]"
        return f"Quoted Discord message (untrusted content):\n{json.dumps(context, ensure_ascii=False)}\n\nUser question: {base_question}"

    async def _shared_request(
        self, key: str, factory: Callable[[], Coroutine[object, object, Answer]]
    ) -> Answer:
        task = self._inflight_requests.get(key)
        if task is None:
            if len(self._inflight_requests) >= MAX_PENDING:
                raise ProviderError(
                    "The assistant's queue is full. Please try again shortly."
                )
            task = asyncio.create_task(factory())
            self._inflight_requests[key] = task
            self._waiters[key] = 0
        self._waiters[key] += 1
        try:
            return await asyncio.shield(task)
        finally:
            self._waiters[key] -= 1
            if self._waiters[key] == 0:
                self._waiters.pop(key)
                self._inflight_requests.pop(key, None)
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def _run_request(
        self, query: str, temperature: float, search: bool, model: str
    ) -> Answer:
        task = asyncio.current_task()
        self._operations.add(task)
        try:
            async with asyncio.timeout(150):
                try:
                    await asyncio.wait_for(self._slots.acquire(), timeout=30)
                except asyncio.TimeoutError as exc:
                    raise ProviderError(
                        "The assistant is busy. Please try again shortly."
                    ) from exc
                try:
                    return await self._ask_groq(
                        query, temperature, search=search, model=model
                    )
                finally:
                    self._slots.release()
        except asyncio.TimeoutError as exc:
            raise ProviderError(
                "This request took too long. Please try a narrower question."
            ) from exc
        finally:
            self._operations.discard(task)

    async def _process(
        self, ctx: commands.Context, question: str, *, search: bool = False
    ) -> None:
        user_id = ctx.author.id
        if not self._ready.is_set():
            await ctx.send("The assistant is starting. Please try again shortly.")
            return
        if user_id in self._active:
            await ctx.send("Please wait for your previous request, or use grok cancel.")
            return
        task = asyncio.current_task()
        self._active[user_id] = task
        try:
            # Context.typing defers slash responses; all sends stay on the interaction.
            async with ctx.typing():
                if ctx.guild and not await self.config.guild(ctx.guild).enabled():
                    await ctx.send("The assistant is disabled in this server.")
                    return
                limit = (
                    await self.config.guild(ctx.guild).max_input_length()
                    if ctx.guild
                    else MAX_INPUT_LENGTH
                )
                if not question.strip() or len(question) > min(
                    MAX_INPUT_LENGTH, max(1, limit)
                ):
                    await ctx.send(
                        f"Please provide a question of at most {min(MAX_INPUT_LENGTH, max(1, limit))} characters."
                    )
                    return
                cooldown = min(3600, max(0, int(await self.config.cooldown_seconds())))
                now = time.monotonic()
                self._cooldowns = {
                    uid: ts for uid, ts in self._cooldowns.items() if now - ts < 3600
                }
                remaining = self._cooldowns.get(user_id, -3600) + cooldown - now
                if remaining > 0:
                    await ctx.send(
                        f"Please wait {remaining:.1f}s before another question."
                    )
                    return
                self._cooldowns[user_id] = now
                query = await self._build_context_query(ctx.message, question)
                search = search or self._needs_search(question)
                model = SEARCH_MODEL if search else await self.config.model_name()
                temperature = (
                    await self.config.guild(ctx.guild).default_temperature()
                    if ctx.guild
                    else 0.3
                )
                epoch = self._cache_epoch
                key = self._key(
                    json.dumps(
                        [
                            ctx.guild.id if ctx.guild else None,
                            ctx.channel.id,
                            model,
                            temperature,
                            search,
                            query,
                            epoch,
                            datetime.now(timezone.utc).date().isoformat(),
                        ]
                    )
                )
                cached = self._cache.get(key)
                ttl = 300 if search else 3600
                if cached and now - cached[0] < ttl:
                    result = cached[1]
                else:
                    result = await self._shared_request(
                        key,
                        lambda: self._run_request(query, temperature, search, model),
                    )
                    if epoch == self._cache_epoch and (
                        not search or result["searched"]
                    ):
                        self._cache[key] = (time.monotonic(), result)
                        while len(self._cache) > 256:
                            self._cache.pop(next(iter(self._cache)))
                pages = answer_pages(result)
                view = AnswerPages(user_id, pages) if len(pages) > 1 else None
                if view:
                    self._views.add(view)
                kwargs = {
                    "embed": pages[0],
                    "view": view,
                    "allowed_mentions": discord.AllowedMentions.none(),
                }
                if ctx.interaction is None:
                    sent = await ctx.reply(**kwargs, mention_author=False)
                else:
                    sent = await ctx.send(**kwargs)
                if view:
                    view.message = sent
                async with self.config.user(ctx.author).all() as stats:
                    stats["request_count"] = stats.get("request_count", 0) + 1
                    stats["last_request_time"] = time.time()
        except asyncio.CancelledError:
            if self._ready.is_set() and ctx.interaction is not None:
                try:
                    await ctx.send("Request cancelled.")
                except discord.HTTPException:
                    log.warning("Could not complete a cancelled interaction")
            raise
        except ProviderError as exc:
            await ctx.send(str(exc), allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            log.warning("Could not deliver an assistant response to Discord")
        except Exception as exc:  # noqa: BLE001 -- final UI boundary must not leak raw errors/secrets
            # Never log prompts, credentials, raw provider bodies or private messages.
            log.error("Assistant request failed (%s)", type(exc).__name__)
            await ctx.send(
                "The assistant encountered an unexpected error. Please try again."
            )
        finally:
            if self._active.get(user_id) is task:
                self._active.pop(user_id, None)

    @commands.Cog.listener()
    async def on_message(self, msg: discord.Message) -> None:
        if msg.author.bot or not self._ready.is_set() or not self.bot.user:
            return
        if await self.bot.cog_disabled_in_guild(self, msg.guild):
            return
        if not await self.bot.allowed_by_whitelist_blacklist(msg.author):
            return
        if not await self.bot.ignored_channel_or_guild(msg):
            return
        ctx = await self.bot.get_context(msg)
        if ctx.valid:
            return
        is_mention = self.bot.user in msg.mentions
        reference = msg.reference
        replied = reference.resolved if reference else None
        is_reply_to_me = (
            isinstance(replied, discord.Message)
            and replied.author.id == self.bot.user.id
        )
        if not msg.guild:
            if not isinstance(msg.channel, discord.DMChannel):
                return
            prefixes = await self.bot.get_valid_prefixes()
            if any(msg.content.startswith(prefix) for prefix in prefixes):
                return
        elif not (is_mention or is_reply_to_me):
            return
        content = re.sub(rf"<@!?{self.bot.user.id}>", "", msg.content).strip()
        if content:
            await self._process(ctx, content)

    @commands.hybrid_group(name="grok", fallback="ask", invoke_without_command=True)
    async def grok(self, ctx: commands.Context, *, question: str) -> None:
        """Ask a question; fact checks automatically search for evidence."""
        await self._process(ctx, question)

    @grok.command(name="search")
    async def grok_search(self, ctx: commands.Context, *, question: str) -> None:
        """Search the web for an answer with retrieved sources."""
        await self._process(ctx, question, search=True)

    @grok.command(name="cancel")
    async def grok_cancel(self, ctx: commands.Context) -> None:
        """Cancel your current question or queued request."""
        task = self._active.get(ctx.author.id)
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await ctx.send(
            "Request cancelled." if task else "You have no active request.",
            ephemeral=True,
        )

    @grok.command(name="stats")
    async def grok_stats(self, ctx: commands.Context) -> None:
        """Show successful answers delivered to you, including cache hits."""
        stats = await self.config.user(ctx.author).all()
        embed = discord.Embed(
            title=f"{ctx.author.display_name}'s Stats", color=discord.Color.gold()
        )
        embed.add_field(name="Total Queries", value=stats.get("request_count", 0))
        await ctx.send(embed=embed)

    async def _models(self) -> list[str]:
        now = time.monotonic()
        if self._models_cache[1] and now - self._models_cache[0] < 300:
            return self._models_cache[1]
        epoch = self._cache_epoch
        task = asyncio.current_task()
        self._operations.add(task)
        try:
            async with asyncio.timeout(30):
                async with self._model_lock:
                    if self._models_cache[1] and now - self._models_cache[0] < 300:
                        return self._models_cache[1]
                    async with self._slots:
                        data = await self._request_json("GET", "/models")
                    items = data.get("data")
                    if not isinstance(items, list):
                        raise ProviderError("Groq returned an invalid model list.")
                    models = sorted(
                        {
                            item["id"]
                            for item in items
                            if isinstance(item, dict)
                            and isinstance(item.get("id"), str)
                        }
                    )
                    if epoch == self._cache_epoch:
                        self._models_cache = (now, models)
                    return models
        except asyncio.TimeoutError as exc:
            raise ProviderError(
                "Model discovery timed out. Please try again shortly."
            ) from exc
        finally:
            self._operations.discard(task)

    @grok.command(name="models")
    @commands.cooldown(1, 10, commands.BucketType.user)
    async def grok_models(self, ctx: commands.Context) -> None:
        """List models available through the configured Groq key."""
        async with ctx.typing():
            try:
                models = await self._models()
            except ProviderError as exc:
                await ctx.send(str(exc))
                return
            text = "\n".join(models) or "No models available."
            for offset in range(0, len(text), 1800):
                await ctx.send(
                    text[offset : offset + 1800],
                    allowed_mentions=discord.AllowedMentions.none(),
                )

    @grok.group(name="admin")
    async def grok_admin(self, ctx: commands.Context) -> None:
        """Configure the Groq assistant."""

    @grok_admin.command(name="apikey")
    @commands.is_owner()
    async def admin_apikey(
        self, ctx: commands.Context, *, api_key: str | None = None
    ) -> None:
        """Set the key via slash (private response) or a prefix command in DM."""
        if ctx.guild and ctx.interaction is None:
            if api_key:
                try:
                    await ctx.message.delete()
                except discord.HTTPException:
                    pass
            await ctx.send(
                "Use this command in DM or via slash. If you posted a key here, rotate it in Groq's console."
            )
            return
        if (
            not api_key
            or not api_key.strip()
            or len(api_key) > 512
            or any(ch.isspace() for ch in api_key.strip())
        ):
            await ctx.send(
                "Provide a valid Groq API key via this slash command or in DM.",
                ephemeral=True,
            )
            return
        await self.config.api_key.set(api_key.strip())
        self._models_cache = (0, [])
        self._clear_cache()
        await ctx.send("Groq API key saved.", ephemeral=True)

    @grok_admin.command(name="verify")
    @commands.is_owner()
    async def admin_verify(
        self, ctx: commands.Context, mode: Literal["chat", "search"] = "chat"
    ) -> None:
        """Test normal chat, or use mode search to check live source retrieval."""
        async with ctx.typing(ephemeral=True):
            try:
                search = mode == "search"
                result = await self._run_request(
                    "Search the web for Groq's official API documentation and cite its URL."
                    if search
                    else "Reply with OK.",
                    0.1,
                    search,
                    SEARCH_MODEL if search else await self.config.model_name(),
                )
                if search:
                    if result["sources"]:
                        status = f"Search is working. Retrieved {len(result['sources'])} source(s) through {SEARCH_MODEL}."
                    else:
                        status = "Groq responded, but search returned no usable sources. Check the red.grokcog search_no_sources warning in the logs."
                else:
                    status = f"Chat is working. Model: {result['model']}. This does not test search; use grok admin verify search."
                await ctx.send(status, ephemeral=True)
            except ProviderError as exc:
                await ctx.send(str(exc), ephemeral=True)

    @grok_admin.command(name="toggle")
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    async def admin_toggle(self, ctx: commands.Context) -> None:
        """Enable or disable assistant answers in this server."""
        async with self.config.guild(ctx.guild).all() as settings:
            settings["enabled"] = not settings["enabled"]
            enabled = settings["enabled"]
        await ctx.send(f"Grok is now {'enabled' if enabled else 'disabled'}.")

    @grok_admin.command(name="cooldown")
    @commands.is_owner()
    async def admin_cooldown(self, ctx: commands.Context, seconds: int) -> None:
        """Set seconds between questions per user across all entry points (0-3600)."""
        if not 0 <= seconds <= 3600:
            await ctx.send(
                "Cooldown must be between 0 and 3600 seconds.", ephemeral=True
            )
            return
        await self.config.cooldown_seconds.set(seconds)
        await ctx.send(f"Cooldown set to {seconds}s.", ephemeral=True)

    @grok_admin.command(name="setmodel")
    @commands.is_owner()
    async def admin_setmodel(self, ctx: commands.Context, model: str) -> None:
        """Choose an available chat model; search uses Groq Compound."""
        async with ctx.typing(ephemeral=True):
            try:
                if model not in await self._models():
                    await ctx.send(
                        "Unknown model. Use grok models to list available IDs.",
                        ephemeral=True,
                    )
                    return
                # Also check chat compatibility: the catalog includes audio and guard models.
                await self._run_request("Reply with OK.", 0.1, False, model)
            except ProviderError as exc:
                await ctx.send(str(exc), ephemeral=True)
                return
            await self.config.model_name.set(model)
            self._clear_cache()
            await ctx.send(
                f"Model set to {model}.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )

    @admin_setmodel.autocomplete("model")
    async def model_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[discord.app_commands.Choice[str]]:
        if not await self.bot.is_owner(interaction.user):
            return []
        return [
            discord.app_commands.Choice(name=model, value=model)
            for model in self._models_cache[1]
            if current.lower() in model.lower() and len(model) <= 100
        ][:25]

    @grok_admin.command(name="ratelimits")
    @commands.is_owner()
    async def admin_ratelimits(
        self, ctx: commands.Context, per_minute: int, min_gap: float
    ) -> None:
        """Set the global request budget and spacing between API attempts."""
        if (
            not 1 <= per_minute <= 600
            or not math.isfinite(min_gap)
            or not 0 <= min_gap <= 60
        ):
            await ctx.send(
                "Use 1-600 requests/minute and a finite gap between 0 and 60 seconds.",
                ephemeral=True,
            )
            return
        async with self.config.all() as settings:
            settings["max_requests_per_minute"] = per_minute
            settings["min_api_call_gap"] = min_gap
        await ctx.send(
            f"Rate limits updated: {per_minute}/min, {min_gap}s gap.", ephemeral=True
        )

    @grok_admin.command(name="clearcache")
    @commands.is_owner()
    async def admin_clearcache(self, ctx: commands.Context) -> None:
        """Clear cached answers, including answers still being generated."""
        self._clear_cache()
        await ctx.send("Cache cleared.", ephemeral=True)
