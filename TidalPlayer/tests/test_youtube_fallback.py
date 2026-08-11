"""Regression coverage for Tidal-first single-video YouTube playback."""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from TidalPlayer.ui.embeds import Messages


VIDEO_ID = "dQw4w9WgXcQ"
CANONICAL_URL = f"https://www.youtube.com/watch?v={VIDEO_ID}"


def _context(guild_id: int = 71):
    channel = SimpleNamespace(id=12)
    return SimpleNamespace(
        guild=SimpleNamespace(id=guild_id),
        author=SimpleNamespace(voice=SimpleNamespace(channel=channel)),
        channel=channel,
        send=AsyncMock(),
    )


def _candidate(title: str = "Rivals", artist: str = "AZALI"):
    return SimpleNamespace(
        id=269931027,
        name=title,
        full_name=title,
        artist=SimpleNamespace(name=artist),
    )


def _loaded_track(
    *,
    title: str = "AZALI - Rivals",
    author: str = "AZALI",
    length: int = 243_000,
    thumbnail: str | None = "https://i.ytimg.com/vi/dQw4w9WgXcQ/hqdefault.jpg",
):
    return SimpleNamespace(
        title=title,
        author=author,
        length=length,
        thumbnail=thumbnail,
        uri=CANONICAL_URL,
    )


def _player(load_result):
    return SimpleNamespace(
        current=None,
        queue=[],
        add=MagicMock(),
        play=AsyncMock(),
        load_tracks=AsyncMock(return_value=load_result),
    )


def _youtube_client(payload):
    request = SimpleNamespace(execute=lambda: payload)
    videos = SimpleNamespace(list=lambda **_kwargs: request)
    return SimpleNamespace(videos=lambda: videos)


def _api_payload(
    *, title: str = "AZALI - Rivals (Official Audio)", channel: str = "AZALI - Topic"
):
    return {
        "items": [
            {
                "snippet": {
                    "title": title,
                    "channelTitle": channel,
                    "thumbnails": {
                        "high": {"url": "https://i.ytimg.com/vi/dQw4w9WgXcQ/hqdefault.jpg"}
                    },
                }
            }
        ]
    }


async def _run_blocking(_handler, operation, **_kwargs):
    return operation()


@pytest.mark.asyncio
async def test_confident_tidal_match_avoids_youtube_lavalink_load(cog) -> None:
    ctx = _context()
    candidate = _candidate()
    cog.yt = _youtube_client(_api_payload())
    queue_tidal = AsyncMock(return_value=True)
    get_player = AsyncMock(side_effect=AssertionError("YouTube player must not be prepared"))

    with (
        patch.object(type(cog.tidal), "_run_blocking", new=_run_blocking),
        patch.object(type(cog.tidal), "search", new=AsyncMock(return_value=[candidate])),
        patch.object(type(cog), "_load_and_queue_track", new=queue_tidal),
        patch.object(type(cog), "_get_player", new=get_player),
    ):
        await cog._handle_youtube_video(ctx, VIDEO_ID)

    queue_tidal.assert_awaited_once_with(ctx, candidate)
    get_player.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_confident_match_loads_and_admits_youtube_once(cog) -> None:
    ctx = _context()
    loaded = _loaded_track(thumbnail=None)
    player = _player(SimpleNamespace(tracks=[loaded], load_type="TRACK_LOADED"))
    cog.yt = _youtube_client(_api_payload())

    with (
        patch.object(type(cog.tidal), "_run_blocking", new=_run_blocking),
        patch.object(type(cog.tidal), "search", new=AsyncMock(return_value=[])),
        patch.object(type(cog), "_get_player", new=AsyncMock(return_value=player)),
        patch.object(type(cog), "_ensure_vc_connected", new=AsyncMock(return_value=player)),
        patch.object(type(cog), "_send_now_playing", new=AsyncMock()),
    ):
        await cog._handle_youtube_video(ctx, VIDEO_ID)

    player.load_tracks.assert_awaited_once_with(CANONICAL_URL)
    player.add.assert_called_once_with(ctx.author, loaded)
    player.play.assert_awaited_once()
    meta = cog._current_meta[ctx.guild.id]
    assert meta["source"] == "YouTube"
    assert meta["duration"] == 243
    assert meta["share_url"] == CANONICAL_URL
    assert meta["image"].endswith("hqdefault.jpg")


@pytest.mark.asyncio
async def test_missing_api_metadata_uses_loaded_metadata_then_prefers_tidal(cog) -> None:
    ctx = _context()
    loaded = _loaded_track()
    player = _player(SimpleNamespace(tracks=[loaded], load_type="TRACK_LOADED"))
    candidate = _candidate()
    cog.yt = _youtube_client({"items": [{}]})
    queue_tidal = AsyncMock(return_value=True)

    with (
        patch.object(type(cog.tidal), "_run_blocking", new=_run_blocking),
        patch.object(type(cog.tidal), "search", new=AsyncMock(return_value=[candidate])),
        patch.object(type(cog), "_get_player", new=AsyncMock(return_value=player)),
        patch.object(type(cog), "_ensure_vc_connected", new=AsyncMock(return_value=player)),
        patch.object(type(cog), "_load_and_queue_track", new=queue_tidal),
    ):
        await cog._handle_youtube_video(ctx, VIDEO_ID)

    player.load_tracks.assert_awaited_once_with(CANONICAL_URL)
    queue_tidal.assert_awaited_once_with(ctx, candidate, player=player)


