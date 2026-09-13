import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import discord
import pytest

from grokcog.grokcog import GrokCog
from grokcog.helpers import (
    AnswerPages,
    ProviderError,
    answer_pages,
    response_answer,
    safe_sources,
)


class Value:
    def __init__(self, parent, name):
        self.parent, self.name = parent, name

    async def __call__(self):
        return self.parent.data[self.name]

    async def set(self, value):
        self.parent.data[self.name] = value


class AllValues:
    def __init__(self, group):
        self.group = group

    def __await__(self):
        async def read():
            return self.group.data.copy()

        return read().__await__()

    async def __aenter__(self):
        return self.group.data

    async def __aexit__(self, *args):
        return False


class Group:
    def __init__(self, **data):
        self.data = data

    def __getattr__(self, name):
        return Value(self, name)

    def all(self):
        return AllValues(self)

    async def clear(self):
        self.data.clear()


class Config(Group):
    def __init__(self):
        super().__init__()
        self.users, self.guilds = {}, {}

    def register_global(self, **data):
        self.data.update(data)

    def register_guild(self, **data):
        self.guild_defaults = data

    def register_user(self, **data):
        self.user_defaults = data

    def user_from_id(self, uid):
        return self.users.setdefault(uid, Group(**self.user_defaults))

    def user(self, user):
        return self.user_from_id(user.id)

    def guild(self, guild):
        return self.guilds.setdefault(guild.id, Group(**self.guild_defaults))


@pytest.fixture
def cog():
    config = Config()
    with patch("grokcog.grokcog.Config.get_conf", return_value=config):
        result = GrokCog(Mock())
    result._ready.set()
    config.data.update(api_key="test-key", min_api_call_gap=0, cooldown_seconds=0)
    return result


class Typing:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


def context(uid=1, channel_id=10, slash=False):
    return SimpleNamespace(
        author=SimpleNamespace(id=uid),
        guild=SimpleNamespace(id=20),
        channel=SimpleNamespace(id=channel_id),
        interaction=object() if slash else None,
        message=SimpleNamespace(reference=None),
        typing=Mock(return_value=Typing()),
        send=AsyncMock(),
        reply=AsyncMock(),
        valid=False,
    )


def answer(text="81"):
    return {"answer": text, "sources": [], "model": "test-model", "searched": False}


@pytest.mark.asyncio
async def test_process_uses_reply_context_and_search(cog):
    ctx = context()
    ctx.message.reference = SimpleNamespace(
        resolved=SimpleNamespace(
            content="A test claim", embeds=[discord.Embed(description="Extra evidence")]
        )
    )
    cog._run_request = AsyncMock(return_value=answer())
    await cog._process(ctx, "is this true?")
    query, temperature, search, model = cog._run_request.call_args.args
    assert temperature == 0.3
    assert "A test claim" in query and "Extra evidence" in query
    assert search and model == "groq/compound"
    ctx.reply.assert_awaited_once()
    assert ctx.reply.call_args.kwargs["mention_author"] is False
    assert not ctx.reply.call_args.kwargs["allowed_mentions"].everyone


@pytest.mark.asyncio
async def test_slash_sends_through_context_and_counts_cache_hits(cog):
    ctx = context(slash=True)
    cog._run_request = AsyncMock(return_value=answer())
    await cog._process(ctx, "whats 9x9?")
    await cog._process(ctx, "whats 9x9?")
    assert cog._run_request.await_count == 1
    assert ctx.send.await_count == 2
    ctx.reply.assert_not_awaited()
    assert cog.config.user(ctx.author).data["request_count"] == 2


@pytest.mark.asyncio
async def test_cache_isolated_by_channel_model_and_case(cog):
    ctx = context()
    cog._run_request = AsyncMock(return_value=answer())
    await cog._process(ctx, "Foo")
    await cog._process(ctx, "foo")
    await cog._process(context(channel_id=11), "Foo")
    await cog.config.model_name.set("new-model")
    await cog._process(ctx, "Foo")
    assert cog._run_request.await_count == 4


@pytest.mark.asyncio
async def test_configured_cooldown_applies_to_process(cog):
    ctx = context()
    cog._run_request = AsyncMock(return_value=answer())
    await cog.config.cooldown_seconds.set(60)
    await cog._process(ctx, "first")
    await cog._process(ctx, "second")
    assert cog._run_request.await_count == 1
    assert "wait" in ctx.send.call_args.args[0]


