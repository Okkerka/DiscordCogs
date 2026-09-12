import asyncio
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

import chattriggers.chattriggers as module


@pytest.mark.asyncio
async def test_rename_cannot_overwrite_another_trigger(trigger_cog):
    await trigger_cog.save_trigger(1, "first", {"title": "First"})
    await trigger_cog.save_trigger(1, "second", {"title": "Second"})
    before = await trigger_cog.get_settings(1)
    with pytest.raises(ValueError):
        await trigger_cog.save_trigger(
            1, "second", {"title": "Changed"}, old_key="first"
        )
    assert await trigger_cog.get_settings(1) == before


@pytest.mark.asyncio
async def test_edit_preserves_disabled_state_and_audio_choice(trigger_cog):
    await trigger_cog.save_trigger(
        1, "alarm", {"title": "Before", "audio_mode": "interrupt"}
    )
    await trigger_cog.update_trigger(
        1, "alarm", active=False, cooldown=60, channels=[10]
    )
    await trigger_cog.save_trigger(1, "new alarm", {"title": "After"}, old_key="alarm")
    data = (await trigger_cog.get_settings(1))["triggers"]["new alarm"]
    assert data["active"] is False
    assert data["audio_mode"] == "interrupt"
    assert data["cooldown"] == 60
    assert data["channels"] == [10]


@pytest.mark.asyncio
async def test_stale_edit_rejected(trigger_cog):
    await trigger_cog.save_trigger(1, "alarm", {"title": "Before"})
    snapshot = deepcopy((await trigger_cog.get_settings(1))["triggers"]["alarm"])
    await trigger_cog.update_trigger(1, "alarm", active=False)
    with pytest.raises(ValueError):
        await trigger_cog.save_trigger(
            1, "alarm", {"title": "Stale"}, old_key="alarm", expected=snapshot
        )
    assert (await trigger_cog.get_settings(1))["triggers"]["alarm"]["title"] == "Before"


@pytest.mark.asyncio
async def test_cooldown_admitted_atomically(trigger_cog, message):
    await trigger_cog.save_trigger(1, "hello", {"title": "Alert", "cooldown": 60})
    await trigger_cog.change_permission(1, "allowed_users", 2, True)
    trigger_cog.play_trigger = AsyncMock(return_value="shown")
    await asyncio.gather(*(trigger_cog.on_message(message) for _ in range(5)))
    assert trigger_cog.play_trigger.await_count == 1


@pytest.mark.asyncio
async def test_channel_filter_and_authorization(trigger_cog, message):
    await trigger_cog.save_trigger(1, "hello", {"title": "Alert", "channels": [20]})
    trigger_cog.play_trigger = AsyncMock()
    await trigger_cog.on_message(message)
    await trigger_cog.change_permission(1, "allowed_users", 2, True)
    await trigger_cog.on_message(message)
    trigger_cog.play_trigger.assert_not_awaited()


@pytest.mark.asyncio
async def test_long_message_still_matches(trigger_cog, message):
    await trigger_cog.save_trigger(1, "hello", {"title": "Alert"})
    await trigger_cog.change_permission(1, "allowed_users", 2, True)
    message.content = "hello" + " text" * 200
    trigger_cog.play_trigger = AsyncMock()
    await trigger_cog.on_message(message)
    assert trigger_cog.play_trigger.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,stops", [("skip", 0), ("interrupt", 1)])
async def test_audio_choice_controls_music_interruption(
    trigger_cog, message, monkeypatch, mode, stops
):
    track = object()
    player = SimpleNamespace(
        channel=SimpleNamespace(id=30),
        current=object(),
        is_playing=True,
        queue=[object()],
        load_tracks=AsyncMock(return_value=SimpleNamespace(tracks=[track])),
        stop=AsyncMock(),
        play=AsyncMock(),
        add=Mock(),
        move_to=AsyncMock(),
    )
    monkeypatch.setattr(module, "LAVALINK_AVAILABLE", True)
    monkeypatch.setattr(module.lavalink, "get_player", lambda _: player)
    message.author.voice = SimpleNamespace(
        channel=SimpleNamespace(
            id=30, permissions_for=lambda _: SimpleNamespace(connect=True, speak=True)
        )
    )
    await trigger_cog.play_trigger(
        message.channel,
        message.author,
        {
            "sound": "https://www.youtube.com/watch?v=abc",
            "title": "Alert",
            "audio_mode": mode,
        },
    )
    assert player.stop.await_count == stops
    assert len(player.queue) == (0 if stops else 1)
    assert message.channel.send.await_count == 1


