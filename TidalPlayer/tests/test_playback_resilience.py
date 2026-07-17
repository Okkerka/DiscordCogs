"""Regression coverage for TidalPlayer playback failure handling."""

from __future__ import annotations

import asyncio
import importlib
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.mark.asyncio
async def test_load_lavalink_track_does_not_retry_timeout_with_a_fresh_url(cog) -> None:
    player = SimpleNamespace(
        load_tracks=AsyncMock(side_effect=TimeoutError())
    )
    tidal_track = SimpleNamespace(id=460908504)
    with patch.object(
        type(cog.tidal),
        "get_stream_url",
        new=AsyncMock(return_value="https://stream/first"),
    ) as get_stream_url:
        result = await cog._load_lavalink_track(player, tidal_track, guild_id=1)

    assert result is None
    assert player.load_tracks.await_args_list[0].args == ("https://stream/first",)
    assert player.load_tracks.await_count == 1
    assert get_stream_url.await_count == 1


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
    cog._schedule_controller_recommendations = MagicMock()

    with patch.object(
        type(cog), "_resend_controller_for_track_start", new=AsyncMock()
    ) as resend_controller:
        await cog.on_red_audio_track_start(
            guild,
            SimpleNamespace(title="Next Track", author="Next Artist - Next Album"),
            requester=SimpleNamespace(),
        )

    assert cog._current_meta[guild.id] == next_meta
    assert not cog._queued_meta[guild.id]
    resend_controller.assert_awaited_once_with(guild_id=guild.id)
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


@pytest.mark.asyncio
async def test_get_url_is_preferred_without_calling_get_stream(cog) -> None:
    class Track:
        id = 530850206

        def __init__(self) -> None:
            self.get_stream_calls = 0

        def get_url(self) -> str:
            return "https://stream/legacy"

        def get_stream(self):
            self.get_stream_calls += 1
            raise AssertionError("get_stream must not be called when get_url works")

    track = Track()
    with patch.object(type(cog.tidal), "get_track", new=AsyncMock(return_value=track)):
        assert await cog.tidal.get_stream_url(track) == "https://stream/legacy"

    assert track.get_stream_calls == 0


@pytest.mark.asyncio
async def test_lavalink_load_logs_elapsed_time_without_the_signed_url(cog, caplog) -> None:
    caplog.set_level(logging.INFO, logger="red.tidalplayer")
    signed_url = "https://stream.example/signed-secret"
    loaded_track = SimpleNamespace()
    player = SimpleNamespace(load_tracks=AsyncMock(return_value=SimpleNamespace(tracks=[loaded_track])))

    assert await cog._load_lavalink_track(
        player,
        SimpleNamespace(id=530850206),
        guild_id=1,
        initial_stream_url=signed_url,
    ) is loaded_track

    assert "Lavalink loaded Tidal track 530850206 in" in caplog.text
    assert signed_url not in caplog.text


@pytest.mark.asyncio
async def test_slow_lavalink_load_warns_without_cancelling_the_request(cog, caplog, monkeypatch) -> None:
    module = importlib.import_module(cog.__class__.__module__)
    monkeypatch.setattr(module, "LAVALINK_SLOW_LOAD_WARNING_DELAY", 0.0)
    caplog.set_level(logging.WARNING, logger="red.tidalplayer")
    started = asyncio.Event()
    release = asyncio.Event()
    loaded_track = SimpleNamespace()

    async def load_tracks(_url):
        started.set()
        await release.wait()
        return SimpleNamespace(tracks=[loaded_track])

    task = asyncio.create_task(
        cog._load_lavalink_track(
            SimpleNamespace(load_tracks=load_tracks),
            SimpleNamespace(id=530850206),
            guild_id=1,
            initial_stream_url="https://stream.example/signed-secret",
        )
    )
    await started.wait()
    await asyncio.sleep(0)
    assert "Lavalink is still loading Tidal track 530850206" in caplog.text

    release.set()
    assert await task is loaded_track


@pytest.mark.asyncio
async def test_lavalink_load_stops_after_the_hard_safety_deadline(cog, caplog, monkeypatch) -> None:
    module = importlib.import_module(cog.__class__.__module__)
    monkeypatch.setattr(module, "LAVALINK_SLOW_LOAD_WARNING_DELAY", 0.0)
    monkeypatch.setattr(module, "LAVALINK_LOAD_HARD_TIMEOUT", 0.0)
    caplog.set_level(logging.WARNING, logger="red.tidalplayer")
    started = asyncio.Event()

    async def load_tracks(_url):
        started.set()
        await asyncio.Event().wait()

    assert await cog._load_lavalink_track(
        SimpleNamespace(load_tracks=load_tracks),
        SimpleNamespace(id=530850206),
        guild_id=1,
        initial_stream_url="https://stream.example/signed-secret",
    ) is None
    assert started.is_set()
    assert "Lavalink timed out loading Tidal track 530850206" in caplog.text


@pytest.mark.asyncio
async def test_lavalink_no_tracks_logs_the_safe_result_type(cog, caplog) -> None:
    caplog.set_level(logging.WARNING, logger="red.tidalplayer")
    signed_url = "https://stream.example/signed-secret"
    player = SimpleNamespace(
        load_tracks=AsyncMock(return_value=SimpleNamespace(tracks=[], load_type="LOAD_FAILED"))
    )

    assert await cog._load_lavalink_track(
        player,
        SimpleNamespace(id=530850206),
        guild_id=1,
        initial_stream_url=signed_url,
    ) is None

    assert "load_type=LOAD_FAILED" in caplog.text
    assert signed_url not in caplog.text
