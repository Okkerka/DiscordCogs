"""Recording identity must survive external-provider lookup and ISRC misses."""
import importlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from TidalPlayerExp.domain.candidates import NormalizedCandidate
from TidalPlayerExp.domain.matching import select_best_tidal_track


def track(title="The Night We Met", artist="Lord Huron", **changes):
    return SimpleNamespace(id=12, name=title, full_name=None,
                           artist=SimpleNamespace(name=artist), **changes)


@pytest.mark.parametrize("title,artist,wrong_artist", [
    ("The Night We Met", "Lord Huron", "John Smith"),
    ("Nothing Else Matters", "Metallica", "Someone Else"),
])
def test_title_subset_does_not_override_explicit_wrong_artist(title, artist, wrong_artist):
    assert select_best_tidal_track(f"{title} {artist}", [track(title, wrong_artist)]) is None


@pytest.mark.parametrize("wrong", [
    track(artist="John Smith"), track(title="The Night We Met (Live)"),
    track(title="The Night We Met (Remix)"), track(title="The Night We Met Again"),
])
def test_structured_candidates_require_title_artist_and_variant_identity(wrong):
    candidate = NormalizedCandidate("The Night We Met", ("Lord Huron",), source="spotify")
    assert select_best_tidal_track(candidate, [wrong]) is None


def test_structured_metadata_selects_the_matching_recording():
    right = track()
    candidate = NormalizedCandidate("The Night We Met", ("Lord Huron",), duration=208, source="spotify")
    assert select_best_tidal_track(candidate, [track(artist="John Smith"), right]) is right


def test_missing_structured_artist_never_becomes_title_only_match():
    assert select_best_tidal_track(NormalizedCandidate("The Night We Met", ()), [track()]) is None


@pytest.mark.parametrize("query", ["The Night We Met", "Lord Huron The Night We Met", "The Night We Met Lord Huron"])
def test_freeform_title_and_artist_queries_remain_supported(query):
    right = track()
    assert select_best_tidal_track(query, [right]) is right


def spotify_item():
    return {"name": "The Night We Met", "artists": [{"name": "Lord Huron"}],
            "external_ids": {"isrc": "USAAA0000001"}, "duration_ms": 208500}


@pytest.mark.parametrize("converter,wrap", [("_spotify_album_item_to_query", False), ("_spotify_item_to_query", True)])
def test_spotify_conversion_keeps_isrc_and_textual_fallback(cog, converter, wrap):
    module = importlib.import_module(cog.__class__.__module__)
    item = spotify_item()
    candidate = getattr(module, converter)({"item": item} if wrap else item)
    assert candidate == NormalizedCandidate("The Night We Met", ("Lord Huron",),
        isrc="USAAA0000001", duration=208, source="spotify")


@pytest.mark.asyncio
async def test_spotify_batch_isrc_miss_falls_back_to_title_and_artist(cog):
    module = importlib.import_module(cog.__class__.__module__)
    right = track()
    meta = {"title": "The Night We Met", "artist": "Lord Huron", "track_id": 12}
    with (
        patch.object(type(cog.tidal), "get_track_by_isrc", new=AsyncMock(return_value=None)) as isrc,
        patch.object(type(cog.tidal), "search", new=AsyncMock(return_value=[right])) as search,
        patch.object(type(cog), "_extract_meta", new=AsyncMock(return_value=meta)),
    ):
        result = await cog._resolve_and_extract({"item": spotify_item()}, module._spotify_item_to_query, False)
    assert result == (right, meta)
    isrc.assert_awaited_once_with("USAAA0000001")
    search.assert_awaited_once_with("The Night We Met Lord Huron", filter_remixes=False)


@pytest.mark.asyncio
async def test_single_spotify_track_skips_unrelated_first_result(cog):
    cog.sp = object()
    ctx = SimpleNamespace(guild=SimpleNamespace(id=1), send=AsyncMock())
    right = track()
    with (
        patch.object(type(cog), "_run_spotify", new=AsyncMock(return_value=spotify_item())),
        patch.object(type(cog.tidal), "get_track_by_isrc", new=AsyncMock(return_value=None)),
        patch.object(type(cog.tidal), "search", new=AsyncMock(return_value=[track(artist="John Smith"), right])),
        patch.object(type(cog), "_load_and_queue_track", new=AsyncMock()) as queue,
    ):
        await cog._handle_spotify_track(ctx, "https://open.spotify.com/track/0123456789012345678901")
    queue.assert_awaited_once_with(ctx, right)


@pytest.mark.asyncio
async def test_spotify_album_reuses_initial_metadata_snapshot(cog):
    item = spotify_item()
    album = {"name": "Album", "images": [], "tracks": {"items": [item], "next": None}}
    client = SimpleNamespace(album=MagicMock(return_value=album))
    cog.sp = client
    ctx = SimpleNamespace(guild=SimpleNamespace(id=1), send=AsyncMock())

    async def run_spotify(_self, operation, **kwargs):
        return operation(client)

    with (
        patch.object(type(cog), "_run_spotify", new=run_spotify),
        patch.object(type(cog), "_process_track_list", new=AsyncMock()) as process,
    ):
        await cog._handle_spotify_album(ctx, "https://open.spotify.com/album/0123456789012345678901")
    client.album.assert_called_once_with("0123456789012345678901")
    assert process.await_args.args[1:3] == ([item], "Album")
