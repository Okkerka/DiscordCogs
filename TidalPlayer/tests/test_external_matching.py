import importlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.asyncio
async def test_external_batch_does_not_fall_back_to_first_uncertain_result(cog) -> None:
    wrong = SimpleNamespace(
        id=99,
        name="Completely Different",
        full_name=None,
        artist=SimpleNamespace(name="Other Artist"),
    )
    with (
        patch.object(
            type(cog.tidal), "search", new=AsyncMock(return_value=[wrong])
        ),
        patch.object(type(cog), "_extract_meta", new=AsyncMock()) as extract_meta,
    ):
        result = await cog._resolve_and_extract(
            {"name": "Expected"},
            lambda _item: "Expected Artist Expected Song",
            False,
        )

    assert result is None
    extract_meta.assert_not_awaited()


@pytest.mark.asyncio
async def test_external_batch_keeps_exact_isrc_result(cog) -> None:
    track = SimpleNamespace(id=7)
    meta = {"track_id": 7, "title": "Exact", "artist": "Artist", "album": None}
    with (
        patch.object(
            type(cog.tidal),
            "get_track_by_isrc",
            new=AsyncMock(return_value=track),
        ),
        patch.object(type(cog), "_extract_meta", new=AsyncMock(return_value=meta)),
        patch.object(type(cog.tidal), "search", new=AsyncMock()) as search,
    ):
        result = await cog._resolve_and_extract(
            object(), lambda _item: "isrc:USUM71703861", False
        )

    assert result == (track, meta)
    search.assert_not_awaited()


@pytest.mark.asyncio
async def test_external_batch_keeps_direct_tidal_track(cog) -> None:
    module = importlib.import_module(cog.__class__.__module__)
    track = module.TidalTrack()
    track.id = 8
    meta = {"track_id": 8, "title": "Direct", "artist": "Artist", "album": None}
    with (
        patch.object(type(cog), "_extract_meta", new=AsyncMock(return_value=meta)),
        patch.object(type(cog.tidal), "search", new=AsyncMock()) as search,
    ):
        result = await cog._resolve_and_extract(track, lambda item: item, False)

    assert result == (track, meta)
    search.assert_not_awaited()


@pytest.mark.asyncio
async def test_lastfm_uncertain_first_result_is_not_a_candidate(cog) -> None:
    wrong = SimpleNamespace(
        id=77,
        name="Unrelated Noise",
        full_name=None,
        artist=SimpleNamespace(name="Someone Else"),
    )
    with (
        patch.object(
            type(cog),
            "_lastfm_similar_tracks",
            new=AsyncMock(return_value=[("Expected Artist", "Expected Song")]),
        ),
        patch.object(
            type(cog.tidal), "search", new=AsyncMock(side_effect=[[wrong], []])
        ),
    ):
        candidates = await cog._radio_candidates_limited(
            1, {"track_id": 1, "title": "Current", "artist": "Artist"}
        )

    assert candidates == []
