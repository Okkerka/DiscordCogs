import asyncio
import json
import logging
import hashlib
from typing import Dict, List, Optional, Tuple
from datetime import datetime, timedelta
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

import discord
from redbot.core import commands, Config, checks

log = logging.getLogger("red.grokcog")

ROUTER_SYSTEM_PROMPT = """Role: Helpful analyst that answers clearly and cites web snippets when available.

Router:
- If math → compute result concisely.
- If time/date → answer directly.
- If a checkable claim → do fact-check.
- Else → general Q&A with concise answer + 2-3 key bullets.

Policies:
- For fact-checks return JSON: {"type":"fact","verdict":"TRUE|FALSE|UNCLEAR","reason":"...", "citations":[1,2]}
- For general Q&A return JSON: {"type":"qa","answer":"...","bullets":["...","..."], "citations":[1,3]}
- Use evidence from provided Sources only; if weak, say so.
- Think step-by-step privately; do not reveal chain-of-thought.
"""

def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:10]


class GrokCog(commands.Cog):
    """Intelligent assistant (Groq Llama 3.3 70B) with search, mentions, >grok, and DMs."""

    __version__ = "1.2.0"

    def __init__(self, bot):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=987654321098765, force_registration=True)

        self.config.register_global(
            api_key=None,
            model="llama-3.3-70b-versatile",
            timeout=30,
            max_retries=3,
        )
        self.config.register_guild(enabled=True, max_input_length=2000)
        self.config.register_user(request_count=0, last_request_time=None)

        self._active_requests: Dict[int, asyncio.Task] = {}
        self._cache_search: Dict[str, Tuple[float, str]] = {}
        self._cache_answer: Dict[str, Tuple[float, str]] = {}

    async def cog_unload(self):
        for task in self._active_requests.values():
            if not task.done():
                task.cancel()
        self._active_requests.clear()

    # ------------------ Utilities ------------------

    @staticmethod
    def _trim(s: str, n: int) -> str:
        return s if len(s) <= n else s[: n - 1] + "…"

    def _cache_get(self, store: Dict[str, Tuple[float, str]], key: str, ttl: int = 86400) -> Optional[str]:
        now = datetime.utcnow().timestamp()
        item = store.get(key)
        if not item:
            return None
        ts, val = item
        if now - ts > ttl:
            store.pop(key, None)
            return None
        return val

    def _cache_set(self, store: Dict[str, Tuple[float, str]], key: str, val: str):
        self._cache_prune(store)
        store[key] = (datetime.utcnow().timestamp(), val)

    def _cache_prune(self, store: Dict[str, Tuple[float, str]], max_items: int = 256):
        if len(store) <= max_items:
            return
        # Drop oldest half
        items = sorted(store.items(), key=lambda kv: kv[1][0])
        for k, _ in items[: len(items) // 2]:
            store.pop(k, None)

    # ------------------ Search ------------------

    @staticmethod
    def _web_search(query: str, max_results: int = 5) -> List[Dict[str, str]]:
        try:
            from ddgs import DDGS
            results = list(DDGS().text(query, max_results=max_results))
            cleaned = []
            for r in results:
                title = r.get("title") or "Untitled"
                body = r.get("body") or ""
                href = r.get("href") or ""
                cleaned.append({"title": title, "snippet": body.strip(), "url": href})
            return cleaned
        except ImportError:
            return [{"title": "Dependency missing", "snippet": "Run: pip install ddgs", "url": ""}]
        except Exception as e:
            log.error(f"Search error: {e}")
            return [{"title": "Search failed", "snippet": "Search provider error", "url": ""}]

    def _format_sources(self, results: List[Dict[str, str]], limit: int = 5) -> str:
        # Order: best first, second-best last (use simple length heuristic)
        results = results[:limit]
        if not results:
            return "Sources:\n"
        scored = sorted(results, key=lambda r: len(r.get("snippet", "")), reverse=True)
        if len(scored) >= 2:
            first = scored[0]
            last = scored[1]
            rest = scored[2:]
            ordered = [first] + rest + [last]
        else:
            ordered = scored

        lines = ["Sources:"]
        for i, r in enumerate(ordered, 1):
            snippet = self._trim(r.get("snippet", "").replace("\n", " "), 280)
            title = self._trim(r.get("title", ""), 80)
            lines.append(f"{i}) {title} — \"{snippet}\"")
        return "\n".join(lines)

    # ------------------ Groq Calls ------------------

    def _groq_chat(self, api_key: str, model: str, messages: List[Dict], temperature: float, top_p: float, max_tokens: int) -> str:
        payload = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "top_p": top_p,
            "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},
        }
        req = Request(
            "https://api.groq.com/openai/v1/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            method="POST",
        )
        resp = urlopen(req, timeout=30)
        result = json.loads(resp.read().decode("utf-8"))
        return result["choices"][0]["message"]["content"].strip()

    def _decide_type(self, text: str) -> str:
        # naive: presence of '?', verbs; keep simple
        t = text.lower()
        if any(k in t for k in ["true or false", "is it true", "prove", "claim", "alleg", "fact check", "fact-check"]):
            return "fact"
        # short heuristic: if ends with '?', treat as qa
        return "qa" if "?" in t and len(t) < 300 else "auto"

    def _verdict_vote(self, api_key: str, model: str, user_input: str, sources_text: str) -> str:
        # Three quick votes at higher temp; returns label
        messages = [
            {"role": "system", "content": ROUTER_SYSTEM_PROMPT},
            {"role": "user", "content": f"Task: Decide verdict only.\nUser: {user_input}\n\n{sources_text}\nReturn JSON: {{\"verdict\":\"TRUE|FALSE|UNCLEAR\"}}"},
        ]
        labels = []
        for _ in range(3):
            try:
                out = self._groq_chat(api_key, model, messages, temperature=0.6, top_p=0.9, max_tokens=60)
                data = json.loads(out)
                v = str(data.get("verdict", "")).upper()
                if v in ("TRUE", "FALSE", "UNCLEAR"):
                    labels.append(v)
            except Exception as e:
                log.debug(f"vote error: {e}")
        if not labels:
            return "UNCLEAR"
        return max(set(labels), key=labels.count)

    def _answer_json(self, api_key: str, model: str, user_input: str, sources_text: str, forced_verdict: Optional[str]) -> Dict:
        base_user = f"User: {user_input}\n\n{sources_text}"
        if forced_verdict:
            base_user += f"\n\nGivenVerdict: {forced_verdict}"
        messages = [
            {"role": "system", "content": ROUTER_SYSTEM_PROMPT},
            {"role": "user", "content": base_user},
        ]
        out = self._groq_chat(api_key, model, messages, temperature=0.3, top_p=0.9, max_tokens=480)
        try:
            data = json.loads(out)
        except Exception:
            # fallback minimal schema
            data = {"type": "qa", "answer": out, "bullets": [], "citations": []}
        return data

    # ------------------ Core processing ------------------

    async def _process(self, user_id: int, guild_id: int, question: str, channel: discord.abc.Messageable):
        # Server gate
        if isinstance(channel, discord.TextChannel):
            guild_cfg = await self.config.guild_from_id(guild_id).all()
            if not guild_cfg["enabled"]:
                return await channel.send("❌ Disabled here")

        if not question.strip():
            return await channel.send("❌ Empty")
        if len(question) > 2000:
            return await channel.send("❌ Too long")

        if user_id in self._active_requests and not self._active_requests[user_id].done():
            return await channel.send("❌ Already processing")

        api_key = await self.config.api_key()
        model = await self.config.model()
        if not api_key:
            return await channel.send("❌ No API key set")

        self._active_requests[user_id] = asyncio.current_task()
        cache_key = _sha(question)
        search_msg = None

        try:
            # Cache check
            cached = self._cache_get(self._cache_answer, cache_key)
            if cached:
                return await channel.send(cached)

            search_msg = await channel.send("🔍 Searching…")
            # Search cache
            s_key = _sha("search|" + question.lower())
            s_cached = self._cache_get(self._cache_search, s_key)
            if s_cached:
                sources_text = s_cached
            else:
                results = await asyncio.to_thread(self._web_search, question, 5)
                sources_text = self._format_sources(results, 5)
                self._cache_set(self._cache_search, s_key, sources_text)

            await search_msg.edit(content="🧠 Thinking…")

            # Fact vs QA: do quick verdict voting if looks like a claim
            forced_verdict: Optional[str] = None
            if self._decide_type(question) != "qa":
                forced_verdict = await asyncio.to_thread(self._verdict_vote, api_key, model, question, sources_text)

            data = await asyncio.to_thread(self._answer_json, api_key, model, question, sources_text, forced_verdict)

            # Compose Discord-friendly output
            if data.get("type") == "fact":
                verdict = data.get("verdict", forced_verdict or "UNCLEAR")
                reason = data.get("reason", "")
                cits = data.get("citations", [])
                text = f"VERDICT: {verdict}\nREASON: {reason}"
            else:
                answer = data.get("answer", "")
                bullets = data.get("bullets", [])[:3]
                parts = [answer] + [f"• {b}" for b in bullets if b]
                text = "\n".join([p for p in parts if p])

            await search_msg.delete()
            # Pagination safeguard
            if len(text) > 1800:
                for chunk in [text[i : i + 1800] for i in range(0, len(text), 1800)]:
                    await channel.send(chunk)
            else:
                await channel.send(text)

            self._cache_set(self._cache_answer, cache_key, text)

            async with self.config.user_from_id(user_id).all() as u:
                u["request_count"] += 1
                u["last_request_time"] = datetime.utcnow().isoformat()

        except commands.UserFeedbackCheckFailure as e:
            if search_msg:
                await search_msg.delete()
            await channel.send(str(e))
        except asyncio.CancelledError:
            if search_msg:
                await search_msg.delete()
        except HTTPError as e:
            if search_msg:
                await search_msg.delete()
            await channel.send(f"❌ API error {e.code}")
        except Exception as e:
            if search_msg:
                await search_msg.delete()
            log.error(f"Error: {e}", exc_info=True)
            await channel.send("❌ Error")
        finally:
            self._active_requests.pop(user_id, None)

    # ------------------ Triggers ------------------

    @commands.Cog.listener()
    async def on_message(self, msg: discord.Message):
        if msg.author.bot:
            return

        # Server mention flow
        if msg.guild and self.bot.user in msg.mentions:
            q = msg.content
            for u in msg.mentions:
                q = q.replace(f"<@{u.id}>", "").replace(f"<@!{u.id}>", "")
            q = q.strip()

            # Reply capture
            if msg.reference:
                try:
                    replied = await msg.channel.fetch_message(msg.reference.message_id)
                    if replied and replied.content:
                        q = replied.content
                except Exception:
                    pass

            if q:
                await self._process(msg.author.id, msg.guild.id, q, msg.channel)

        # DMs & Group DMs: auto respond (mutual server required by Discord)
        elif isinstance(msg.channel, (discord.DMChannel, discord.GroupChannel)):
            q = msg.content.strip()
            if q and not q.startswith((">", "/")):
                await self._process(msg.author.id, msg.channel.id, q, msg.channel)

    # ------------------ Commands ------------------

    @commands.group(name="grok", invoke_without_command=True)
    @commands.cooldown(1, 20, commands.BucketType.user)
    async def grok(self, ctx: commands.Context, *, question: str = None):
        """Ask a question or fact-check a claim."""
        if not question:
            return
        if not ctx.guild:
            return await ctx.send("❌ Use in a server channel or DM me directly.")
        await self._process(ctx.author.id, ctx.guild.id, question, ctx.channel)

    @grok.command(name="stats")
    async def stats(self, ctx: commands.Context):
        """Show your usage stats."""
        cfg = await self.config.user(ctx.author).all()
        embed = discord.Embed(title="Grok Stats", color=discord.Color.blue())
        embed.add_field(name="Queries", value=cfg["request_count"])
        if cfg["last_request_time"]:
            ts = int(datetime.fromisoformat(cfg["last_request_time"]).timestamp())
            embed.add_field(name="Last", value=f"<t:{ts}:R>")
        await ctx.send(embed=embed)

    @grok.group(name="set")
    async def grok_set(self, ctx):
        """Owner/admin settings."""
        pass

    @grok_set.command(name="apikey")
    async def apikey(self, ctx: commands.Context, key: str):
        if not await self.bot.is_owner(ctx.author):
            return await ctx.send("❌ Owner only")
        if not key or len(key) < 10:
            return await ctx.send("❌ Invalid key")
        await self.config.api_key.set(key)
        await ctx.send("✅ API key saved")

    @grok_set.command(name="model")
    async def set_model(self, ctx: commands.Context, *, name: str = "llama-3.3-70b-versatile"):
        if not await self.bot.is_owner(ctx.author):
            return await ctx.send("❌ Owner only")
        await self.config.model.set(name)
        await ctx.send(f"✅ Model set to {name}")

    @grok_set.command(name="toggle")
    async def toggle(self, ctx: commands.Context):
        if not ctx.guild:
            return await ctx.send("❌ Use in a server")
        if not await checks.admin_or_permissions(manage_guild=True).predicate(ctx):
            return await ctx.send("❌ Admin only")
        cur = await self.config.guild(ctx.guild).enabled()
        await self.config.guild(ctx.guild).enabled.set(not cur)
        await ctx.send(f"{'✅ Enabled' if not cur else '❌ Disabled'} here")

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