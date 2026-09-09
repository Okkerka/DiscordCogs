"""Bounded catalogue requests, negative results, and iterable consumption."""

from __future__ import annotations

import asyncio
import importlib
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest


@pytest.mark.asyncio
async def test_definitive_empty_isrc_result_is_cached_briefly_and_expires(cog) -> None:
    fetch = Mock(return_value=[])
    cog.tidal.session = SimpleNamespace(get_tracks_by_isrc=fetch)
    key = "USAAA0000001"

    assert await cog.tidal.get_track_by_isrc(key) is None
    assert await cog.tidal.get_track_by_isrc(key) is None
    assert fetch.call_count == 1
    value, expiry = cog.tidal._cache["isrc"][key]
    assert value is None
    assert 0 < expiry - asyncio.get_running_loop().time() <= 60

    cog.tidal._cache["isrc"][key] = (None, asyncio.get_running_loop().time() - 1)
    track = SimpleNamespace(id=123)
    fetch.return_value = [track]
    assert await cog.tidal.get_track_by_isrc(key) is track
    assert fetch.call_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["exception", "unsupported", "unavailable", "invalid"])
async def test_isrc_failure_is_not_negative_cached(cog, failure) -> None:
    if failure == "exception":
        session = SimpleNamespace(get_tracks_by_isrc=Mock(side_effect=RuntimeError("failed")))
    elif failure == "invalid":
        session = SimpleNamespace(get_tracks_by_isrc=Mock(return_value=None))
    elif failure == "unsupported":
        session = SimpleNamespace()
    else:
        session = None
    cog.tidal.session = session
    key = "USAAA0000002"
    assert await cog.tidal.get_track_by_isrc(key) is None

    track = SimpleNamespace(id=124)
    if session is None:
        session = SimpleNamespace()
        cog.tidal.session = session
    session.get_tracks_by_isrc = Mock(return_value=[track])
    assert await cog.tidal.get_track_by_isrc(key) is track


@pytest.mark.asyncio
async def test_negative_isrc_cache_uses_the_bounded_lru_bucket(cog, monkeypatch) -> None:
    module = importlib.import_module(cog.__class__.__module__)
    monkeypatch.setitem(module._CACHE_CAPS, "isrc", 2)
    fetch = Mock(return_value=[])
    cog.tidal.session = SimpleNamespace(get_tracks_by_isrc=fetch)

    for key in ("one", "two", "three", "three"):
        assert await cog.tidal.get_track_by_isrc(key) is None
    assert fetch.call_count == 3
    assert await cog.tidal.get_track_by_isrc("one") is None
    assert fetch.call_count == 4
    assert len(cog.tidal._cache["isrc"]) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("method, sdk_method", [
    ("get_album", "album"), ("get_playlist", "playlist"),
    ("get_mix", "mix_v2"), ("get_video", "video"),
])
async def test_catalogue_waiters_share_work_and_survive_one_cancellation(cog, method, sdk_method) -> None:
    loop = asyncio.get_running_loop()
    started = asyncio.Event()
    release = threading.Event()
    result = SimpleNamespace(id=123)

    def fetch(identifier):
        assert identifier == "123"
        loop.call_soon_threadsafe(started.set)
        assert release.wait(timeout=3)
        return result

    provider = Mock(side_effect=fetch)
    cog.tidal.session = SimpleNamespace(**{sdk_method: provider})
    lookup = getattr(cog.tidal, method)
    tasks = [asyncio.create_task(lookup("123")) for _ in range(6)]
    try:
        await asyncio.wait_for(started.wait(), timeout=1)
        tasks[0].cancel()
        with pytest.raises(asyncio.CancelledError):
            await tasks[0]
        release.set()
        assert all(value is result for value in await asyncio.gather(*tasks[1:]))
        assert await lookup("123") is result
        assert provider.call_count == 1
    finally:
        release.set()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("method, sdk_method", [
    ("get_album", "album"), ("get_playlist", "playlist"),
    ("get_mix", "mix_v2"), ("get_video", "video"),
])
async def test_logout_cancels_shared_catalogue_result_before_session_replacement(cog, method, sdk_method) -> None:
    loop = asyncio.get_running_loop()
    started = asyncio.Event()
    release = threading.Event()
    old = SimpleNamespace(id="old")
    new = SimpleNamespace(id="new")

    def fetch(_identifier):
        loop.call_soon_threadsafe(started.set)
        assert release.wait(timeout=3)
        return old

    cog.tidal.session = SimpleNamespace(**{sdk_method: fetch})
    lookup = getattr(cog.tidal, method)
    pending = asyncio.create_task(lookup("123"))
    try:
        await asyncio.wait_for(started.wait(), timeout=1)
        await cog.tidal.logout()
        cog.tidal.session = SimpleNamespace(**{sdk_method: lambda _identifier: new})
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert await lookup("123") is new
        assert await lookup("123") is new
    finally:
        release.set()
        await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("attribute, callable_value", [("tracks", True), ("tracks", False), ("items", False)])
async def test_fallback_stops_consuming_at_container_item_limit(cog, monkeypatch, attribute, callable_value) -> None:
    module = importlib.import_module(cog.__class__.__module__)
    monkeypatch.setattr(module, "MAX_ITEMS", 3)

    def values():
        yield from (1, 2, 3)
        raise AssertionError("must not consume beyond the requested item limit")

    container = SimpleNamespace(**{attribute: values if callable_value else values()})
    assert await cog.tidal.get_items(container) == [1, 2, 3]


@pytest.mark.asyncio
@pytest.mark.parametrize("sparse_supported", [True, False])
async def test_oversized_iterable_pages_are_bounded_before_materialization(cog, monkeypatch, sparse_supported) -> None:
    module = importlib.import_module(cog.__class__.__module__)
    monkeypatch.setattr(module, "MAX_ITEMS", 5)
    monkeypatch.setattr(module, "PAGINATION_LIMIT", 2)
    calls = []

    def items(*, limit, offset, **kwargs):
        if kwargs and not sparse_supported:
            raise TypeError("sparse_album unsupported")
        calls.append((limit, offset))

        def values():
            yield from range(offset, offset + limit)
            raise AssertionError("provider ignored its requested page size")

        return values()

    assert await cog.tidal.get_items(SimpleNamespace(items=items)) == [0, 1, 2, 3, 4]
    assert calls == [(2, 0), (2, 2), (1, 4)]