@pytest.mark.asyncio
async def test_same_user_cannot_race_validation(cog):
    release = asyncio.Event()

    async def request(*args):
        await release.wait()
        return answer()

    cog._run_request = request
    first = asyncio.create_task(cog._process(context(), "one"))
    await asyncio.sleep(0)
    second = context()
    await cog._process(second, "two")
    assert "previous request" in second.send.call_args.args[0]
    release.set()
    await first


@pytest.mark.asyncio
async def test_cache_clear_during_request_prevents_repopulation(cog):
    started, release = asyncio.Event(), asyncio.Event()

    async def request(*args):
        started.set()
        await release.wait()
        return answer()

    cog._run_request = request
    task = asyncio.create_task(cog._process(context(), "one"))
    await started.wait()
    cog._clear_cache()
    release.set()
    await task
    assert not cog._cache


@pytest.mark.asyncio
async def test_unload_cancels_requests_and_closes_session(cog):
    started = asyncio.Event()

    async def request(*args):
        started.set()
        await asyncio.Event().wait()

    cog._run_request = request
    task = asyncio.create_task(cog._process(context(), "one"))
    await started.wait()
    cog._session = SimpleNamespace(closed=False, close=AsyncMock())
    await cog.cog_unload()
    assert task.cancelled()
    assert not cog._inflight_requests and not cog._active
    cog._session.close.assert_awaited_once()


class Response:
    def __init__(self, status, data=None, headers=None):
        self.status, self.headers = status, headers or {}
        raw = json.dumps(data).encode()

        async def chunks(size):
            for offset in range(0, len(raw), 3):
                yield raw[offset : offset + 3]

        self.content = SimpleNamespace(iter_chunked=chunks)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


@pytest.mark.asyncio
async def test_http_chunks_are_read_to_eof(cog):
    cog._session = SimpleNamespace(
        closed=False, request=Mock(return_value=Response(200, {"long": "response"}))
    )
    assert await cog._request_json("GET", "/models") == {"long": "response"}


@pytest.mark.asyncio
async def test_429_retries_use_limiter_and_fractional_retry_after(cog, monkeypatch):
    cog._session = SimpleNamespace(
        closed=False,
        request=Mock(
            side_effect=[
                Response(429, headers={"Retry-After": "0.25"}),
                Response(200, {"ok": True}),
            ]
        ),
    )
    cog._respect_api_rate_limits = AsyncMock()
    sleep = AsyncMock()
    monkeypatch.setattr("grokcog.grokcog.asyncio.sleep", sleep)
    assert await cog._request_json("POST", "/chat/completions") == {"ok": True}
    assert cog._respect_api_rate_limits.await_count == 2
    sleep.assert_awaited_once_with(0.25)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 401, 403, 404])
async def test_permanent_error_not_retried_or_exposed(cog, status):
    cog._session = SimpleNamespace(
        closed=False,
        request=Mock(return_value=Response(status, {"error": "private-detail"})),
    )
    with pytest.raises(ProviderError) as error:
        await cog._request_json("POST", "/chat/completions")
    assert "private-detail" not in str(error.value)
    assert cog._session.request.call_count == 1


def test_search_sources_are_from_metadata_not_model_json():
    data = {
        "choices": [
            {
                "message": {
                    "content": json.dumps(
                        {
                            "answer": "Claim",
                            "sources": [
                                {"url": "https://invented.test", "title": "Made up"}
                            ],
                        }
                    ),
                    "executed_tools": [
                        {
                            "search_results": {
                                "results": [
                                    {"url": "https://actual.test", "title": "Actual"}
                                ]
                            }
                        }
                    ],
                }
            }
        ]
    }
    result = response_answer(data, "groq/compound", True)
    assert result["sources"] == [{"url": "https://actual.test", "title": "Actual"}]
    assert result["searched"]


def test_search_without_evidence_never_presents_unverified_verdict():
    result = response_answer(
        {"choices": [{"message": {"content": "Definitely true."}}]},
        "groq/compound",
        True,
    )
    assert "Definitely true" not in result["answer"]
    assert "can't verify" in result["answer"]


