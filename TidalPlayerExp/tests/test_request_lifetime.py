"""A stopped request cannot enqueue after an outstanding provider call."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from TidalPlayerExp.tests.conftest import make_entry


def context():
    return SimpleNamespace(guild=SimpleNamespace(id=1), author=SimpleNamespace(id=2, voice=SimpleNamespace(channel=SimpleNamespace(id=22))),
                           channel=SimpleNamespace(), interaction=object(),
                           send=AsyncMock(), defer=AsyncMock())


@pytest.mark.asyncio
async def test_stop_during_track_lookup_prevents_late_enqueue(cog, native_session, monkeypatch):
    ctx = context()
    entered, release = asyncio.Event(), asyncio.Event()
    async def lookup(*_):
        entered.set()
        await release.wait()
        return SimpleNamespace(id=1)
    monkeypatch.setattr(type(cog.tidal), "get_track", lookup)
    cog._extract_meta = AsyncMock(return_value=make_entry().meta)
    cog._prepare_playback_session = AsyncMock(return_value=native_session)
    pending = asyncio.create_task(cog._handle_track(ctx, "1"))
    await entered.wait()
    cog._cancel_imports(1)
    release.set()
    await pending
    assert not native_session.entries
    assert ctx.send.await_count == 1


@pytest.mark.asyncio
async def test_stop_command_cancels_initial_playlist_lookup_and_clears_playback(cog, native_session, monkeypatch):
    ctx = context()
    entered, release = asyncio.Event(), asyncio.Event()
    async def lookup(*_):
        entered.set()
        await release.wait()
        return SimpleNamespace(name="Collection")
    monkeypatch.setattr(type(cog.tidal), "get_playlist", lookup)
    monkeypatch.setattr(type(cog.tidal), "get_items", AsyncMock(return_value=[SimpleNamespace(id=1)]))
    cog._resolve_and_extract = AsyncMock(return_value=(SimpleNamespace(id=1), make_entry().meta))
    cog.check_ready = AsyncMock(return_value=True)
    cog._prepare_playback_session = AsyncMock(return_value=native_session)
    native_session.current = make_entry(2)
    native_session.entries = [make_entry(3)]
    pending = asyncio.create_task(cog._handle_playlist(ctx, "1"))
    await entered.wait()
    await cog.stop_command(ctx)
    release.set()
    await pending
    assert not native_session.entries
    assert native_session.current is None
    native_session.stop.assert_awaited_once_with(clear_queue=True)
    assert 1 not in cog._cancel_events


@pytest.mark.asyncio
async def test_search_defers_before_provider_readiness(cog):
    ctx = context()
    async def ready(_):
        assert ctx.defer.await_count == 1
        return False
    cog.check_ready = ready
    await cog.tsearch(ctx, query="song")


@pytest.mark.asyncio
async def test_aborted_admission_finishes_slash_response(cog, native_session):
    ctx = context()
    cog._stop_generations[1] = 1
    assert not await cog._admit_entry(ctx, native_session, make_entry(), stop_generation=0)
    assert ctx.send.await_count == 1


@pytest.mark.asyncio
async def test_full_queue_stops_further_batch_resolution(cog, native_session):
    ctx = context()
    cog.check_ready = AsyncMock(return_value=True)
    cog._prepare_playback_session = AsyncMock(return_value=native_session)
    cog._resolve_and_extract = AsyncMock(return_value=(SimpleNamespace(id=1), make_entry().meta))
    native_session.enqueue.side_effect = lambda *args, **kwargs: False
    await cog._process_track_list(ctx, list(range(24)), "Collection", lambda item: item)
    assert cog._resolve_and_extract.await_count <= 8
    assert native_session.enqueue.await_count == 1
