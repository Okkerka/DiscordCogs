"""Regression tests for bounded, shared provider workloads."""

from __future__ import annotations

import asyncio
import importlib
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def test_queued_embed_delete_delay_is_one_minute(cog) -> None:
    module = importlib.import_module(cog.__class__.__module__)
    assert module.QUEUED_EMBED_DELETE_DELAY == 60.0


@pytest.mark.asyncio
async def test_concurrent_identical_searches_share_one_tidal_request(cog) -> None:
    calls = 0
    result_track = SimpleNamespace(id=1, name="Track")

    def search(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        time.sleep(0.02)
        return {"tracks": [result_track]}

    cog.tidal.session.search = search

    results = await asyncio.gather(*(cog.tidal.search("artist track") for _ in range(12)))

    assert calls == 1
    assert all(result == [result_track] for result in results)


@pytest.mark.asyncio
async def test_cancelling_one_search_waiter_does_not_cancel_the_shared_request(cog) -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0
    result_track = SimpleNamespace(id=2, name="Track")

    async def search_operation(*_args):
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return [result_track]

    with patch.object(type(cog.tidal), "_search_uncached", new=AsyncMock(side_effect=search_operation)):
        first = asyncio.create_task(cog.tidal.search("artist track"))
        await started.wait()
        cancelled_waiter = asyncio.create_task(cog.tidal.search("artist track"))
        cancelled_waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cancelled_waiter
        release.set()

        assert await first == [result_track]
        assert calls == 1


@pytest.mark.asyncio
async def test_recommendations_reserve_capacity_for_foreground_work(cog) -> None:
    active = 0
    peak_active = 0
    two_started = asyncio.Event()
    release = asyncio.Event()

    async def limited(_self, _guild_id, _meta):
        nonlocal active, peak_active
        active += 1
        peak_active = max(peak_active, active)
        if active == 2:
            two_started.set()
        await release.wait()
        active -= 1
        return []

    with patch.object(type(cog), "_radio_candidates_limited", new=limited):
        tasks = [asyncio.create_task(cog._radio_candidates(guild_id, {})) for guild_id in range(3)]
        await two_started.wait()
        assert peak_active == 2
        release.set()
        await asyncio.gather(*tasks)


@pytest.mark.asyncio
async def test_lastfm_request_uses_the_reusable_async_session(cog) -> None:
    class Response:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        def raise_for_status(self) -> None:
            return None

        async def json(self, *, content_type):
            assert content_type is None
            return {"similartracks": {"track": [{"name": "Song", "artist": {"name": "Artist"}}]}}

    session = SimpleNamespace(closed=False, get=MagicMock(return_value=Response()))
    cog._lastfm_session = session
    cog.bot.get_shared_api_tokens = AsyncMock(return_value={"api_key": "key"})

    assert await cog._lastfm_similar_tracks("Artist", "Song") == [("Artist", "Song")]
    session.get.assert_called_once()


@pytest.mark.asyncio
async def test_autoplay_rejects_same_song_with_a_different_tidal_id(cog) -> None:
    guild_id = 1
    cog._remember_track(
        guild_id,
        {
            "title": "Same Song",
            "artist": "Artist",
            "album": None,
            "duration": 120,
            "quality": "LOSSLESS",
            "image": None,
            "share_url": None,
            "audio_resolution": None,
            "track_id": 1,
        },
    )
    candidate = SimpleNamespace(id=2, name="Same Song", artist=SimpleNamespace(name="Artist"))

    assert not await cog.queue_autoplay_track(guild_id, SimpleNamespace(), candidate)


@pytest.mark.asyncio
async def test_large_queue_metadata_does_not_drop_tracks_before_they_start(cog) -> None:
    guild = SimpleNamespace(id=42)
    entries = [
        {
            "title": f"Track {index}",
            "artist": "Artist",
            "album": None,
            "duration": 120,
            "quality": "LOSSLESS",
            "image": None,
            "share_url": None,
            "audio_resolution": None,
            "track_id": index,
        }
        for index in range(30)
    ]
    cog._queued_meta[guild.id].extend(entries)
    cog._refresh_controller = AsyncMock()
    cog._schedule_controller_recommendations = MagicMock()

    await cog.on_red_audio_track_start(
        guild,
        SimpleNamespace(title="Track 0", author="Artist"),
        requester=SimpleNamespace(),
    )

    assert cog._current_meta[guild.id] == entries[0]
    assert len(cog._queued_meta[guild.id]) == 29
    assert cog._queued_meta[guild.id].maxlen is None


@pytest.mark.asyncio
async def test_fallback_recommendations_remove_alternate_tidal_ids_for_same_song(cog) -> None:
    first = SimpleNamespace(id=10, name="Suggested", artist=SimpleNamespace(name="Artist"))
    alternate = SimpleNamespace(id=11, name="Suggested", artist=SimpleNamespace(name="Artist"))
    distinct = SimpleNamespace(id=12, name="Different", artist=SimpleNamespace(name="Artist"))

    with (
        patch.object(type(cog), "_lastfm_similar_tracks", new=AsyncMock(return_value=[])),
        patch.object(type(cog.tidal), "search", new=AsyncMock(return_value=[first, alternate, distinct])),
    ):
        candidates = await cog._radio_candidates_limited(
            1,
            {
                "title": "Current",
                "artist": "Artist",
                "album": None,
                "duration": 120,
                "quality": "LOSSLESS",
                "image": None,
                "share_url": None,
                "audio_resolution": None,
                "track_id": 1,
            },
        )

    assert candidates == [first, distinct]


@pytest.mark.asyncio
async def test_batch_playback_initialises_controller_state_with_current_track(cog) -> None:
    class Player:
        def __init__(self) -> None:
            self.current = None
            self.queue = []

        def add(self, _requester, track) -> None:
            self.queue.append(track)

        async def play(self) -> None:
            self.current = self.queue[0]

    guild_id = 77
    meta = {
        "title": "Track",
        "artist": "Artist",
        "album": None,
        "duration": 120,
        "quality": "LOSSLESS",
        "image": None,
        "share_url": None,
        "audio_resolution": None,
        "track_id": 77,
    }
    ctx = SimpleNamespace(
        guild=SimpleNamespace(id=guild_id),
        author=SimpleNamespace(),
        channel=SimpleNamespace(id=10),
    )
    loaded = SimpleNamespace()

    with patch.object(type(cog), "_load_lavalink_track", new=AsyncMock(return_value=loaded)):
        queued, skipped = await cog._queue_resolved_chunk(
            ctx,
            Player(),
            [(SimpleNamespace(id=77), "https://stream", meta)],
            asyncio.Event(),
        )

    assert (queued, skipped) == (1, 0)
    assert cog._current_meta[guild_id] == meta
    assert cog._controller_meta[guild_id] == meta
    assert cog._playback_channels[guild_id] == ctx.channel


@pytest.mark.asyncio
async def test_youtube_import_stops_when_the_api_repeats_a_page_token(cog) -> None:
    responses = iter((
        {
            "items": [{"snippet": {"title": "First track"}}],
            "nextPageToken": "repeat-token",
        },
        {
            "items": [{"snippet": {"title": "Repeated page"}}],
            "nextPageToken": "repeat-token",
        },
        {"items": [], "nextPageToken": None},
    ))
    request_count = 0

    class PlaylistItems:
        def list(self, **_kwargs):
            return SimpleNamespace(execute=lambda: next(responses))

    cog.yt = SimpleNamespace(playlistItems=lambda: PlaylistItems())

    async def run_blocking(_handler, operation, **_kwargs):
        nonlocal request_count
        request_count += 1
        return operation()

    with patch.object(type(cog.tidal), "_run_blocking", new=run_blocking):
        tracks = await cog._fetch_all_youtube_tracks("playlist")

    assert [item["snippet"]["title"] for item in tracks] == ["First track", "Repeated page"]
    assert request_count == 2


@pytest.mark.asyncio
async def test_youtube_import_handles_a_malformed_api_response_without_crashing(cog) -> None:
    cog.yt = SimpleNamespace(
        playlistItems=lambda: SimpleNamespace(
            list=lambda **_kwargs: SimpleNamespace(execute=lambda: "not a response object")
        )
    )

    async def run_blocking(_handler, operation, **_kwargs):
        return operation()

    with patch.object(type(cog.tidal), "_run_blocking", new=run_blocking):
        assert await cog._fetch_all_youtube_tracks("playlist") == []


@pytest.mark.asyncio
async def test_finished_autoplay_task_does_not_remove_a_newer_task(cog) -> None:
    guild_id = 101
    await cog.config.guild_from_id(guild_id).autoplay_enabled.set(True)
    cog._current_meta[guild_id] = {"track_id": 1, "title": "Track", "artist": "Artist"}
    recommendations_started = asyncio.Event()
    release_recommendations = asyncio.Event()

    async def get_recommendations(_cog, _guild_id, _meta):
        recommendations_started.set()
        await release_recommendations.wait()
        return []

    player = SimpleNamespace(queue=[], current=None)
    with patch.object(type(cog), "_get_recommendations", new=get_recommendations):
        first_task = asyncio.create_task(cog._run_autoplay(guild_id, player))
        cog._autoplay_tasks[guild_id] = first_task
        await recommendations_started.wait()

        replacement_task = asyncio.create_task(asyncio.Event().wait())
        cog._autoplay_tasks[guild_id] = replacement_task
        release_recommendations.set()
        try:
            await first_task
            assert cog._autoplay_tasks[guild_id] is replacement_task
        finally:
            replacement_task.cancel()
            await asyncio.gather(replacement_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_controller_stop_cancels_recommendation_work(cog) -> None:
    guild_id = 102
    pending = asyncio.Event()
    recommendation_task = asyncio.create_task(pending.wait())
    controller_task = asyncio.create_task(pending.wait())
    cog._recommendation_tasks[guild_id] = recommendation_task
    cog._recommendation_task_sources[guild_id] = "id:1"
    cog._controller_recommendation_tasks[guild_id] = controller_task
    cog._recommendation_cache[guild_id] = ("id:1", [])
    interaction = SimpleNamespace(
        guild=SimpleNamespace(id=guild_id),
        response=SimpleNamespace(defer=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()),
    )

    try:
        with patch.object(type(cog), "_get_player_for_guild", new=AsyncMock(return_value=None)):
            await cog.controller_stop(interaction)
        await asyncio.sleep(0)

        assert recommendation_task.cancelled()
        assert controller_task.cancelled()
        assert guild_id not in cog._recommendation_tasks
        assert guild_id not in cog._controller_recommendation_tasks
        assert guild_id not in cog._recommendation_task_sources
        assert guild_id not in cog._recommendation_cache
    finally:
        recommendation_task.cancel()
        controller_task.cancel()
        await asyncio.gather(recommendation_task, controller_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_recommendation_cannot_queue_a_song_already_in_the_queue(cog) -> None:
    guild_id = 103
    cog._queued_meta[guild_id].append(
        {"track_id": 2, "title": "Suggested", "artist": "Artist"}
    )
    interaction = SimpleNamespace(
        guild=SimpleNamespace(id=guild_id),
        user=SimpleNamespace(),
        response=SimpleNamespace(is_done=MagicMock(return_value=True)),
        followup=SimpleNamespace(send=AsyncMock()),
    )
    player = SimpleNamespace()
    candidate = SimpleNamespace(id=2, name="Suggested", artist=SimpleNamespace(name="Artist"))

    with (
        patch.object(type(cog), "_get_player_for_guild", new=AsyncMock(return_value=player)),
        patch.object(type(cog), "_load_lavalink_track", new=AsyncMock()) as load_track,
    ):
        assert not await cog.queue_recommendation(interaction, candidate)

    load_track.assert_not_awaited()
