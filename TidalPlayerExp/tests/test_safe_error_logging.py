"""Unexpected SDK failures must not expose signed URLs or credentials."""

from __future__ import annotations

import asyncio
import gc
import importlib
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

SECRET = "https://provider.invalid/audio?secret_token=private-credential"


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["get_track", "get_track_by_isrc", "get_video", "get_similar_albums", "get_user_playlists", "get_user_playlist_by_id"])
async def test_tidal_metadata_failures_do_not_log_sdk_exception(cog, caplog, method) -> None:
    caplog.set_level(logging.DEBUG, logger="red.tidalplayerexp")
    failing = Mock(side_effect=RuntimeError(SECRET))
    cog.tidal.session = SimpleNamespace(
        track=failing, get_tracks_by_isrc=failing, video=failing,
        user=SimpleNamespace(playlists=failing),
    )
    if method == "get_user_playlists":
        args = ()
    elif method == "get_similar_albums":
        args = (SimpleNamespace(similar=failing),)
    elif method == "get_user_playlist_by_id":
        class BrokenPlaylist:
            @property
            def creator(self):
                raise RuntimeError(SECRET)

        cog.tidal.session.playlist = Mock(return_value=BrokenPlaylist())
        args = ("123",)
    else:
        args = ("123",)
    try:
        assert not await getattr(cog.tidal, method)(*args)
        assert SECRET not in caplog.text
        assert "RuntimeError" in caplog.text
        assert all(record.exc_info is None for record in caplog.records)
    finally:
        await cog.tidal.unload()


@pytest.mark.asyncio
@pytest.mark.parametrize("slash,deferred", [(False, False), (True, False), (True, True)])
@pytest.mark.parametrize("error_type", [RuntimeError, ValueError])
async def test_command_error_handlers_keep_provider_failures_private(cog, caplog, monkeypatch, slash, deferred, error_type) -> None:
    module = importlib.import_module(cog.__class__.__module__)

    class InvokeError(Exception):
        def __init__(self):
            self.original = error_type(SECRET)

    monkeypatch.setattr(module.app_commands if slash else module.commands, "CommandInvokeError", InvokeError)
    target = SimpleNamespace(
        command="tplay", send=AsyncMock(),
        response=SimpleNamespace(is_done=lambda: deferred, send_message=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()),
    )
    if slash:
        await cog.cog_app_command_error(target, InvokeError())
        response = target.followup.send if deferred else target.response.send_message
    else:
        await cog.cog_command_error(target, InvokeError())
        response = target.send
    response.assert_awaited_once()
    assert SECRET not in caplog.text
    assert error_type.__name__ in caplog.text
    assert SECRET not in response.await_args.kwargs["embed"].description
    assert all(record.exc_info is None for record in caplog.records)


@pytest.mark.asyncio
async def test_recommendation_failure_log_does_not_include_provider_url(cog, caplog) -> None:
    cog._radio_candidates = AsyncMock(side_effect=RuntimeError(SECRET))
    assert await cog._get_recommendations(1, {"track_id": "123"}) == []
    assert SECRET not in caplog.text
    assert "RuntimeError" in caplog.text
    assert all(record.exc_info is None for record in caplog.records)


@pytest.mark.asyncio
async def test_replaced_abandoned_recommendation_task_retrieves_late_failure(cog) -> None:
    loop = asyncio.get_running_loop()
    reports = []
    previous_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: reports.append(context))
    started, release = asyncio.Event(), asyncio.Event()

    async def candidates(*args):
        if not started.is_set():
            started.set()
            await release.wait()
            raise RuntimeError(SECRET)
        return []

    cog._radio_candidates = candidates

    async def exercise():
        meta = {"track_id": "123"}
        waiter = asyncio.create_task(cog._get_recommendations(1, meta))
        await asyncio.wait_for(started.wait(), timeout=1)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        release.set()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert await cog._get_recommendations(1, meta) == []

    try:
        await exercise()
        gc.collect()
        assert reports == []
    finally:
        release.set()
        loop.set_exception_handler(previous_handler)


@pytest.mark.asyncio
async def test_recommendation_panel_failure_log_does_not_include_provider_url(cog, caplog) -> None:
    cog._controller_meta[1] = {"track_id": "123"}
    cog._get_recommendations = AsyncMock(side_effect=RuntimeError(SECRET))
    await cog._refresh_controller_recommendations(1, "id:123")
    assert SECRET not in caplog.text
    assert "RuntimeError" in caplog.text
    assert all(record.exc_info is None for record in caplog.records)