@pytest.mark.asyncio
async def test_unplayable_sound_does_not_stop_music(trigger_cog, message, monkeypatch):
    player = SimpleNamespace(
        channel=SimpleNamespace(id=30),
        current=object(),
        is_playing=True,
        queue=[object()],
        load_tracks=AsyncMock(return_value=SimpleNamespace(tracks=[])),
        stop=AsyncMock(),
    )
    monkeypatch.setattr(module, "LAVALINK_AVAILABLE", True)
    monkeypatch.setattr(module.lavalink, "get_player", lambda _: player)
    message.author.voice = SimpleNamespace(
        channel=SimpleNamespace(
            id=30, permissions_for=lambda _: SimpleNamespace(connect=True, speak=True)
        )
    )
    await trigger_cog.play_trigger(
        message.channel,
        message.author,
        {
            "sound": "https://www.youtube.com/watch?v=abc",
            "title": "Alert",
            "audio_mode": "interrupt",
        },
    )
    player.stop.assert_not_awaited()
    assert len(player.queue) == 1


@pytest.mark.asyncio
async def test_legacy_trigger_retains_interrupt_mode(trigger_cog):
    group = trigger_cog.config.guild_from_id(1)
    await group.set(
        {
            **await group.all(),
            "triggers": {"old": {"phrase_case": "Old", "title": "Old"}},
        }
    )
    assert (await trigger_cog.get_settings(1))["triggers"]["old"][
        "audio_mode"
    ] == "interrupt"


@pytest.mark.asyncio
async def test_user_data_deletion_removes_permission_entries(trigger_cog):
    await trigger_cog.change_permission(1, "allowed_users", 2, True)
    await trigger_cog.change_permission(1, "admin_users", 2, True)
    await trigger_cog.red_delete_data_for_user(
        requester="discord_deleted_user", user_id=2
    )
    settings = await trigger_cog.get_settings(1)
    assert settings["allowed_users"] == settings["admin_users"] == []


@pytest.mark.asyncio
async def test_edit_does_not_change_first_match_priority(trigger_cog):
    await trigger_cog.save_trigger(1, "hello", {"title": "First"})
    await trigger_cog.save_trigger(1, "hello there", {"title": "Second"})
    await trigger_cog.save_trigger(1, "hello", {"title": "Edited"}, old_key="hello")
    assert list((await trigger_cog.get_settings(1))["triggers"]) == [
        "hello",
        "hello there",
    ]


@pytest.mark.asyncio
async def test_first_use_connects_when_lavalink_has_no_player(
    trigger_cog, message, monkeypatch
):
    player = SimpleNamespace(
        channel=SimpleNamespace(id=30),
        current=None,
        is_playing=False,
        queue=[],
        load_tracks=AsyncMock(return_value=SimpleNamespace(tracks=[object()])),
        add=Mock(),
        play=AsyncMock(),
    )
    monkeypatch.setattr(
        module.lavalink,
        "get_player",
        Mock(side_effect=module.lavalink.PlayerNotFound()),
    )
    connect = AsyncMock(return_value=player)
    monkeypatch.setattr(module.lavalink, "connect", connect)
    message.author.voice = SimpleNamespace(
        channel=SimpleNamespace(
            id=30, permissions_for=lambda _: SimpleNamespace(connect=True, speak=True)
        )
    )
    result = await trigger_cog.play_trigger(
        message.channel,
        message.author,
        {"sound": "https://www.youtube.com/watch?v=abc", "audio_mode": "skip"},
    )
    assert result == "Sound played."
    assert connect.await_count == 1


@pytest.mark.asyncio
async def test_interrupt_stops_paused_track_before_replacing(
    trigger_cog, message, monkeypatch
):
    player = SimpleNamespace(
        channel=SimpleNamespace(id=30),
        current=object(),
        is_playing=False,
        queue=[],
        load_tracks=AsyncMock(return_value=SimpleNamespace(tracks=[object()])),
        stop=AsyncMock(),
        add=Mock(),
        play=AsyncMock(),
    )
    monkeypatch.setattr(module.lavalink, "get_player", lambda _: player)
    message.author.voice = SimpleNamespace(
        channel=SimpleNamespace(
            id=30, permissions_for=lambda _: SimpleNamespace(connect=True, speak=True)
        )
    )
    await trigger_cog.play_trigger(
        message.channel,
        message.author,
        {"sound": "https://www.youtube.com/watch?v=abc", "audio_mode": "interrupt"},
    )
    assert player.stop.await_count == 1


@pytest.mark.asyncio
async def test_simultaneous_cache_misses_do_one_config_read(trigger_cog, monkeypatch):
    group = trigger_cog.config.guild_from_id(1)
    read = AsyncMock(wraps=group.all)
    group.all = read
    monkeypatch.setattr(trigger_cog.config, "guild_from_id", lambda _: group)
    await asyncio.gather(*(trigger_cog.get_settings(1) for _ in range(10)))
    assert read.await_count == 1
