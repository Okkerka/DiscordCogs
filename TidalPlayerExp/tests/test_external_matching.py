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


def _tidal_track(identifier, title, artist, full_name=None):
    return SimpleNamespace(
        id=identifier,
        name=title,
        full_name=full_name,
        artist=SimpleNamespace(name=artist),
    )


@pytest.mark.asyncio
async def test_the_playing_song_in_another_version_is_not_suggested(cog) -> None:
    """A live take of the playing song is the same music, not a suggestion."""
    # Tidal stores some live releases with the qualifier inside the title.
    live = _tidal_track(78, "Doomed (Live at the Royal Albert Hall)", "Bring Me The Horizon")
    other = _tidal_track(79, "Drown", "Bring Me The Horizon")
    with (
        patch.object(
            type(cog),
            "_lastfm_similar_tracks",
            new=AsyncMock(return_value=[
                ("Bring Me The Horizon", "Doomed (Live at the Royal Albert Hall)"),
                ("Bring Me The Horizon", "Drown"),
            ]),
        ),
        patch.object(
            type(cog.tidal), "search", new=AsyncMock(side_effect=[[live], [other]])
        ),
    ):
        candidates = await cog._radio_candidates_limited(
            1, {"track_id": 1, "title": "Doomed", "artist": "Bring Me The Horizon"}
        )

    assert candidates == [other]


@pytest.mark.asyncio
async def test_a_live_version_named_through_the_version_field_is_not_suggested(cog) -> None:
    """The same rule applies when Tidal keeps the qualifier in the version."""
    live = _tidal_track(
        78, "Doomed", "Bring Me The Horizon", "Doomed (L.I.V.E. In Sao Paulo)"
    )
    other = _tidal_track(79, "Can You Feel My Heart", "Bring Me The Horizon")
    with (
        patch.object(
            type(cog),
            "_lastfm_similar_tracks",
            new=AsyncMock(return_value=[
                ("Bring Me The Horizon", "Doomed (L.I.V.E. In Sao Paulo)"),
                ("Bring Me The Horizon", "Can You Feel My Heart"),
            ]),
        ),
        patch.object(
            type(cog.tidal), "search", new=AsyncMock(side_effect=[[live], [other]])
        ),
    ):
        candidates = await cog._radio_candidates_limited(
            1, {"track_id": 1, "title": "Doomed", "artist": "Bring Me The Horizon"}
        )

    assert candidates == [other]


@pytest.mark.asyncio
async def test_the_search_fallback_drops_the_playing_song_in_another_version(cog) -> None:
    live = _tidal_track(78, "Doomed (L.I.V.E. In Sao Paulo)", "Bring Me The Horizon")
    other = _tidal_track(79, "Can You Feel My Heart", "Bring Me The Horizon")
    with (
        patch.object(
            type(cog), "_lastfm_similar_tracks", new=AsyncMock(return_value=[])
        ),
        patch.object(
            type(cog.tidal), "search", new=AsyncMock(return_value=[live, other])
        ),
    ):
        candidates = await cog._radio_candidates_limited(
            1, {"track_id": 1, "title": "Doomed", "artist": "Bring Me The Horizon"}
        )

    assert candidates == [other]


@pytest.mark.asyncio
async def test_a_different_song_by_another_artist_is_still_suggested(cog) -> None:
    """Only the playing song itself is dropped, not every similar title."""
    collab = _tidal_track(80, "Bad Habits (feat. Bring Me The Horizon)", "Ed Sheeran")
    with (
        patch.object(
            type(cog),
            "_lastfm_similar_tracks",
            new=AsyncMock(return_value=[("Ed Sheeran", "Bad Habits")]),
        ),
        patch.object(
            type(cog.tidal), "search", new=AsyncMock(return_value=[collab])
        ),
    ):
        candidates = await cog._radio_candidates_limited(
            1, {"track_id": 1, "title": "Doomed", "artist": "Bring Me The Horizon"}
        )

    assert candidates == [collab]


@pytest.mark.asyncio
async def test_a_live_version_of_another_song_is_still_suggested(cog) -> None:
    """A different song in another version is a different recording."""
    live = _tidal_track(81, "Drown (Live)", "Bring Me The Horizon")
    with (
        patch.object(
            type(cog),
            "_lastfm_similar_tracks",
            new=AsyncMock(return_value=[("Bring Me The Horizon", "Drown (Live)")]),
        ),
        patch.object(
            type(cog.tidal), "search", new=AsyncMock(return_value=[live])
        ),
    ):
        candidates = await cog._radio_candidates_limited(
            1, {"track_id": 1, "title": "Doomed", "artist": "Bring Me The Horizon"}
        )

    assert candidates == [live]
