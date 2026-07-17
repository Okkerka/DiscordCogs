"""Regression coverage for TidalPlayer playback failure handling."""

from __future__ import annotations

import asyncio
import importlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.mark.asyncio
async def test_load_lavalink_track_retries_timeout_with_fresh_tidal_url(cog, monkeypatch) -> None:
    module = importlib.import_module(cog.__class__.__module__)
    monkeypatch.setattr(module, "LAVALINK_LOAD_RETRY_DELAY", 0)

    loaded_track = SimpleNamespace()
    player = SimpleNamespace(
        load_tracks=AsyncMock(
            side_effect=[
                TimeoutError(),
                SimpleNamespace(tracks=[loaded_track]),
            ]
        )
    )
    tidal_track = SimpleNamespace(id=460908504)
    with patch.object(
        type(cog.tidal),
        "get_stream_url",
        new=AsyncMock(side_effect=["https://stream/first", "https://stream/fresh"]),
    ) as get_stream_url:
        result = await cog._load_lavalink_track(player, tidal_track, guild_id=1)

    assert result is loaded_track
    assert player.load_tracks.await_args_list[0].args == ("https://stream/first",)
    assert player.load_tracks.await_args_list[1].args == ("https://stream/fresh",)
    assert get_stream_url.await_count == 2


@pytest.mark.asyncio
async def test_unverifiable_playlist_owner_is_not_returned_for_write(cog) -> None:
    playlist = SimpleNamespace(creator=None)
    with patch.object(type(cog.tidal), "get_playlist", new=AsyncMock(return_value=playlist)):
        assert await cog.tidal.get_user_playlist_by_id("playlist-id") is None


@pytest.mark.asyncio
async def test_track_start_advances_queued_tidal_metadata(cog) -> None:
    guild = SimpleNamespace(id=23)
    next_meta = {
        "title": "Next Track",
        "artist": "Next Artist",
        "album": "Next Album",
        "duration": 120,
        "quality": "LOSSLESS",
        "image": None,
        "share_url": None,
        "audio_resolution": None,
        "track_id": 337293380,
    }
    cog._queued_meta[guild.id].append(next_meta)
    cog._refresh_controller = AsyncMock()
    cog._schedule_controller_recommendations = MagicMock()

    await cog.on_red_audio_track_start(
        guild,
        SimpleNamespace(title="Next Track", author="Next Artist - Next Album"),
        requester=SimpleNamespace(),
    )

    assert cog._current_meta[guild.id] == next_meta
    assert not cog._queued_meta[guild.id]
    cog._refresh_controller.assert_awaited_once_with(guild.id)
    cog._schedule_controller_recommendations.assert_called_once_with(guild.id)


@pytest.mark.asyncio
async def test_load_lavalink_track_waits_for_a_reconnecting_node(cog, monkeypatch) -> None:
    module = importlib.import_module(cog.__class__.__module__)
    monkeypatch.setattr(module, "LAVALINK_NODE_READY_RETRY_DELAY", 0)

    loaded_track = SimpleNamespace()
    player = SimpleNamespace(
        load_tracks=AsyncMock(
            side_effect=[
                RuntimeError("Cannot execute REST request when node not ready."),
                SimpleNamespace(tracks=[loaded_track]),
            ]
        )
    )

    result = await cog._load_lavalink_track(
        player,
        SimpleNamespace(id=530850206),
        guild_id=1,
        initial_stream_url="https://stream/current",
    )

    assert result is loaded_track
    assert player.load_tracks.await_count == 2
    assert player.load_tracks.await_args_list[0].args == ("https://stream/current",)
    assert player.load_tracks.await_args_list[1].args == ("https://stream/current",)


@pytest.mark.asyncio
async def test_duplicate_guild_track_loads_share_one_lavalink_request(cog) -> None:
    loaded_track = SimpleNamespace()
    calls = 0

    async def load_tracks(_url):
        nonlocal calls
        calls += 1
        await asyncio.sleep(0)
        return SimpleNamespace(tracks=[loaded_track])

    player = SimpleNamespace(load_tracks=load_tracks)
    tidal_track = SimpleNamespace(id=530850206)

    first, second = await asyncio.gather(
        cog._load_lavalink_track(
            player, tidal_track, guild_id=1, initial_stream_url="https://stream/current"
        ),
        cog._load_lavalink_track(
            player, tidal_track, guild_id=1, initial_stream_url="https://stream/current"
        ),
    )

    assert first is loaded_track
    assert second is loaded_track
    assert calls == 1
