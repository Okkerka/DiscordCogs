import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


@pytest.mark.asyncio
async def test_concurrent_messages_only_post_once(random_cog, message):
    group = random_cog.config.guild(message.guild)
    await group.enabled.set(True)
    await group.target.set(2)
    random_cog.get_brainrot = AsyncMock(return_value="A generated joke")
    await group.set({**await group.all(), "categories": ["brainrot"]})
    await asyncio.gather(*(random_cog.on_message(message) for _ in range(2)))
    assert message.channel.send.await_count == 1
    assert (await group.all())["counter"] == 0
    await random_cog.cog_unload()


@pytest.mark.asyncio
async def test_excluded_channel_does_not_count(random_cog, message):
    group = random_cog.config.guild(message.guild)
    await group.set({**await group.all(), "enabled": True, "channels": [20]})
    await random_cog.on_message(message)
    assert (await group.all())["counter"] == 0
    await random_cog.cog_unload()


@pytest.mark.asyncio
async def test_frequency_survives_each_post(random_cog, message):
    await random_cog.update_settings(
        1,
        frequency_min=7,
        frequency_max=7,
        enabled=True,
        target=1,
        categories=["brainrot"],
    )
    random_cog.get_brainrot = AsyncMock(return_value="A generated joke")
    await random_cog.on_message(message)
    assert (await random_cog.config.guild(message.guild).all())["target"] == 7
    await random_cog.cog_unload()


@pytest.mark.asyncio
async def test_failed_category_never_falls_back_to_disabled_brainrot(random_cog):
    random_cog.get_fact = AsyncMock(return_value=None)
    random_cog.get_brainrot = AsyncMock(return_value="disabled content")
    assert await random_cog.generate_text(["fact"]) is None
    random_cog.get_brainrot.assert_not_awaited()
    await random_cog.cog_unload()


@pytest.mark.asyncio
async def test_invalid_settings_leave_existing_config(random_cog):
    before = await random_cog.config.guild_from_id(1).all()
    for changes in (
        {"categories": []},
        {"frequency_min": 100, "frequency_max": 10},
        {"categories": ["unknown"]},
        {"channels": [-1]},
    ):
        with pytest.raises(ValueError):
            await random_cog.update_settings(1, **changes)
        assert await random_cog.config.guild_from_id(1).all() == before
    await random_cog.cog_unload()


@pytest.mark.asyncio
async def test_output_bounds_and_mentions(random_cog, message):
    await random_cog.send_split_message(message.channel, "@everyone " + "x" * 20000)
    assert message.channel.send.await_count <= 3
    for call in message.channel.send.await_args_list:
        assert call.kwargs["allowed_mentions"].everyone is False
        assert len(call.kwargs["embed"].description) <= 4096
    await random_cog.cog_unload()


@pytest.mark.asyncio
async def test_simultaneous_cache_misses_do_one_config_read(random_cog, monkeypatch):
    group = random_cog.config.guild_from_id(1)
    read = AsyncMock(wraps=group.all)
    group.all = read
    monkeypatch.setattr(random_cog.config, "guild_from_id", lambda _: group)
    await asyncio.gather(*(random_cog.get_settings(1) for _ in range(10)))
    assert read.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["categories", "channels", "toggle"])
async def test_concurrent_settings_commands_do_not_lose_changes(random_cog, operation):
    await random_cog.get_settings(1)
    ctx = SimpleNamespace(guild=SimpleNamespace(id=1), send=AsyncMock())
    await random_cog._locks[1].acquire()
    if operation == "categories":
        calls = [
            random_cog.category.callback(random_cog, ctx, category, False)
            for category in ("brainrot", "fact")
        ]
    elif operation == "channels":
        calls = [
            random_cog.channel.callback(random_cog, ctx, "add", SimpleNamespace(id=cid))
            for cid in (20, 30)
        ]
    else:
        calls = [random_cog.toggle.callback(random_cog, ctx) for _ in range(2)]
    tasks = [asyncio.create_task(call) for call in calls]
    try:
        # Both commands start while another persistence operation holds the lock.
        await asyncio.sleep(0)
    finally:
        random_cog._locks[1].release()
    await asyncio.gather(*tasks)
    settings = await random_cog.get_settings(1)
    if operation == "categories":
        assert settings["categories"] == ["showerthought", "dadjoke"]
    elif operation == "channels":
        assert set(settings["channels"]) == {20, 30}
    else:
        assert settings["enabled"] is False
