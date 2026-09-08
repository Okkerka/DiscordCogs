import asyncio
import gc
import importlib
import threading
from unittest.mock import patch

import pytest


@pytest.mark.asyncio
@pytest.mark.parametrize("abandonment", ["timeout", "cancel"])
async def test_abandoned_worker_failure_is_retrieved(cog, monkeypatch, abandonment) -> None:
    loop = asyncio.get_running_loop()
    reports = []
    previous_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: reports.append(context))
    submitted = []
    started = asyncio.Event()

    def submit(_executor, _func):
        future = loop.create_future()
        submitted.append(future)
        started.set()
        return future

    async def exercise():
        with monkeypatch.context() as scoped:
            scoped.setattr(loop, "run_in_executor", submit)
            waiter = asyncio.create_task(cog.tidal._run_blocking(lambda: None, timeout=0.02))
            await started.wait()
            if abandonment == "cancel":
                waiter.cancel()
            with pytest.raises((asyncio.TimeoutError, asyncio.CancelledError)):
                await waiter
            submitted.pop().set_exception(RuntimeError("provider secret_token=private"))
            await asyncio.sleep(0)
            await asyncio.sleep(0)

    try:
        await exercise()
        gc.collect()
        assert reports == []
        # Late failure must release the worker, not leave capacity occupied.
        assert await cog.tidal._run_blocking(lambda: 42) == 42
    finally:
        loop.set_exception_handler(previous_handler)
        await cog.tidal.unload()


@pytest.mark.asyncio
async def test_abandoned_shared_request_failure_is_retrieved(cog) -> None:
    loop = asyncio.get_running_loop()
    reports = []
    previous_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: reports.append(context))
    started, release = asyncio.Event(), asyncio.Event()

    async def fail():
        started.set()
        await release.wait()
        raise RuntimeError("provider secret_token=private")

    async def exercise():
        waiter = asyncio.create_task(cog.tidal._coalesce("test", "one", fail))
        await started.wait()
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        release.set()
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    try:
        await exercise()
        gc.collect()
        assert cog.tidal._inflight == {}
        assert reports == []
    finally:
        loop.set_exception_handler(previous_handler)
        await cog.tidal.unload()


@pytest.mark.asyncio
async def test_active_waiters_still_receive_provider_errors(cog) -> None:
    def fail():
        raise ValueError("provider failure")

    async def shared():
        return await cog.tidal._run_blocking(fail)

    try:
        with pytest.raises(ValueError, match="provider failure"):
            await cog.tidal._coalesce("test", "one", shared)
    finally:
        await cog.tidal.unload()


@pytest.mark.asyncio
async def test_run_blocking_times_out_while_waiting_for_worker_slot(cog) -> None:
    release = threading.Event()
    started = threading.Barrier(5)

    def occupy() -> None:
        started.wait(timeout=2)
        release.wait(timeout=2)

    occupiers = [
        asyncio.create_task(cog.tidal._run_blocking(occupy, timeout=1.0))
        for _ in range(4)
    ]
    await asyncio.to_thread(started.wait, 2)
    submitted = False

    def fifth() -> None:
        nonlocal submitted
        submitted = True

    waiting = asyncio.create_task(cog.tidal._run_blocking(fifth, timeout=0.02))
    await asyncio.sleep(0.05)
    try:
        assert waiting.done()
        with pytest.raises(asyncio.TimeoutError):
            await waiting
        assert not submitted
    finally:
        waiting.cancel()
        release.set()
        await asyncio.gather(waiting, *occupiers, return_exceptions=True)


@pytest.mark.asyncio
async def test_recommendations_leave_two_provider_workers_for_foreground_calls(cog) -> None:
    module = importlib.import_module(cog.__class__.__module__)
    release = asyncio.Event()
    two_started = asyncio.Event()
    active = 0
    peak_active = 0

    async def similar(_cog, _artist, _title, limit=25):
        return [("Artist", "One"), ("Artist", "Two")]

    async def search(_handler, _query, filter_remixes=False):
        nonlocal active, peak_active
        active += 1
        peak_active = max(peak_active, active)
        if active >= 2:
            two_started.set()
        try:
            await release.wait()
        finally:
            active -= 1
        return []

    metas = [
        {"track_id": guild_id, "title": f"Track {guild_id}", "artist": "Artist"}
        for guild_id in (1, 2)
    ]
    with (
        patch.object(type(cog), "_lastfm_similar_tracks", new=similar),
        patch.object(type(cog.tidal), "search", new=search),
    ):
        tasks = [
            asyncio.create_task(cog._radio_candidates(guild_id, meta))
            for guild_id, meta in zip((1, 2), metas)
        ]
        await asyncio.wait_for(two_started.wait(), timeout=1.0)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        try:
            assert module.RECOMMENDATION_LOOKUP_CONCURRENCY == 2
            assert module.RECOMMENDATION_SEARCH_CONCURRENCY == 1
            assert peak_active == 2
        finally:
            release.set()
            await asyncio.gather(*tasks)
