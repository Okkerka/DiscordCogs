"""Native session behavior with controlled network and voice boundaries."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace

import discord
import pytest

from TidalPlayerExp.playback.errors import PlaybackUnavailable
from TidalPlayerExp.playback.models import (
    PlaybackEntry,
    ResolvedSource,
    SourceKind,
    SourceReference,
)
from TidalPlayerExp.playback.session import NativePlaybackSession


def entry(number=1, **kwargs):
    return PlaybackEntry(
        str(number),
        SourceReference(SourceKind.TIDAL, str(number)),
        None,
        {
            "title": str(number),
            "artist": "Artist",
            "album": None,
            "duration": 20,
            "quality": "LOSSLESS",
            "image": None,
            "share_url": None,
            "audio_resolution": "24/96",
            "track_id": number,
        },
        42,
        **kwargs,
    )


class Source(discord.AudioSource):
    def __init__(self):
        self.cleanups = 0

    def read(self):
        return b"audio"

    def is_opus(self):
        return True

    def cleanup(self):
        self.cleanups += 1


class Voice:
    def __init__(self):
        self.guild = SimpleNamespace(voice_client=self)
        self.channel = SimpleNamespace(id=99)
        self.connected = True
        self.played = []
        self.callbacks = []
        self.stops = 0
        self.disconnects = 0
        self.paused = False

    def is_connected(self):
        return self.connected

    def play(self, source, *, after):
        assert self.connected
        self.played.append(source)
        self.callbacks.append(after)

    def stop(self):
        self.stops += 1
        if self.callbacks:
            self.callbacks[-1](None)

    def pause(self):
        self.paused = True

    def resume(self):
        self.paused = False

    async def disconnect(self, *, force=False):
        self.disconnects += 1
        self.connected = False


class Resolver:
    def __init__(self):
        self.calls = []
        self.entered = asyncio.Event()
        self.gate = None
        self.fail = set()

    async def resolve(self, reference):
        self.calls.append(reference)
        self.entered.set()
        if self.gate:
            await self.gate.wait()
        if reference.kind in self.fail:
            raise RuntimeError("private signed URL")
        return ResolvedSource("https://example.com/audio", {})

    async def close(self):
        pass


class Factory:
    def __init__(self):
        self.sources = []
        self.entered = asyncio.Event()
        self.gate = None

    async def create(self, resolved):
        self.entered.set()
        if self.gate:
            await self.gate.wait()
        source = Source()
        self.sources.append(source)
        return source

    async def close(self):
        pass


class Sink:
    def __init__(self):
        self.events = asyncio.Queue()
        self.started = []
        self.failed = []
        self.ended = []
        self.reenter = None
        self.raise_error = False

    async def track_started(self, guild_id, current):
        self.started.append(current)
        await self.events.put("started")
        if self.reenter:
            await self.reenter()
        if self.raise_error:
            raise RuntimeError("private signed URL")

    async def track_failed(self, guild_id, current, reason):
        self.failed.append((current, reason))
        await self.events.put("failed")

    async def queue_ended(self, guild_id, previous):
        self.ended.append(previous)
        await self.events.put("ended")

    async def expect(self, expected):
        assert await asyncio.wait_for(self.events.get(), 2) == expected


def setup(**kwargs):
    voice, resolver, factory, sink = Voice(), Resolver(), Factory(), Sink()
    session = NativePlaybackSession(1, voice, resolver, factory, sink, **kwargs)
    return session, voice, resolver, factory, sink


@pytest.mark.asyncio
@pytest.mark.parametrize("known_duration, resolved_duration, expected", [(0, 243, 243), (180, 243, 180), (0, None, 0)])
@pytest.mark.parametrize("use_fallback", [False, True])
async def test_playback_publishes_missing_youtube_duration_without_overwriting_known_metadata(
    known_duration, resolved_duration, expected, use_fallback,
):
    voice, factory, sink = Voice(), Factory(), Sink()

    class DurationResolver(Resolver):
        async def resolve(self, reference):
            await super().resolve(reference)
            return ResolvedSource("https://example.com/audio", {}, duration=resolved_duration)

    resolver = DurationResolver()
    reference = SourceReference(SourceKind.YOUTUBE, "dQw4w9WgXcQ")
    youtube_meta = {**entry().meta, "duration": known_duration, "source": "YouTube", "track_id": None}
    original = (
        replace(entry(), fallback=reference, fallback_meta=youtube_meta)
        if use_fallback else replace(entry(), primary=reference, meta=youtube_meta)
    )
    if use_fallback:
        resolver.fail.add(SourceKind.TIDAL)
    session = NativePlaybackSession(1, voice, resolver, factory, sink)
    try:
        assert await session.enqueue(original)
        await sink.expect("started")
        current = session.snapshot().current
        assert current.primary == reference
        assert current.meta["duration"] == expected
        assert sink.started[0].meta["duration"] == expected
        assert current.entry_id == original.entry_id
        assert current.meta["source"].casefold() == "youtube"
        assert youtube_meta["duration"] == known_duration
        assert (original.fallback_meta if use_fallback else original.meta)["duration"] == known_duration
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_fifo_pause_resume_and_threaded_duplicate_end():
    session, voice, resolver, factory, sink = setup()
    assert await session.enqueue(entry(1))
    assert await session.enqueue(entry(2))
    await sink.expect("started")
    assert session.snapshot().current.entry_id == "1"
    assert await session.set_paused(True)
    assert session.snapshot().paused and voice.paused
    assert await session.set_paused(False)
    old_callback = voice.callbacks[0]
    await asyncio.to_thread(old_callback, None)
    old_callback(None)
    await sink.expect("started")
    assert session.snapshot().current.entry_id == "2"
    assert factory.sources[0].cleanups == 1
    voice.callbacks[-1](None)
    await sink.expect("ended")
    assert session.snapshot().current is None
    assert len(sink.ended) == 1
    assert [ref.identifier for ref in resolver.calls] == ["1", "2"]
    await session.close()


@pytest.mark.asyncio
async def test_simultaneous_enqueue_capacity_and_detached_snapshot():
    session, _voice, resolver, _factory, sink = setup(queue_capacity=2)
    results = await asyncio.gather(
        *(session.enqueue(entry(i), start_if_idle=False) for i in range(1, 6))
    )
    assert results == [True, True, False, False, False]
    snapshot = session.snapshot()
    assert [item.entry_id for item in snapshot.queued] == ["1", "2"]
    assert not resolver.calls
    await session.stop()
    assert len(snapshot.queued) == 2
    assert not session.snapshot().queued
    assert not sink.ended
    await session.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["skip", "stop", "close"])
async def test_cancel_resolving_and_no_stale_play(operation):
    session, voice, resolver, _factory, sink = setup()
    resolver.gate = asyncio.Event()
    await session.enqueue(entry())
    await resolver.entered.wait()
    assert session.snapshot().current.entry_id == "1"
    await getattr(session, operation)()
    resolver.gate.set()
    assert not voice.played
    assert session.snapshot().current is None
    assert len(sink.ended) == (1 if operation == "skip" else 0)
    await session.close()
    assert not await session.enqueue(entry(2))
    assert voice.disconnects == 1


@pytest.mark.asyncio
async def test_cancel_creation_waits_for_cleanup_before_next_source():
    session, voice, _resolver, factory, sink = setup()
    factory.gate = asyncio.Event()
    await session.enqueue(entry())
    await session.enqueue(entry(2))
    await factory.entered.wait()
    skipping = asyncio.create_task(session.skip())
    await asyncio.sleep(0)
    factory.gate.set()
    assert await skipping
    await sink.expect("started")
    assert session.snapshot().current.entry_id == "2"
    assert factory.sources[0].cleanups == 1
    assert len(voice.played) == 1
    await session.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("with_meta", [True, False])
async def test_retry_fallback_effective_metadata(with_meta):
    session, _voice, resolver, _factory, sink = setup()
    resolver.fail.add(SourceKind.TIDAL)
    fallback = SourceReference(SourceKind.YOUTUBE, "dQw4w9WgXcQ")
    metadata = {
        **entry().meta,
        "title": "Video title",
        "artist": "Channel",
        "share_url": "https://youtube.com/watch?v=dQw4w9WgXcQ",
    }
    queued = replace(
        entry(), fallback=fallback, fallback_meta=metadata if with_meta else None
    )
    await session.enqueue(queued)
    await sink.expect("started")
    effective = sink.started[0]
    assert effective.entry_id == queued.entry_id
    assert effective.primary == fallback
    assert effective.meta["source"] == "youtube"
    assert effective.meta["quality"] == "YouTube"
    assert effective.meta["track_id"] is None
    assert effective.meta["audio_resolution"] is None
    assert effective.meta["title"] == ("Video title" if with_meta else "YouTube video")
    assert [ref.kind for ref in resolver.calls] == [
        SourceKind.TIDAL,
        SourceKind.TIDAL,
        SourceKind.YOUTUBE,
    ]
    await session.close()


@pytest.mark.asyncio
async def test_three_failed_entries_halt_queue_and_user_enqueue_resumes():
    session, _voice, resolver, _factory, sink = setup()
    resolver.fail.add(SourceKind.TIDAL)
    for i in range(1, 6):
        await session.enqueue(entry(i))
    for _ in range(3):
        await sink.expect("failed")
    assert [item.entry_id for item in session.snapshot().queued] == ["4", "5"]
    assert not sink.ended
    resolver.fail.clear()
    await session.enqueue(entry(6))
    await sink.expect("started")
    assert session.snapshot().current.entry_id == "4"
    await session.close()


@pytest.mark.asyncio
async def test_reentrant_failing_sink_does_not_wedge_or_leak(caplog):
    session, voice, _resolver, _factory, sink = setup()

    async def reenter():
        assert session.snapshot().current is not None
        if len(sink.started) == 1:
            await session.enqueue(entry(2))

    sink.reenter = reenter
    sink.raise_error = True
    await session.enqueue(entry())
    await sink.expect("started")
    voice.callbacks[0](RuntimeError("private signed URL"))
    await sink.expect("failed")
    await sink.expect("started")
    assert session.snapshot().current.entry_id == "2"
    assert "private signed URL" not in caplog.text
    await session.close()


@pytest.mark.asyncio
async def test_replacement_client_is_never_stopped_or_disconnected():
    session, voice, _resolver, factory, sink = setup()
    await session.enqueue(entry())
    await sink.expect("started")
    foreign = Voice()
    voice.guild.voice_client = foreign
    await session.close()
    assert voice.stops == voice.disconnects == 0
    assert foreign.stops == foreign.disconnects == 0
    assert factory.sources[0].cleanups == 1


@pytest.mark.asyncio
async def test_disconnected_client_fails_safely():
    session, voice, _resolver, _factory, sink = setup()
    voice.connected = False
    await session.enqueue(entry())
    await sink.expect("failed")
    assert not voice.played
    await session.close()


@pytest.mark.asyncio
async def test_stale_callback_after_successor_started_cannot_end_new_track():
    session, voice, _resolver, _factory, sink = setup()
    await session.enqueue(entry())
    await session.enqueue(entry(2))
    await sink.expect("started")
    old_after = voice.callbacks[0]
    old_after(None)
    await sink.expect("started")
    assert session.snapshot().current.entry_id == "2"
    assert not sink.ended
    old_after(RuntimeError("stale error"))
    voice.callbacks[-1](None)
    await sink.expect("ended")
    await session.close()


@pytest.mark.asyncio
async def test_queue_end_sink_can_enqueue_autoplay():
    session, voice, _resolver, _factory, sink = setup()

    async def ended(guild_id, previous):
        await sink.events.put("ended")
        await session.enqueue(entry(2))

    sink.queue_ended = ended
    await session.enqueue(entry())
    await sink.expect("started")
    voice.callbacks[0](None)
    await sink.expect("ended")
    await sink.expect("started")
    assert session.snapshot().current.entry_id == "2"
    await session.close()


@pytest.mark.asyncio
async def test_concurrent_stop_skip_close_drain_pending_creation():
    session, voice, _resolver, factory, _sink = setup()
    factory.gate = asyncio.Event()
    await session.enqueue(entry())
    await factory.entered.wait()
    skip = asyncio.create_task(session.skip())
    await asyncio.sleep(0)
    stop = asyncio.create_task(session.stop())
    await asyncio.sleep(0)
    close = asyncio.create_task(session.close())
    await asyncio.sleep(0)
    factory.gate.set()
    await asyncio.gather(skip, stop, close)
    assert not voice.played
    assert len(factory.sources) == 1
    assert factory.sources[0].cleanups == 1
    assert session.closed


@pytest.mark.asyncio
async def test_failed_disconnect_is_sanitized_and_retryable():
    session, voice, _resolver, _factory, _sink = setup()

    async def fail(*, force=False):
        raise RuntimeError("private signed URL")

    voice.disconnect = fail
    with pytest.raises(PlaybackUnavailable) as caught:
        await session.close()
    assert "private signed URL" not in str(caught.value)
    assert caught.value.__context__ is None
    voice.disconnect = Voice.disconnect.__get__(voice)
    await session.close()
    assert voice.disconnects == 1
