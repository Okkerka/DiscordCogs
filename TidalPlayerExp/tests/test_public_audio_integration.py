"""Public-audio references share native admission and extractor ownership."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from TidalPlayerExp.playback.errors import PlaybackUnavailable
from TidalPlayerExp.playback.models import ResolvedSource, SourceKind, SourceReference
from TidalPlayerExp.providers.tidal_source import CompositeSourceResolver
from TidalPlayerExp.providers.public_audio import PublicAudioMetadata
from TidalPlayerExp.providers.urls import parse_provider_url
from TidalPlayerExp.tests.test_youtube_fallback import _context


def _metadata(url):
    parsed = parse_provider_url(url)
    return PublicAudioMetadata(SourceReference(SourceKind(parsed.provider.value), parsed.identifier), {
        "title": "Song", "artist": "Artist", "duration": 240, "album": None,
        "quality": "Public audio", "image": None, "share_url": parsed.identifier,
        "audio_resolution": None, "track_id": None, "source": parsed.provider.value,
    })


@pytest.fixture
def public_ctx(cog, native_session):
    cog._initialized = True
    cog.check_ready = AsyncMock(side_effect=AssertionError("Public audio needs no TIDAL login"))
    cog.public_audio_resolver = SimpleNamespace(
        fetch_metadata=AsyncMock(), fetch_collection=AsyncMock(),
        resolve=AsyncMock(side_effect=AssertionError("No media resolution during admission")),
    )
    return _context()


@pytest.mark.asyncio
@pytest.mark.parametrize("url", [
    "https://soundcloud.com/artist/song?utm_source=share",
    "https://artist.bandcamp.com/track/song",
])
async def test_public_track_command_is_keyless_and_queues_stable_reference(cog, public_ctx, native_session, url):
    metadata = _metadata(url)
    cog.public_audio_resolver.fetch_metadata.return_value = metadata
    await cog.tplay(public_ctx, query=url)
    cog.check_ready.assert_not_awaited()
    cog.public_audio_resolver.fetch_metadata.assert_awaited_once_with(metadata.reference)
    entry = native_session.entries[0]
    assert entry.primary == metadata.reference
    assert entry.meta == metadata.meta
    assert entry.fallback is None
    assert entry.requester_id == public_ctx.author.id
    cog.public_audio_resolver.resolve.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("url,track_url", [
    ("https://soundcloud.com/artist/sets/set", "https://soundcloud.com/artist/song"),
    ("https://artist.bandcamp.com/album/record", "https://artist.bandcamp.com/track/song"),
])
async def test_public_collection_is_capped_ordered_and_lazy(cog, public_ctx, native_session, url, track_url):
    items = tuple(_metadata(f"{track_url}-{index}") for index in range(12))
    cog.public_audio_resolver.fetch_collection.return_value = items
    await cog.tplay(public_ctx, query=url)
    cog.public_audio_resolver.fetch_collection.assert_awaited_once_with(url, 100)
    assert [entry.primary for entry in native_session.entries] == [item.reference for item in items]
    cog.public_audio_resolver.fetch_metadata.assert_not_awaited()
    cog.public_audio_resolver.resolve.assert_not_awaited()
    assert not cog._cancel_events
    assert "Queued 12/12" in public_ctx.send.return_value.edit.await_args.kwargs["embed"].description


@pytest.mark.asyncio
async def test_public_track_checks_voice_before_extraction(cog, public_ctx, native_session):
    public_ctx.author.voice = None
    await cog.tplay(public_ctx, query="https://soundcloud.com/artist/song")
    cog.public_audio_resolver.fetch_metadata.assert_not_awaited()
    native_session.enqueue.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("collection", [False, True])
async def test_stop_while_public_metadata_pending_cannot_restart_playback(cog, public_ctx, native_session, collection):
    ready, release = asyncio.Event(), asyncio.Event()
    item = _metadata("https://soundcloud.com/artist/song")

    async def metadata(*args):
        ready.set()
        await release.wait()
        return (item,) if collection else item

    resolver_method = "fetch_collection" if collection else "fetch_metadata"
    getattr(cog.public_audio_resolver, resolver_method).side_effect = metadata
    url = "https://soundcloud.com/artist/sets/set" if collection else item.reference.identifier
    task = asyncio.create_task(cog.tplay(public_ctx, query=url))
    try:
        await asyncio.wait_for(ready.wait(), 2)
        await cog.controller_stop(SimpleNamespace(
            guild=public_ctx.guild, response=SimpleNamespace(defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        ))
        release.set()
        await asyncio.wait_for(task, 2)
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    native_session.enqueue.assert_not_awaited()
    assert not cog._cancel_events


@pytest.mark.asyncio
async def test_tstop_public_collection_preserves_admitted_entries(cog, public_ctx, native_session):
    items = tuple(_metadata(f"https://soundcloud.com/artist/song-{index}") for index in range(4))
    cog.public_audio_resolver.fetch_collection.return_value = items

    async def enqueue(entry):
        native_session._enqueue(entry)
        await cog.tstop(public_ctx)
        return True

    native_session.enqueue.side_effect = enqueue
    await cog.tplay(public_ctx, query="https://soundcloud.com/artist/sets/set")
    assert len(native_session.entries) == 1
    native_session.stop.assert_not_awaited()
    assert "Cancelled" in public_ctx.send.return_value.edit.await_args.kwargs["embed"].title
    assert not cog._cancel_events


@pytest.mark.asyncio
async def test_public_queue_capacity_reports_remaining(cog, public_ctx, native_session):
    items = tuple(_metadata(f"https://soundcloud.com/artist/song-{index}") for index in range(4))
    cog.public_audio_resolver.fetch_collection.return_value = items
    native_session.enqueue.side_effect = lambda entry: native_session._enqueue(entry) if not native_session.entries else False
    await cog.tplay(public_ctx, query="https://soundcloud.com/artist/sets/set")
    assert len(native_session.entries) == 1
    final = public_ctx.send.return_value.edit.await_args.kwargs["embed"]
    assert "Queued 1/4" in final.description and "Skipped 3" in final.description
    assert not cog._cancel_events


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [RuntimeError("token=secret"), asyncio.CancelledError()])
async def test_public_collection_failure_releases_claim_without_leaking_details(cog, public_ctx, native_session, error):
    cog.public_audio_resolver.fetch_collection.side_effect = error
    if isinstance(error, asyncio.CancelledError):
        with pytest.raises(asyncio.CancelledError):
            await cog.tplay(public_ctx, query="https://soundcloud.com/artist/sets/set")
    else:
        await cog.tplay(public_ctx, query="https://soundcloud.com/artist/sets/set")
        assert "secret" not in public_ctx.send.await_args.kwargs["embed"].description
    assert not cog._cancel_events
    native_session.enqueue.assert_not_awaited()


@pytest.mark.asyncio
async def test_public_collection_cannot_take_an_existing_batch_claim(cog, public_ctx, native_session):
    event = cog._claim_batch(public_ctx.guild.id)
    try:
        await cog.tplay(public_ctx, query="https://soundcloud.com/artist/sets/set")
        cog.public_audio_resolver.fetch_collection.assert_not_awaited()
        assert cog._cancel_events[public_ctx.guild.id] is event
        native_session.enqueue.assert_not_awaited()
    finally:
        cog._release_batch(public_ctx.guild.id, event)


@pytest.mark.asyncio
async def test_public_audio_routes_through_shared_extractor_without_double_close():
    tidal = SimpleNamespace(resolve=AsyncMock(), close=AsyncMock())
    youtube = SimpleNamespace(resolve=AsyncMock(), close=AsyncMock())
    expected = ResolvedSource("https://media.example/audio", {}, codec="mp3")
    public = SimpleNamespace(resolve=AsyncMock(return_value=expected), close=AsyncMock())
    resolver = CompositeSourceResolver(tidal, youtube, public_audio=public)
    for kind, url in (
        (SourceKind.SOUNDCLOUD, "https://soundcloud.com/artist/song"),
        (SourceKind.BANDCAMP, "https://artist.bandcamp.com/track/song"),
    ):
        reference = SourceReference(kind, url)
        assert await resolver.resolve(reference) is expected
        public.resolve.assert_awaited_with(reference)
    await resolver.close()
    await resolver.close()
    youtube.close.assert_awaited_once_with()
    public.close.assert_not_awaited()
    tidal.close.assert_not_awaited()
    with pytest.raises(PlaybackUnavailable):
        await resolver.resolve(reference)
