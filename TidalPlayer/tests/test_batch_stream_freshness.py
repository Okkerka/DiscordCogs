import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.mark.asyncio
async def test_batch_resolves_each_stream_immediately_before_its_load(cog) -> None:
    events: list[str] = []
    first = SimpleNamespace(id=1)
    second = SimpleNamespace(id=2)
    metas = [
        {"track_id": 1, "title": "One", "artist": "A", "album": None},
        {"track_id": 2, "title": "Two", "artist": "B", "album": None},
    ]

    async def stream(_handler, track):
        events.append(f"stream:{track.id}")
        return f"https://stream/{track.id}"

    async def load(url):
        events.append(f"load:{url.rsplit('/', 1)[-1]}")
        return SimpleNamespace(tracks=[SimpleNamespace()])

    player = SimpleNamespace(
        queue=[], current=object(), add=MagicMock(), load_tracks=load
    )
    ctx = SimpleNamespace(
        guild=SimpleNamespace(id=9),
        author=SimpleNamespace(),
        channel=SimpleNamespace(),
    )

    with patch.object(type(cog.tidal), "get_stream_url", new=stream):
        queued, skipped = await cog._queue_resolved_chunk(
            ctx,
            player,
            [(first, metas[0]), (second, metas[1])],
            asyncio.Event(),
        )

    assert (queued, skipped) == (2, 0)
    assert events == ["stream:1", "load:1", "stream:2", "load:2"]


@pytest.mark.asyncio
async def test_batch_catalog_resolution_does_not_prefetch_signed_url(cog) -> None:
    track = SimpleNamespace(
        id=5,
        name="Song",
        full_name=None,
        artist=SimpleNamespace(name="Artist"),
    )
    meta = {"track_id": 5, "title": "Song", "artist": "Artist", "album": None}
    with (
        patch.object(
            type(cog.tidal), "search", new=AsyncMock(return_value=[track])
        ),
        patch.object(type(cog), "_extract_meta", new=AsyncMock(return_value=meta)),
        patch.object(type(cog.tidal), "get_stream_url", new=AsyncMock()) as get_stream,
    ):
        result = await cog._resolve_and_extract(
            object(), lambda _item: "Artist Song", False
        )

    assert result == (track, meta)
    get_stream.assert_not_awaited()