def test_long_answer_preserved_in_pages():
    text = "abcdef" * 2000
    pages = answer_pages(answer(text))
    assert "".join(page.description for page in pages) == text
    assert all(len(page.description) <= 4096 and len(page) <= 6000 for page in pages)
    assert all(
        "test-model" in page.footer.text and "Fact-Checked" not in page.footer.text
        for page in pages
    )


def test_invalid_source_urls_rejected():
    assert (
        safe_sources(
            [
                {"url": "javascript:alert(1)"},
                {"url": "https://a.test/with space"},
                {"url": "https://user:secret@host.test"},
                None,
            ]
        )
        == []
    )


@pytest.mark.asyncio
async def test_pagination_restricted_to_asker():
    view = AnswerPages(1, answer_pages(answer("long" * 2000)))
    interaction = SimpleNamespace(
        user=SimpleNamespace(id=2), response=SimpleNamespace(send_message=AsyncMock())
    )
    assert not await view.interaction_check(interaction)
    interaction.response.send_message.assert_awaited_once()
    view.stop()


@pytest.mark.asyncio
async def test_public_prefix_key_is_not_saved(cog):
    ctx = context()
    ctx.message.delete = AsyncMock()
    await GrokCog.admin_apikey.callback(cog, ctx, api_key="new-key")
    assert await cog.config.api_key() == "test-key"
    ctx.message.delete.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "per_minute,gap",
    [(0, 0), (-1, 1), (60, float("nan")), (60, float("inf")), (60, -1)],
)
async def test_invalid_rate_settings_rejected(cog, per_minute, gap):
    await GrokCog.admin_ratelimits.callback(cog, context(), per_minute, gap)
    assert await cog.config.max_requests_per_minute() == 60
    assert await cog.config.min_api_call_gap() == 0


@pytest.mark.asyncio
async def test_mention_listener_preserves_other_mentions(cog):
    bot_user = SimpleNamespace(id=99)
    ctx = context()
    cog.bot = SimpleNamespace(
        user=bot_user,
        cog_disabled_in_guild=AsyncMock(return_value=False),
        allowed_by_whitelist_blacklist=AsyncMock(return_value=True),
        ignored_channel_or_guild=AsyncMock(return_value=True),
        get_context=AsyncMock(return_value=ctx),
    )
    msg = SimpleNamespace(
        author=SimpleNamespace(bot=False),
        guild=ctx.guild,
        content="<@99> is <@55> correct?",
        mentions=[bot_user],
        reference=None,
    )
    cog._process = AsyncMock()
    await cog.on_message(msg)
    cog._process.assert_awaited_once_with(ctx, "is <@55> correct?")


@pytest.mark.asyncio
async def test_listener_ignores_real_commands(cog):
    ctx = context()
    ctx.valid = True
    cog.bot = SimpleNamespace(
        user=SimpleNamespace(id=99),
        cog_disabled_in_guild=AsyncMock(return_value=False),
        allowed_by_whitelist_blacklist=AsyncMock(return_value=True),
        ignored_channel_or_guild=AsyncMock(return_value=True),
        get_context=AsyncMock(return_value=ctx),
    )
    cog._process = AsyncMock()
    await cog.on_message(
        SimpleNamespace(author=SimpleNamespace(bot=False), guild=ctx.guild)
    )
    cog._process.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancel_completes_original_slash_response(cog):
    ctx = context(slash=True)
    started = asyncio.Event()

    async def request(*args):
        started.set()
        await asyncio.Event().wait()

    cog._run_request = request
    task = asyncio.create_task(cog._process(ctx, "one"))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert "cancelled" in ctx.send.call_args.args[0].lower()


@pytest.mark.asyncio
async def test_unload_stops_admin_verification(cog):
    started = asyncio.Event()

    async def ask(*args, **kwargs):
        started.set()
        await asyncio.Event().wait()

    cog._ask_groq = ask
    task = asyncio.create_task(cog._run_request("OK", 0.1, False, "test-model"))
    await started.wait()
    try:
        await cog.cog_unload()
        assert task.done()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_page_timeout_releases_answer_text():
    view = AnswerPages(1, answer_pages(answer("text" * 2000)))
    await view.on_timeout()
    assert view.pages == []
    view.stop()


