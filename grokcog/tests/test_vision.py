from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import discord
import pytest

from grokcog.grokcog import GrokCog
from grokcog.helpers import ProviderError
from grokcog.tests.test_requests import Config, answer, context
from grokcog.vision import ImageCollector


def attachment(index=1, **overrides):
    fields = {
        "url": f"https://cdn.discordapp.com/attachments/10/{index}/image.png?ex=test",
        "filename": "image.png",
        "content_type": "image/png",
        "size": 1000,
    }
    fields.update(overrides)
    return SimpleNamespace(**fields)


@pytest.fixture
def cog():
    config = Config()
    with patch("grokcog.grokcog.Config.get_conf", return_value=config):
        result = GrokCog(Mock())
    result._ready.set()
    config.data.update(
        api_key="test-key",
        min_api_call_gap=0,
        cooldown_seconds=0,
        model_name="qwen/qwen3.8-27b",
    )
    return result


@pytest.mark.asyncio
async def test_images_in_own_and_replied_messages_reach_request(cog):
    ctx = context()
    ctx.message.attachments = [attachment(1)]
    ctx.message.reference = SimpleNamespace(
        resolved=SimpleNamespace(content="", embeds=[], attachments=[attachment(2)])
    )
    cog._run_request = AsyncMock(return_value=answer())
    await cog._process(ctx, "is this true?")
    kwargs = cog._run_request.call_args.kwargs
    assert len(kwargs["images"]) == 2
    assert cog._run_request.call_args.args[3] == "qwen/qwen3.8-27b"


@pytest.mark.asyncio
async def test_slash_image_option_is_registered_and_passed(cog):
    command = cog.get_commands()[0].app_command.get_command("ask")
    assert (
        command.get_parameter("image").type == discord.AppCommandOptionType.attachment
    )
    ctx = context(slash=True)
    image = attachment()
    cog._process = AsyncMock()
    await GrokCog.grok.callback(cog, ctx, question="What does this say?", image=image)
    cog._process.assert_awaited_once_with(ctx, "What does this say?", image=image)


@pytest.mark.asyncio
async def test_vision_payload_has_actual_image_input(cog):
    cog._request_json = AsyncMock(
        return_value={"choices": [{"message": {"content": "A screenshot."}}]}
    )
    url = attachment().url
    await cog._ask_groq("Describe this", 0.3, model="qwen/qwen3.8-27b", images=[url])
    payload = cog._request_json.call_args.args[2]
    content = payload["messages"][1]["content"]
    assert content[0] == {"type": "text", "text": "Describe this"}
    assert content[1] == {"type": "image_url", "image_url": {"url": url}}
    assert (
        "cannot inspect attachments or images in this cog"
        not in payload["messages"][0]["content"]
    )


@pytest.mark.asyncio
async def test_image_fact_check_reads_before_search(cog):
    sourced = answer("Checked the claim")
    sourced.update(
        searched=True, sources=[{"title": "Source", "url": "https://example.org"}]
    )
    cog._ask_groq = AsyncMock(
        side_effect=[answer("The screenshot says the moon is cheese."), sourced]
    )
    result = await cog._run_request(
        "Is this true?", 0.3, True, "qwen/qwen3.8-27b", images=[attachment().url]
    )
    first, second = cog._ask_groq.call_args_list
    assert first.kwargs["images"] == [attachment().url]
    assert first.kwargs["transcribe"]
    assert second.kwargs["search"]
    assert "The screenshot says the moon is cheese." in second.args[0]
    assert not second.kwargs.get("images")
    assert result["sources"]


@pytest.mark.asyncio
async def test_text_model_does_not_silently_ignore_images(cog):
    cog._request_json = AsyncMock()
    with pytest.raises(ProviderError, match="vision|image"):
        await cog._ask_groq(
            "What is this?", 0.3, model="openai/gpt-oss-120b", images=[attachment().url]
        )
    cog._request_json.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/image.png",
        "https://cdn.discordapp.com.evil.test/image.png",
        "https://user:pass@cdn.discordapp.com/attachments/a.png",
    ],
)
async def test_untrusted_image_hosts_rejected(cog, url):
    ctx = context()
    ctx.message.attachments = [attachment(url=url)]
    cog._run_request = AsyncMock()
    await cog._process(ctx, "Read this")
    cog._run_request.assert_not_awaited()
    assert ctx.send.await_count


@pytest.mark.asyncio
async def test_too_many_images_rejected_before_provider(cog):
    ctx = context()
    ctx.message.attachments = [attachment(i) for i in range(4)]
    cog._run_request = AsyncMock()
    await cog._process(ctx, "Read this")
    cog._run_request.assert_not_awaited()
    assert "3" in ctx.send.call_args.args[0]


@pytest.mark.asyncio
async def test_different_images_do_not_share_cached_answer(cog):
    cog._run_request = AsyncMock(return_value=answer())
    ctx = context()
    ctx.message.attachments = [attachment(1)]
    await cog._process(ctx, "Read this")
    ctx.message.attachments = [attachment(2)]
    await cog._process(ctx, "Read this")
    assert cog._run_request.await_count == 2


def test_attachment_duplicate_keeps_signed_url_and_counts_once():
    collector = ImageCollector()
    item = attachment()
    collector.add_attachment(item)
    collector.add_attachment(item)
    assert collector.urls == [item.url]
    assert collector.known_bytes == item.size


@pytest.mark.parametrize("size", [-1, 8 * 1024 * 1024 + 1])
def test_invalid_attachment_sizes_rejected(size):
    with pytest.raises(ProviderError, match="8 MB"):
        ImageCollector().add_attachment(attachment(size=size))


def test_combined_attachment_budget_is_enforced():
    collector = ImageCollector()
    for index in range(2):
        collector.add_attachment(attachment(index, size=8 * 1024 * 1024))
    with pytest.raises(ProviderError, match="16 MB"):
        collector.add_attachment(attachment(3))


def test_embed_images_use_discord_proxy_not_external_origin():
    embed = discord.Embed.from_dict(
        {
            "image": {
                "url": "https://example.org/image.png",
                "proxy_url": "https://images-ext-1.discordapp.net/external/hash/https/example.org/image.png",
            }
        }
    )
    collector = ImageCollector()
    collector.add_message(SimpleNamespace(attachments=[], embeds=[embed]))
    assert collector.urls == [embed.image.proxy_url]


def test_explicit_non_image_attachment_rejected():
    with pytest.raises(ProviderError, match="PNG"):
        ImageCollector().add_attachment(
            attachment(filename="file.pdf", content_type="application/pdf"),
            explicit=True,
        )


@pytest.mark.asyncio
async def test_prefix_question_parser_preserves_text_with_attachments(cog):
    from discord.ext.commands.view import StringView

    ctx = context()
    ctx.interaction = None
    ctx.message.attachments = [attachment()]
    ctx.view = StringView("is this true?")
    command = cog.get_commands()[0]
    await command._parse_arguments(ctx)
    assert ctx.kwargs["question"] == "is this true?"


@pytest.mark.asyncio
async def test_vision_failure_does_not_search_invented_observations(cog):
    cog._ask_groq = AsyncMock(side_effect=ProviderError("Image unavailable"))
    with pytest.raises(ProviderError, match="Image unavailable"):
        await cog._run_request(
            "is this true?", 0.3, True, "qwen/qwen3.8-27b", images=[attachment().url]
        )
    assert cog._ask_groq.await_count == 1
    assert not cog._operations