@pytest.mark.asyncio
async def test_youtube_empty_load_sends_source_specific_error(cog) -> None:
    ctx = _context()
    player = _player(SimpleNamespace(tracks=[], load_type="NO_MATCHES"))
    cog.yt = _youtube_client(_api_payload())

    with (
        patch.object(type(cog.tidal), "_run_blocking", new=_run_blocking),
        patch.object(type(cog.tidal), "search", new=AsyncMock(return_value=[])),
        patch.object(type(cog), "_get_player", new=AsyncMock(return_value=player)),
        patch.object(type(cog), "_ensure_vc_connected", new=AsyncMock(return_value=player)),
    ):
        await cog._handle_youtube_video(ctx, VIDEO_ID)

    player.load_tracks.assert_awaited_once()
    assert ctx.send.await_args.kwargs["embed"].description == Messages.ERROR_YOUTUBE_FAILED


@pytest.mark.asyncio
async def test_youtube_api_cancellation_propagates_without_error_message(cog) -> None:
    ctx = _context()
    cog.yt = _youtube_client(_api_payload())

    async def cancel(_handler, _operation, **_kwargs):
        raise asyncio.CancelledError

    with patch.object(type(cog.tidal), "_run_blocking", new=cancel):
        with pytest.raises(asyncio.CancelledError):
            await cog._handle_youtube_video(ctx, VIDEO_ID)

    ctx.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_youtube_provider_failure_log_excludes_exception_text(cog, caplog) -> None:
    ctx = _context()
    secret = "api-key-and-request-url"
    player = _player(None)
    player.load_tracks.side_effect = RuntimeError(secret)
    cog.yt = _youtube_client(_api_payload())
    caplog.set_level(logging.WARNING, logger="red.tidalplayer")

    with (
        patch.object(type(cog.tidal), "_run_blocking", new=_run_blocking),
        patch.object(type(cog.tidal), "search", new=AsyncMock(return_value=[])),
        patch.object(type(cog), "_get_player", new=AsyncMock(return_value=player)),
        patch.object(type(cog), "_ensure_vc_connected", new=AsyncMock(return_value=player)),
    ):
        await cog._handle_youtube_video(ctx, VIDEO_ID)

    assert secret not in caplog.text
    assert VIDEO_ID in caplog.text
    assert ctx.send.await_args.kwargs["embed"].description == Messages.ERROR_YOUTUBE_FAILED


@pytest.mark.asyncio
async def test_missing_youtube_client_still_uses_direct_fallback(cog) -> None:
    ctx = _context()
    loaded = _loaded_track()
    player = _player(SimpleNamespace(tracks=[loaded], load_type="TRACK_LOADED"))
    cog.yt = None

    with (
        patch.object(type(cog.tidal), "search", new=AsyncMock(return_value=[])),
        patch.object(type(cog), "_get_player", new=AsyncMock(return_value=player)),
        patch.object(type(cog), "_ensure_vc_connected", new=AsyncMock(return_value=player)),
        patch.object(type(cog), "_send_now_playing", new=AsyncMock()),
    ):
        await cog._handle_youtube_video(ctx, VIDEO_ID)

    player.load_tracks.assert_awaited_once_with(CANONICAL_URL)
    assert cog._current_meta[ctx.guild.id]["source"] == "YouTube"


@pytest.mark.asyncio
async def test_youtube_tidal_search_cancellation_propagates(cog) -> None:
    ctx = _context()
    cog.yt = _youtube_client(_api_payload())

    with (
        patch.object(type(cog.tidal), "_run_blocking", new=_run_blocking),
        patch.object(
            type(cog.tidal), "search", new=AsyncMock(side_effect=asyncio.CancelledError)
        ),
    ):
        with pytest.raises(asyncio.CancelledError):
            await cog._handle_youtube_video(ctx, VIDEO_ID)

    ctx.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_youtube_lavalink_cancellation_propagates(cog) -> None:
    ctx = _context()
    player = _player(None)
    player.load_tracks.side_effect = asyncio.CancelledError
    cog.yt = _youtube_client(_api_payload())

    with (
        patch.object(type(cog.tidal), "_run_blocking", new=_run_blocking),
        patch.object(type(cog.tidal), "search", new=AsyncMock(return_value=[])),
        patch.object(type(cog), "_get_player", new=AsyncMock(return_value=player)),
        patch.object(type(cog), "_ensure_vc_connected", new=AsyncMock(return_value=player)),
    ):
        with pytest.raises(asyncio.CancelledError):
            await cog._handle_youtube_video(ctx, VIDEO_ID)

    ctx.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_tplay_dispatches_parsed_youtube_video_id(cog) -> None:
    ctx = _context()
    handler = AsyncMock()

    with (
        patch.object(type(cog), "check_ready", new=AsyncMock(return_value=True)),
        patch.object(type(cog), "_handle_youtube_video", new=handler),
    ):
        await cog.tplay(ctx, query=f"https://youtu.be/{VIDEO_ID}")

    handler.assert_awaited_once_with(ctx, VIDEO_ID)