@pytest.mark.asyncio
async def test_network_concurrency_is_bounded(cog):
    active = peak = 0
    release = asyncio.Event()
    three = asyncio.Event()

    async def ask(*args, **kwargs):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        if active == 3:
            three.set()
        try:
            await release.wait()
            return answer()
        finally:
            active -= 1

    cog._ask_groq = ask
    tasks = [
        asyncio.create_task(cog._run_request(str(i), 0.1, False, "model"))
        for i in range(6)
    ]
    await three.wait()
    release.set()
    await asyncio.gather(*tasks)
    assert peak == 3 and active == 0


def test_documented_search_output_text_can_supply_sources():
    data = {
        "choices": [
            {
                "message": {
                    "content": "A sourced answer.",
                    "executed_tools": [
                        {
                            "type": "search",
                            "output": "Title: Primary source\nURL: https://example.org/evidence\nContent: Evidence text",
                        }
                    ],
                }
            }
        ]
    }
    result = response_answer(data, "groq/compound", True)
    assert result["sources"][0]["url"] == "https://example.org/evidence"
    assert result["answer"] == "A sourced answer."


@pytest.mark.asyncio
async def test_non_owner_cannot_pass_admin_requirements(cog):
    from redbot.core.commands.requires import PermState

    for name in (
        "apikey",
        "verify",
        "cooldown",
        "setmodel",
        "ratelimits",
        "clearcache",
    ):
        cmd = next(
            cmd for cmd in cog.get_commands()[0].walk_commands() if cmd.name == name
        )
        cmd.requires.ready_event.set()
        ctx = context(slash=True)
        ctx.bot = SimpleNamespace(
            is_owner=AsyncMock(return_value=False),
            cog_disabled_in_guild=AsyncMock(return_value=False),
        )
        ctx.command = cmd
        ctx.cog = cog
        ctx.bot_permissions = discord.Permissions.all()
        ctx.permission_state = PermState.NORMAL
        assert not await cmd.requires.verify(ctx)


def test_browser_results_are_usable_search_evidence():
    data = {
        "choices": [
            {
                "message": {
                    "content": "Supported by this page.",
                    "executed_tools": [
                        {
                            "type": "visit_website",
                            "browser_results": [
                                {
                                    "title": "Primary source",
                                    "url": "https://example.org/claim",
                                    "content": "Evidence",
                                }
                            ],
                        }
                    ],
                }
            }
        ]
    }
    result = response_answer(data, "groq/compound", True)
    assert result["searched"]
    assert result["sources"][0]["url"] == "https://example.org/claim"
    assert result["answer"] == "Supported by this page."


def test_missing_sources_logged_without_private_content(caplog):
    data = {
        "choices": [
            {
                "message": {
                    "content": "PRIVATE_MESSAGE",
                    "executed_tools": [
                        {"type": "UNKNOWN_SECRET", "output": "PRIVATE_TOOL_OUTPUT"}
                    ],
                }
            }
        ]
    }
    with caplog.at_level("WARNING", logger="red.grokcog"):
        result = response_answer(data, "groq/compound", True)
    assert "search_no_sources" in caplog.text
    assert "executed_tools=1" in caplog.text
    assert "PRIVATE" not in caplog.text and "UNKNOWN_SECRET" not in caplog.text
    assert "General answer" not in answer_pages(result)[0].footer.text


@pytest.mark.asyncio
async def test_search_without_sources_is_not_cached(cog):
    cog._run_request = AsyncMock(return_value=answer("No sources"))
    ctx = context()
    await cog._process(ctx, "search for this claim")
    await cog._process(ctx, "search for this claim")
    assert cog._run_request.await_count == 2
    assert not cog._cache


@pytest.mark.asyncio
async def test_empty_reply_requests_text_before_calling_provider(cog):
    ctx = context()
    ctx.message.reference = SimpleNamespace(
        resolved=SimpleNamespace(content="", embeds=[])
    )
    cog._run_request = AsyncMock(return_value=answer())
    await cog._process(ctx, "is this true?")
    cog._run_request.assert_not_awaited()
    assert "text" in ctx.send.call_args.args[0].lower()


