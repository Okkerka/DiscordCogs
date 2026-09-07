import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from TidalPlayerExp.tests.conftest import make_entry


@pytest.mark.asyncio
async def test_batch_admits_stable_references_in_order_without_resolving_streams(cog, native_session) -> None:
    first = SimpleNamespace(id=1)
    second = SimpleNamespace(id=2)
    metas = [make_entry(1, title="One", artist="A").meta, make_entry(2, title="Two", artist="B").meta]

    ctx = SimpleNamespace(
        guild=SimpleNamespace(id=9),
        author=SimpleNamespace(id=5),
        channel=SimpleNamespace(),
    )

    with patch.object(type(cog.tidal), "get_stream_url", new=AsyncMock()) as stream:
        queued, skipped = await cog._queue_resolved_chunk(
            ctx,
            native_session,
            [(first, metas[0]), (second, metas[1])],
            asyncio.Event(),
        )

    assert (queued, skipped) == (2, 0)
    assert [entry.primary.identifier for entry in native_session.entries] == ["1", "2"]
    assert len({entry.entry_id for entry in native_session.entries}) == 2
    stream.assert_not_awaited()
    assert not cog._current_meta


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
