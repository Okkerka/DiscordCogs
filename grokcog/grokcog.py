import asyncio
import hashlib
import json
import logging
import re
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import aiohttp
import discord
from redbot.core import Config, commands

<<<<<<< HEAD
# --- WEB SEARCH SETUP ---
=======
# --- SEARCH MODULE CHECK ---
>>>>>>> parent of 320505f (Update grokcog.py)
try:
    from duckduckgo_search import DDGS
    HAS_DDG = True
except ImportError:
    HAS_DDG = False

# --- CONSTANTS ---
log = logging.getLogger("red.grokcog")

<<<<<<< HEAD
# --- USER CONFIGURATION ---
GROQ_API_BASE = "https://api.groq.com/openai/v1"
# Restored to your requested model
DEFAULT_MODEL = "moonshotai/kimi-k2-instruct-0905"
=======
# Groq API Endpoint
GROQ_API_BASE = "https://api.groq.com/openai/v1"
>>>>>>> parent of 320505f (Update grokcog.py)

# CHANGED: Default to a model that actually exists on Groq
DEFAULT_MODEL = "llama-3.3-70b-versatile"

<<<<<<< HEAD
INSTRUCTIONS:
1. Use the SEARCH RESULTS below (if any) to answer the question.
2. Cite specific sources using [1] notation inline.
3. If no search results help, use your general knowledge.

RESPONSE FORMAT (JSON ONLY):
=======
# Compiled Regex for Safety
JSON_BLOCK_REGEX = re.compile(r"```(?:json)?\s*(.*?)\s*```
JSON_BRACE_REGEX = re.compile(r"\{.*\}", re.DOTALL)

# --- SYSTEM PROMPT ---
K2_PROMPT = """You are DripBot, a chill and helpful AI assistant.

MODES:
1. CASUAL ("hi", "yo"): Just say hello back. No sources.
2. FACTS: Use SEARCH RESULTS if provided. Cite  inline.[1]

OUTPUT JSON ONLY:
>>>>>>> parent of 320505f (Update grokcog.py)
{
  "answer": "Response here.",
  "confidence": 0.9,
<<<<<<< HEAD
  "sources": [{"title": "Source Title", "url": "https://link.com"}]
=======
  "sources": [{"title": "T", "url": "U"}]
>>>>>>> parent of 320505f (Update grokcog.py)
}
"""

class GrokCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=0x4B324B32, force_registration=True)
        self.config.register_global(
            api_key=None,
            model_name=DEFAULT_MODEL,
            enable_search=True,
            min_api_call_gap=0.5,
        )
        self._session: Optional[aiohttp.ClientSession] = None
        self._cache: Dict[str, Tuple[float, discord.Embed]] = {}
        self._api_lock = asyncio.Lock()
        self._last_call = 0.0

    async def cog_load(self):
        self._session = aiohttp.ClientSession()
        log.info("GrokCog loaded. Search enabled: %s", HAS_DDG)

    async def cog_unload(self):
        if self._session:
            await self._session.close()

    async def _get_search_results(self, query: str) -> List[Dict[str, str]]:
        """Fetch web search results if enabled."""
        if not HAS_DDG or not await self.config.enable_search():
            return []

<<<<<<< HEAD
        # Skip for short greetings to save time
        if len(query) < 4 or query.lower().split()[0] in ["hi", "hello", "yo"]:
=======
        # Skip casual words
        if len(query) < 4 or query.lower().split() in ["hi", "hey", "yo", "hello"]:
>>>>>>> parent of 320505f (Update grokcog.py)
            return []

        # Run search in thread to avoid blocking bot
        def _search():
            try:
                with DDGS() as ddgs:
                    return list(ddgs.text(query, max_results=3))
            except Exception as e:
<<<<<<< HEAD
                log.warning(f"DDG Search Failed: {e}")
=======
                log.warning(f"DDG Search Error: {e}")
>>>>>>> parent of 320505f (Update grokcog.py)
                return []

        return await self.bot.loop.run_in_executor(None, _search)

    async def _ask_api(self, prompt: str, context: str = "") -> dict:
        api_key = await self.config.api_key()
        if not api_key:
<<<<<<< HEAD
            return {
                "answer": "⚠️ **API Key Missing**. Use `[p]grokadmin setkey`.",
                "confidence": 0,
                "sources": [],
            }
=======
            return {"answer": "⚠️ API Key not set. Use `[p]grokadmin setkey`.", "confidence": 0, "sources": []}
>>>>>>> parent of 320505f (Update grokcog.py)

        # Rate limit check
        async with self._api_lock:
            now = time.time()
            gap = await self.config.min_api_call_gap()
            if now - self._last_call < gap:
                await asyncio.sleep(gap - (now - self._last_call))
            self._last_call = time.time()

        # Construct Payload
        messages = [{"role": "system", "content": K2_PROMPT}]
        content_str = f"SEARCH RESULTS:\n{context}\n\nUSER:\n{prompt}" if context else prompt
        messages.append({"role": "user", "content": content_str})