@pytest.mark.asyncio
async def test_vague_question_without_reply_requests_claim(cog):
    cog._run_request = AsyncMock(return_value=answer())
    ctx = context()
    await cog._process(ctx, "is this true?")
    cog._run_request.assert_not_awaited()
    assert "claim" in ctx.send.call_args.args[0].lower()


@pytest.mark.asyncio
@pytest.mark.parametrize("has_sources", [False, True])
async def test_verify_search_checks_retrieved_evidence(cog, has_sources):
    result = answer()
    result["searched"] = has_sources
    result["sources"] = (
        [{"title": "Official", "url": "https://example.org"}] if has_sources else []
    )
    cog._run_request = AsyncMock(return_value=result)
    ctx = context(slash=True)
    await GrokCog.admin_verify.callback(cog, ctx, mode="search")
    assert cog._run_request.call_args.args[2] is True
    assert ("Search is working" in ctx.send.call_args.args[0]) is has_sources


@pytest.mark.asyncio
async def test_weapon_ranking_includes_footer_assumptions_and_all_fields(cog):
    embed = discord.Embed(title="Weapon ranking | MED 100 | Prof 6")
    for index in range(15):
        embed.add_field(
            name=f"{index + 1}. Soulwrought Longsword (Sword | Crazy Slots)",
            value="DPS: 91.68 | M1: 43.24",
        )
    embed.set_footer(
        text="DPS includes listed Endlag; bleed, crits, procs, enchants, talents, PEN, and resistance are excluded."
    )
    ctx = context()
    ctx.message.reference = SimpleNamespace(
        resolved=SimpleNamespace(content="", embeds=[embed])
    )
    cog._run_request = AsyncMock(return_value=answer())
    await cog._process(ctx, "is this true?")
    query = cog._run_request.call_args.args[0]
    assert "Weapon ranking | MED 100 | Prof 6" in query
    assert "15. Soulwrought" in query
    assert "DPS includes listed Endlag" in query
    assert "resistance are excluded" in query


@pytest.mark.asyncio
async def test_actual_weapon_ranking_description_reaches_search(cog):
    rows = [
        ("Soulwrought Longsword (Sword | Crazy Slots)", "91.68", "43.24"),
        ("Soulwrought Spear (Spear | Crazy Slots)", "81.12", "43.61"),
        ("Ritual Sacrifice (Spear)", "77.84", "43.24"),
        ("Kyrsblade (Sword)", "71.52", "35.76"),
        ("Alloyed Vigil Longsword (Sword)", "71.28", "35.64"),
        ("Nocturne (Sword)", "71.09", "34.85"),
        ("Alloyed Cavalry Saber (Sword)", "70.80", "35.40"),
        ("Palace Tachi (Sword)", "70.21", "37.75"),
        ("Eye of Malice (Sword)", "69.69", "36.68"),
        ("Alloyed Shotel (Sword)", "69.38", "35.76"),
        ("Serpent's Edge (Sword)", "69.38", "35.76"),
        ("Alloyed Katana (Sword)", "69.19", "34.59"),
        ("Phantomcleave (Sword)", "69.19", "34.59"),
        ("Shattered Katana (Sword)", "69.19", "34.59"),
        ("Alloyed Officer Saber (Sword)", "69.02", "34.51"),
    ]
    description = "\n".join(
        f"{i}. {name}\nDPS: {dps} | M1: {m1}"
        for i, (name, dps, m1) in enumerate(rows, 1)
    )
    footer = (
        "Showing 1-15 of 73. Numeric stat requirements are enforced; non-stat unlock alternatives are not modeled. "
        "Crazy Slots and qualifying hybrids are included. DPS includes listed Endlag; bleed, crits, procs, enchants, talents, PEN, and resistance are excluded."
    )
    embed = discord.Embed(
        title="Weapon ranking | MED 100 | Prof 6", description=description
    )
    embed.set_footer(text=footer)
    ctx = context()
    ctx.message.reference = SimpleNamespace(
        resolved=SimpleNamespace(content="", embeds=[embed])
    )
    cog._run_request = AsyncMock(return_value=answer())
    await cog._process(ctx, "is this true?")
    query = cog._run_request.call_args.args[0]
    quoted = json.loads(query.split("\n\nUser question:", 1)[0].split("\n", 1)[1])
    assert description in quoted and footer in quoted
    assert cog._run_request.call_args.args[2] is True
