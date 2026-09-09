"""Refresh persistence must be ordered with owner logout and client replacement."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


@pytest.mark.asyncio
async def test_logout_wins_over_refresh_write_already_in_progress(cog, monkeypatch):
    stored = {"refresh_token": "old"}
    entered, release = asyncio.Event(), asyncio.Event()
    async def write(_service, **values):
        entered.set()
        await release.wait()
        stored.update(values)
    async def remove(_service, *keys):
        for key in keys:
            stored.pop(key, None)
    async def read(_service):
        return dict(stored)
    cog.bot.set_shared_api_tokens = write
    cog.bot.remove_shared_api_tokens = remove
    cog.bot.get_shared_api_tokens = read
    monkeypatch.setattr(type(cog.tidal), "_run_blocking", AsyncMock(return_value="result"))
    cog.sp = object()
    cog._spotify_auth_manager = SimpleNamespace(
        cache_handler=SimpleNamespace(get_cached_token=lambda: {"refresh_token": "rotated"}))
    cog._spotify_refresh_token = "old"
    refresh = asyncio.create_task(cog._run_spotify(lambda client: "result", timeout=1))
    await entered.wait()
    logout = asyncio.create_task(cog.tidalsetup_spotifylogout(SimpleNamespace(send=AsyncMock())))
    await asyncio.sleep(0)
    release.set()
    await asyncio.wait_for(asyncio.gather(refresh, logout), 1)
    assert "refresh_token" not in stored
    assert cog._spotify_auth_manager is None


@pytest.mark.asyncio
async def test_failed_refresh_write_does_not_publish_token_in_memory(cog, monkeypatch):
    monkeypatch.setattr(type(cog.tidal), "_run_blocking", AsyncMock(return_value="result"))
    cog.sp = object()
    cog._spotify_auth_manager = SimpleNamespace(
        cache_handler=SimpleNamespace(get_cached_token=lambda: {"refresh_token": "rotated"}))
    cog._spotify_refresh_token = "old"
    cog.bot.set_shared_api_tokens = AsyncMock(side_effect=OSError("storage offline"))
    assert await cog._run_spotify(lambda client: "result", timeout=1) == "result"
    assert cog._spotify_refresh_token == "old"
