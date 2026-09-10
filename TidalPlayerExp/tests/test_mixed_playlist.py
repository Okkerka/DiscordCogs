"""Mixed playlist imports are ordered, bounded, cancellable, and resolve audio lazily."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from TidalPlayerExp.playback.models import SourceKind
from TidalPlayerExp.tests.test_youtube_fallback import _candidate, _context, _run_blocking, _video
from TidalPlayerExp.ui.embeds import Messages


PLAYLIST_ID = "PLexample"


def _playlist_item(number: int, title: str | None = None):
    return {"snippet": {
        "title": title or f"Artist - Song {number}",
        "videoOwnerChannelTitle": "Artist",
        "resourceId": {"videoId": f"{number:011d}"},
    }}


def _playlist_client(pages):
    execute = Mock(side_effect=pages)
    listing = Mock(return_value=SimpleNamespace(execute=execute))
    return SimpleNamespace(playlistItems=lambda: SimpleNamespace(list=listing)), listing, execute


@pytest.fixture
def playlist_ctx(cog, native_session, monkeypatch):
    cog.yt = None
    cog._initialized = True
    monkeypatch.setattr(type(cog.tidal), "_run_blocking", _run_blocking)
    monkeypatch.setattr(type(cog.tidal), "is_logged_in", AsyncMock(return_value=False))
    monkeypatch.setattr(type(cog.tidal), "search", AsyncMock(return_value=[]))
    cog.youtube_resolver.fetch_metadata = AsyncMock(side_effect=AssertionError("No per-video extraction at admission"))
    cog.youtube_resolver.resolve = AsyncMock(side_effect=AssertionError("No signed media at admission"))
    cog.youtube_resolver.fetch_playlist = AsyncMock(return_value=(_video("00000000001"), _video("00000000002")))
    return _context()


@pytest.mark.asyncio
async def test_keyless_unauthenticated_playlist_uses_capped_flat_metadata(cog, playlist_ctx, native_session):
    await cog._handle_youtube_playlist(playlist_ctx, PLAYLIST_ID)

    cog.youtube_resolver.fetch_playlist.assert_awaited_once_with(PLAYLIST_ID, 100)
    assert [entry.primary.identifier for entry in native_session.entries] == ["00000000001", "00000000002"]
    assert all(entry.primary.kind is SourceKind.YOUTUBE for entry in native_session.entries)
    cog.tidal.search.assert_not_awaited()
    cog.youtube_resolver.fetch_metadata.assert_not_awaited()
    cog.youtube_resolver.resolve.assert_not_awaited()
    playlist_ctx.send.assert_awaited_once()
    final = playlist_ctx.send.return_value.edit.await_args.kwargs["embed"]
    assert "Queued 2/2" in final.description
    assert "Finished" in final.title
    assert not cog._cancel_events


@pytest.mark.asyncio
async def test_api_failure_uses_keyless_playlist_fallback(cog, playlist_ctx, native_session):
    cog.yt, _, _ = _playlist_client([RuntimeError("provider unavailable")])
    await cog._handle_youtube_playlist(playlist_ctx, PLAYLIST_ID)
    cog.youtube_resolver.fetch_playlist.assert_awaited_once_with(PLAYLIST_ID, 100)
    assert len(native_session.entries) == 2
    assert not cog._cancel_events


@pytest.mark.asyncio
async def test_api_playlist_filters_invalid_and_duplicate_items_in_order(cog, playlist_ctx, native_session):
    malformed = _playlist_item(3)
    malformed["snippet"]["resourceId"]["videoId"] = "invalid!"
    cog.yt, _, _ = _playlist_client([{"items": [
        _playlist_item(2), _playlist_item(1), _playlist_item(2),
        _playlist_item(4, "[Private video]"), _playlist_item(5, "[Deleted video]"),
        malformed, {"snippet": {"title": ["invalid"]}}, None,
    ]}])

    await cog._handle_youtube_playlist(playlist_ctx, PLAYLIST_ID)

    assert [entry.primary.identifier for entry in native_session.entries] == ["00000000002", "00000000001"]
    cog.youtube_resolver.fetch_playlist.assert_not_awaited()
    cog.youtube_resolver.fetch_metadata.assert_not_awaited()
    cog.youtube_resolver.resolve.assert_not_awaited()


@pytest.mark.asyncio
async def test_flat_playlist_filters_private_deleted_and_duplicates(cog, playlist_ctx, native_session):
    cog.youtube_resolver.fetch_playlist.return_value = (
        _video("00000000002"), _video("00000000001"), _video("00000000002"),
        _video("00000000003", "[Private video]"), _video("00000000004", "[Deleted video]"),
    )
    await cog._handle_youtube_playlist(playlist_ctx, PLAYLIST_ID)
    assert [entry.primary.identifier for entry in native_session.entries] == ["00000000002", "00000000001"]


@pytest.mark.asyncio
async def test_mixed_matching_preserves_order_and_original_fallback(cog, playlist_ctx, native_session):
    cog.tidal.is_logged_in.return_value = True
    cog.youtube_resolver.fetch_playlist.return_value = tuple(_video(f"{i:011d}", f"AZALI - Rivals {i}") for i in range(12))
    active = peak = 0

    async def search(query, **_kwargs):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        try:
            await asyncio.sleep(0)
            number = int(query.split("Rivals ")[1].split()[0])
            return [_candidate(f"Rivals {number}")] if number % 2 == 0 else []
        finally:
            active -= 1

    cog.tidal.search.side_effect = search
    await cog._handle_youtube_playlist(playlist_ctx, PLAYLIST_ID)

    assert len(native_session.entries) == 12
    assert 1 < peak <= 8
    for index, entry in enumerate(native_session.entries):
        if index % 2 == 0:
            assert entry.primary.kind is SourceKind.TIDAL
            assert entry.fallback.identifier == f"{index:011d}"
            assert entry.fallback_meta["title"] == f"AZALI - Rivals {index}"
            assert entry.fallback_meta["share_url"].endswith(f"v={index:011d}")
        else:
            assert entry.primary.kind is SourceKind.YOUTUBE
            assert entry.primary.identifier == f"{index:011d}"
    cog.youtube_resolver.resolve.assert_not_awaited()
    cog.youtube_resolver.fetch_metadata.assert_not_awaited()


@pytest.mark.asyncio
async def test_api_playlist_caps_raw_work_and_output_at_1000(cog, playlist_ctx):
    pages = [{"items": [_playlist_item(i) for i in range(start, start + 50)], "nextPageToken": str(start + 50)} for start in range(0, 1100, 50)]
    cog.yt, _, execute = _playlist_client(pages)
    items = await cog._fetch_all_youtube_tracks(PLAYLIST_ID)
    assert len(items) == 1000
    assert execute.call_count == 20
    assert items[-1]["snippet"]["resourceId"]["videoId"] == "00000000999"


@pytest.mark.asyncio
async def test_repeated_page_token_stops_import(cog, playlist_ctx):
    cog.yt, listing, execute = _playlist_client([
        {"items": [_playlist_item(1)], "nextPageToken": "repeat"},
        {"items": [_playlist_item(2)], "nextPageToken": "repeat"},
    ])
    assert len(await cog._fetch_all_youtube_tracks(PLAYLIST_ID)) == 2
    assert execute.call_count == 2
    assert listing.call_args_list[1].kwargs["pageToken"] == "repeat"


@pytest.mark.asyncio
@pytest.mark.parametrize("empty_pages", [False, True])
async def test_unusable_pages_have_a_finite_work_budget(cog, playlist_ctx, empty_pages):
    pages = [{"items": [] if empty_pages else [None] * 50, "nextPageToken": str(i)} for i in range(1001)]
    cog.yt, _, execute = _playlist_client(pages)
    assert await cog._fetch_all_youtube_tracks(PLAYLIST_ID) == []
    assert execute.call_count == (1000 if empty_pages else 20)


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["enumeration", "matching", "admission"])
async def test_cancel_imports_prevents_future_admission_and_preserves_already_queued(cog, playlist_ctx, native_session, stage):
    entered, release = asyncio.Event(), asyncio.Event()
    original_enqueue = native_session._enqueue

    async def enumerate_videos(*_args):
        entered.set()
        await release.wait()
        return (_video("00000000001"), _video("00000000002"))

    async def search(*_args, **_kwargs):
        entered.set()
        await release.wait()
        return []

    async def enqueue(entry, **kwargs):
        result = original_enqueue(entry, **kwargs)
        entered.set()
        await release.wait()
        return result

    if stage == "enumeration":
        cog.youtube_resolver.fetch_playlist.side_effect = enumerate_videos
    elif stage == "matching":
        cog.tidal.is_logged_in.return_value = True
        cog.tidal.search.side_effect = search
    else:
        native_session.enqueue.side_effect = enqueue
    task = asyncio.create_task(cog._handle_youtube_playlist(playlist_ctx, PLAYLIST_ID))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        cog._cancel_imports(playlist_ctx.guild.id)
        release.set()
        await asyncio.wait_for(task, 2)
    finally:
        release.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    assert len(native_session.entries) == (1 if stage == "admission" else 0)
    native_session.stop.assert_not_awaited()
    assert not cog._cancel_events
    assert "Cancelled" in playlist_ctx.send.return_value.edit.await_args.kwargs["embed"].title


@pytest.mark.asyncio
async def test_queue_capacity_stops_catalog_work_and_reports_remaining(cog, playlist_ctx, native_session):
    cog.tidal.is_logged_in.return_value = True
    cog.youtube_resolver.fetch_playlist.return_value = tuple(_video(f"{i:011d}") for i in range(25))
    original_enqueue = native_session._enqueue
    native_session.enqueue.side_effect = lambda entry: original_enqueue(entry) if len(native_session.entries) < 2 else False

    await cog._handle_youtube_playlist(playlist_ctx, PLAYLIST_ID)

    assert len(native_session.entries) == 2
    assert cog.tidal.search.await_count == 8
    final = playlist_ctx.send.return_value.edit.await_args.kwargs["embed"]
    assert "Queued 2/25" in final.description
    assert "Skipped 23" in final.description
    assert not cog._cancel_events


@pytest.mark.asyncio
async def test_batch_claim_precedes_enumeration_and_duplicate_claim_is_rejected(cog, playlist_ctx, native_session):
    event = cog._claim_batch(playlist_ctx.guild.id)
    try:
        await cog._handle_youtube_playlist(playlist_ctx, PLAYLIST_ID)
        cog.youtube_resolver.fetch_playlist.assert_not_awaited()
        native_session.enqueue.assert_not_awaited()
        assert playlist_ctx.send.await_args.kwargs["embed"].description == Messages.ERROR_BATCH_IN_PROGRESS
        assert cog._cancel_events[playlist_ctx.guild.id] is event
    finally:
        cog._release_batch(playlist_ctx.guild.id, event)


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [RuntimeError("unavailable"), asyncio.CancelledError()])
async def test_enumeration_failure_always_releases_claim(cog, playlist_ctx, native_session, error):
    cog.youtube_resolver.fetch_playlist.side_effect = error
    if isinstance(error, asyncio.CancelledError):
        with pytest.raises(asyncio.CancelledError):
            await cog._handle_youtube_playlist(playlist_ctx, PLAYLIST_ID)
        assert playlist_ctx.send.await_count == 1
    else:
        await cog._handle_youtube_playlist(playlist_ctx, PLAYLIST_ID)
        assert playlist_ctx.send.await_args.kwargs["embed"].description == Messages.ERROR_FETCH_FAILED
    assert not cog._cancel_events
    native_session.enqueue.assert_not_awaited()