<<<<<<< HEAD
        if context:
            content = f"SEARCH RESULTS:\n{context}\n\nUSER QUESTION:\n{prompt}"
        else:
            content = prompt

        messages.append({"role": "user", "content": content})

        payload = {
            "model": await self.config.model_name(),
            "messages": messages,
            "temperature": 0.3,
            "max_completion_tokens": 4096,
            "stream": False,
        }
=======
        model = await self.config.model_name()
>>>>>>> parent of 320505f (Update grokcog.py)

        try:
            async with self._session.post(
                f"{GROQ_API_BASE}/chat/completions",
                json={
                    "model": model,
                    "messages": messages,
                    "temperature": 0.3,
                    "max_completion_tokens": 4096,
                    "stream": False
                },
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=30
            ) as resp:
<<<<<<< HEAD
                response_text = await resp.text()

                # Debugging: Print error if not 200
                if resp.status != 200:
                    log.error(f"API Error {resp.status}: {response_text}")
                    return {
                        "answer": f"⚠️ **API Error {resp.status}**: {response_text[:200]}...",
                        "confidence": 0,
                        "sources": [],
                    }

                data = json.loads(response_text)
                return self._parse_json(data["choices"][0]["message"]["content"])

        except Exception as e:
            log.exception("API Request Exception")
            return {
                "answer": f"⚠️ **Connection Failed**: {str(e)}",
                "confidence": 0,
                "sources": [],
            }

    def _parse_json(self, text: str) -> dict:
        """Parse JSON safely."""
=======
                text = await resp.text()

                if resp.status != 200:
                    log.error(f"Groq API Error {resp.status}: {text}")
                    return {"answer": f"⚠️ API Error {resp.status}: {text}", "confidence": 0}

                data = json.loads(text)
                return self._parse_json(data["choices"]["message"]["content"])

        except Exception as e:
            log.error(f"Connection Failed: {e}")
            return {"answer": "⚠️ Connection failed.", "confidence": 0}

    def _parse_json(self, text: str) -> dict:
        # 1. Try pure JSON
>>>>>>> parent of 320505f (Update grokcog.py)
        try:
            return json.loads(text)
        except:
            pass

<<<<<<< HEAD
        # 1. Try Code Block
        match = re.search(r"``````", text, re.DOTALL)
=======
        # 2. Regex for ```json ... ```
        match = JSON_BLOCK_REGEX.search(text)
>>>>>>> parent of 320505f (Update grokcog.py)
        if match:
            try:
                return json.loads(match.group(1))
            except:
                pass

<<<<<<< HEAD
        # 2. Try Raw Braces
        match = re.search(r"\{.*?\}", text, re.DOTALL)
=======
        # 3. Regex for { ... }
        match = JSON_BRACE_REGEX.search(text)
>>>>>>> parent of 320505f (Update grokcog.py)
        if match:
            try:
                return json.loads(match.group(0))
            except:
                pass

<<<<<<< HEAD
<<<<<<< HEAD
=======
        # 4. Fallback
>>>>>>> parent of 320505f (Update grokcog.py)
        return {"answer": text, "confidence": 0.5, "sources": []}

    def _create_embed(self, data: dict) -> discord.Embed:
        answer = data.get("answer", "No response.")
        confidence = data.get("confidence", 0.0)
        sources = data.get("sources", [])

        # Color logic
        color = discord.Color.green() if confidence > 0.8 else discord.Color.gold()
<<<<<<< HEAD
        if "⚠️" in answer:
            color = discord.Color.red()
=======
    async def _process(
        self, user_id: int, guild_id: Optional[int], question: str, channel
    ):
        if not await self._validate(user_id, guild_id, question, channel):
            return
        task = asyncio.current_task()
        self._active[user_id] = task
        key = self._key(question)
        try:
            if cached := self._cache_get(key):
                log.debug(f"Cache hit for user {user_id}")
                await channel.send(embed=cached)
                return
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
                    await channel.send(embed=formatted)
                    return
                except asyncio.TimeoutError:
                    log.warning("Deduplication wait timed out, making new request")
            async with channel.typing():
                temperature = 0.3
                if guild_id:
                    temp_config = await self.config.guild_from_id(
                        guild_id
                    ).default_temperature()
                    temperature = temp_config
                api_coro = self._ask_groq(question, temperature)
                if await self.config.request_queue_enabled():
                    future = asyncio.Future()
                    self._inflight_requests[key] = future
                    try:
                        result = await self._api_queue.enqueue(api_coro)
                        if not future.done():
                            future.set_result(result)
                    except Exception as e:
                        if not future.done():
                            future.set_exception(e)
                        raise
                    finally:
                        self._inflight_requests.pop(key, None)
                else:
                    result = await api_coro
                embed = self._format(result)
                self._cache_set(key, embed)
                await channel.send(embed=embed)
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
=======
        if "⚠️" in answer: color = discord.Color.red()
>>>>>>> parent of 320505f (Update grokcog.py)

>>>>>>> parent of 8473840 (Update grokcog.py)

def _format(self, data: dict) -> discord.Embed:
    if not isinstance(data, dict):
        embed = discord.Embed(
<<<<<<< HEAD
            title="DripBot's Response",
            description=answer[:4000],
            color=color,
            timestamp=datetime.now(timezone.utc)
        )

