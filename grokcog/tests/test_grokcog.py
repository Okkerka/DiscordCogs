import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest
from discord.ext import commands

from grokcog.grokcog import GrokCog


@pytest.mark.parametrize(
    "content",
    ["``````", "[]", "null", '{"answer":null}', '{"answer":"ok","confidence":"high"}'],
)
def test_malformed_output_is_displayable(content):
    cog = object.__new__(GrokCog)
    result = cog._extract_json(content)
    assert isinstance(result, dict)
    assert cog._format(result).description


def test_cache_preserves_case():
    assert GrokCog._key("Explain Foo") != GrokCog._key("Explain foo")


@pytest.mark.asyncio
async def test_slash_question_and_new_commands_register():
    bot = commands.Bot(command_prefix=">", intents=discord.Intents.none())
    async with bot:
        cog = GrokCog.__new__(GrokCog)
        cog.cog_load = AsyncMock()
        cog.cog_unload = AsyncMock()
        await bot.add_cog(cog)
        root = bot.tree.get_command("grok")
        names = {cmd.name for cmd in root.commands}
        assert {"ask", "search", "models", "cancel", "stats"} <= names
        root.to_dict(bot.tree)


@pytest.mark.asyncio
async def test_uncached_reply_is_fetched_and_used():
    cog = object.__new__(GrokCog)
    replied = SimpleNamespace(content="The moon is made of cheese", embeds=[])
    channel = SimpleNamespace(id=10, fetch_message=AsyncMock(return_value=replied))
    message = SimpleNamespace(
        channel=channel,
        reference=SimpleNamespace(resolved=None, message_id=123, channel_id=10),
    )
    query = await cog._build_context_query(message, "is this true?")
    assert "The moon is made of cheese" in query
    channel.fetch_message.assert_awaited_once_with(123)


@pytest.mark.parametrize(
    "question",
    ["is this true?", "fact check this", "look this up", "what is the latest news?"],
)
def test_fact_checks_use_search(question):
    assert GrokCog._needs_search(question)


def test_basic_math_does_not_require_search():
    assert not GrokCog._needs_search("whats 9x9?")


@pytest.mark.asyncio
async def test_duplicate_cancellation_does_not_cancel_other_waiter():
    cog = object.__new__(GrokCog)
    cog._inflight_requests = {}
    cog._waiters = {}
    started, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def work():
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return {"answer": "81"}

    first = asyncio.create_task(cog._shared_request("key", work))
    await started.wait()
    second = asyncio.create_task(cog._shared_request("key", work))
    await asyncio.sleep(0)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    release.set()
    assert await second == {"answer": "81"}
    assert calls == 1
    assert not cog._inflight_requests


@pytest.mark.asyncio
async def test_duplicate_failure_reaches_all_waiters():
    cog = object.__new__(GrokCog)
    cog._inflight_requests = {}
    cog._waiters = {}
    release = asyncio.Event()

    async def work():
        await release.wait()
        raise ValueError("provider unavailable")

    tasks = [asyncio.create_task(cog._shared_request("key", work)) for _ in range(2)]
    await asyncio.sleep(0)
    release.set()
    results = await asyncio.gather(*tasks, return_exceptions=True)
    assert all(isinstance(result, ValueError) for result in results)
    assert not cog._inflight_requests