<<<<<<< HEAD
=======
        # Smart Footer/Fields
>>>>>>> parent of 320505f (Update grokcog.py)
        show_stats = sources or (len(answer) > 100 and "⚠️" not in answer)

        if show_stats:
            embed.add_field(name="Confidence", value=f"{confidence:.0%}", inline=True)

        if sources:
            links = [f"[{i+1}. {s.get('title','Src')}]({s.get('url','#')})" for i,s in enumerate(sources[:5])]
            embed.add_field(name="Sources", value="\n".join(links), inline=False)

        embed.set_footer(text="Powered by 2 Romanian kids -  Retardation only (fact-checks)")
        return embed

    @commands.command()
    @commands.cooldown(1, 3, commands.BucketType.user)
    async def grok(self, ctx: commands.Context, *, query: str):
        """Ask the AI."""
        try:
            async with ctx.typing():
                # Cache Key
                key = hashlib.md5(query.lower().encode()).hexdigest()

                # Check Cache
                if key in self._cache:
                    ts, cached_embed = self._cache[key]
                    if time.time() - ts < 3600:
                        await ctx.send(embed=cached_embed)
                        return

<<<<<<< HEAD
                # 1. Get Search Context
=======
                # Pipeline: Search -> Ask -> Format
>>>>>>> parent of 320505f (Update grokcog.py)
                search_res = await self._get_search_results(query)

                context = ""
                if search_res:
                    context = "\n".join([f"- {r.get('title')}: {r.get('body')}" for r in search_res])

<<<<<<< HEAD
                # 2. Ask API
                response = await self._ask_api(query, context)

                # 3. Send Result
                embed = self._create_embed(response)
                self._cache[key] = (time.time(), embed)
                await ctx.send(embed=embed)

        except Exception as e:
            await ctx.send(f"❌ **Critical Error**: {e}")
=======
            title="Error",
            description=f"Invalid response type: {type(data)}",
            color=discord.Color.red(),
        )
        return embed

    answer = data.get("answer", "")
    if not answer:
        embed = discord.Embed(
            title="No Response",
            description="No answer received from AI",
            color=discord.Color.red(),
        )
        return embed

    confidence = data.get("confidence", 0.0)
    sources = data.get("sources", [])

    if confidence >= 0.9:
        color = discord.Color.green()
    elif confidence >= 0.7:
        color = discord.Color.blue()
    elif confidence >= 0.5:
        color = discord.Color.gold()
    else:
        color = discord.Color.orange()

    embed = discord.Embed(
        title="DripBot's Response",
        description=answer[:4096],
        color=color,
        timestamp=datetime.now(timezone.utc),
    )

    is_informational = sources or confidence >= 0.7

    if is_informational:
        embed.add_field(name="Confidence", value=f"{confidence:.0%}", inline=True)

    if sources and isinstance(sources, list):
        source_text = ""
        for i, src in enumerate(sources[:5], 1):
            if isinstance(src, dict):
                title = src.get("title", "Source")[:100]
                url = src.get("url", "")
                if url:
                    source_text += f"{i}. [{title}]({url})\n"
                else:
                    source_text += f"{i}. {title}\n"
        if source_text:
            embed.add_field(name="Sources", value=source_text[:1024], inline=False)

    embed.set_footer(text="Powered by 2 Romanian kids • Retardation only (fact-checks)")

    return embed
>>>>>>> parent of 8473840 (Update grokcog.py)
=======
                response_data = await self._ask_api(query, context)

                final_embed = self._create_embed(response_data)
                self._cache[key] = (time.time(), final_embed)

                await ctx.send(embed=final_embed)

        except Exception as e:
            log.exception("Command failed")
            await ctx.send(f"❌ internal error: {e}")
>>>>>>> parent of 320505f (Update grokcog.py)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot: return

        # Robust mention check
        if self.bot.user.id in [m.id for m in message.mentions]:
            # Remove mention from text
            clean_content = re.sub(f"<@!?{self.bot.user.id}>", "", message.content).strip()
            if clean_content:
                ctx = await self.bot.get_context(message)
                await ctx.invoke(self.grok, query=clean_content)

    @commands.group()
    @commands.is_owner()
    async def grokadmin(self, ctx):
        pass

    @grokadmin.command()
    async def setkey(self, ctx, key: str):
        await self.config.api_key.set(key)
        await ctx.send("✅ API Key updated.")

    @grokadmin.command()
    async def setmodel(self, ctx, model: str):
        await self.config.model_name.set(model)
        await ctx.send(f"✅ Model set to `{model}`")

    @grokadmin.command()
    async def togglesearch(self, ctx):
        val = not await self.config.enable_search()
        await self.config.enable_search.set(val)
<<<<<<< HEAD
        await ctx.send(f"Web Search: {val}")

=======
        await ctx.send(f"Search enabled: {val}")
>>>>>>> parent of 320505f (Update grokcog.py)

async def setup(bot):
    await bot.add_cog(GrokCog(bot))
