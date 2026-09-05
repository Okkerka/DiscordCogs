"""Behavioral tests for lazy TIDAL playback source resolution."""

from __future__ import annotations

import asyncio
import threading
import traceback
from collections.abc import Callable
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from TidalPlayerExp.playback import (
    PlaybackUnavailable,
    ResolvedSource,
    SourceKind,
    SourceReference,
    SourceResolutionError,
)
from TidalPlayerExp.providers.tidal_source import (
    CompositeSourceResolver,
    TidalSourceResolver,
)


def _error_surface(error: BaseException) -> str:
    formatted = "".join(
        traceback.format_exception(type(error), error, error.__traceback__)
    )
    return "\n".join(
        (
            str(error),
            repr(error),
            formatted,
            repr(error.__cause__),
            repr(error.__context__),
        )
    )


class _DirectTrack:
    def __init__(
        self,
        url: object,
        *,
        duration: object = 180,
        codec: object = None,
        stream: object = None,
    ) -> None:
        self.duration = duration
        self.codec = codec
        self._url = url
        self._stream = stream
        self.get_url_calls = 0
        self.get_stream_calls = 0

    def get_url(self) -> object:
        self.get_url_calls += 1
        if isinstance(self._url, BaseException):
            raise self._url
        return self._url

    def get_stream(self) -> object:
        self.get_stream_calls += 1
        if isinstance(self._stream, BaseException):
            raise self._stream
        if self._stream is None:
            raise AttributeError
        return self._stream


class _Video:
    duration = 240

    def __init__(self, url: object) -> None:
        self._url = url
        self.get_url_calls = 0

    def get_url(self) -> object:
        self.get_url_calls += 1
        if isinstance(self._url, BaseException):
            raise self._url
        return self._url


class _BTSManifest:
    manifest_mime_type = "application/vnd.tidal.bts"
    is_bts = True
    is_mpd = False

    def __init__(
        self,
        urls: object,
        *,
        codecs: object = "FLAC",
        sample_rate: object = 96_000,
        encryption_type: object = "NONE",
        encryption_key: object = None,
        is_encrypted: object = False,
    ) -> None:
        self._urls = urls
        self.codecs = codecs
        self.sample_rate = sample_rate
        self.encryption_type = encryption_type
        self.encryption_key = encryption_key
        self.is_encrypted = is_encrypted

    def get_urls(self) -> object:
        return self._urls


class _Stream:
    def __init__(self, manifest: object, *, sample_rate: object = 96_000) -> None:
        self._manifest = manifest
        self.sample_rate = sample_rate

    def get_stream_manifest(self) -> object:
        return self._manifest


class _HandlerFake:
    def __init__(
        self, *, tracks: list[object] | None = None, videos: list[object] | None = None
    ) -> None:
        self.api_semaphore = asyncio.Semaphore(2)
        self._tracks = list(tracks or [])
        self._videos = list(videos or [])
        self.track_ids: list[str] = []
        self.video_ids: list[str] = []
        self.provider_calls = 0
        self.unload_calls = 0

    async def get_track(self, track_id: str) -> object:
        self.track_ids.append(track_id)
        value = self._tracks.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value

    async def get_video(self, video_id: str) -> object:
        self.video_ids.append(video_id)
        value = self._videos.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value

    async def _run_with_backoff(
        self, func: Callable[[], object], timeout: float = 10.0
    ) -> object:
        del timeout
        self.provider_calls += 1
        return func()

    async def unload(self) -> None:
        self.unload_calls += 1


class _YouTubeFake:
    def __init__(self) -> None:
        self.references: list[SourceReference] = []
        self.close_calls = 0

    async def resolve(self, reference: SourceReference) -> ResolvedSource:
        self.references.append(reference)
        return ResolvedSource("https://youtube.example/fresh", {})

    async def close(self) -> None:
        self.close_calls += 1


@pytest.mark.asyncio
async def test_track_resolution_refetches_object_and_url_on_every_attempt() -> None:
    first = _DirectTrack("https://audio.example/first", duration=181, codec="FLAC")
    second = _DirectTrack("https://audio.example/second", duration=182, codec="mystery")
    handler = _HandlerFake(tracks=[first, second])
    resolver = TidalSourceResolver(handler)
    reference = SourceReference(SourceKind.TIDAL, "00123")

    resolved_first = await resolver.resolve(reference)
    resolved_second = await resolver.resolve(reference)

    assert (resolved_first.url, resolved_first.duration, resolved_first.codec) == (
        "https://audio.example/first",
        181,
        "flac",
    )
    assert (resolved_second.url, resolved_second.duration, resolved_second.codec) == (
        "https://audio.example/second",
        182,
        None,
    )
    assert handler.track_ids == ["00123", "00123"]
    assert first.get_url_calls == second.get_url_calls == 1


@pytest.mark.asyncio
async def test_track_fallback_uses_current_stream_manifest_shape() -> None:
    manifest = _BTSManifest(
        ["https://audio.example/fallback.flac"], codecs="FLAC", sample_rate=96_000
    )
    track = _DirectTrack(RuntimeError("direct unavailable"), stream=_Stream(manifest))
    resolver = TidalSourceResolver(_HandlerFake(tracks=[track]))

    source = await resolver.resolve(SourceReference(SourceKind.TIDAL, "123"))

    assert source.url == "https://audio.example/fallback.flac"
    assert source.codec == "flac"
    assert source.sample_rate == 96_000
    assert track.get_stream_calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "manifest",
    [
        _BTSManifest(["https://audio.example/encrypted"], encryption_type="OLD_AES"),
        _BTSManifest(["https://audio.example/encrypted"], encryption_key="secret-key"),
        _BTSManifest(["https://audio.example/encrypted"], is_encrypted=True),
        SimpleNamespace(
            manifest_mime_type="application/dash+xml",
            is_bts=False,
            is_mpd=True,
            encryption_type="NONE",
            encryption_key=None,
            is_encrypted=False,
            codecs="FLAC",
            sample_rate=44_100,
            get_urls=lambda: ["https://audio.example/init-fragment.mp4"],
        ),
    ],
)
async def test_track_fallback_rejects_encrypted_or_segmented_manifest(
    manifest: object,
) -> None:
    track = _DirectTrack(RuntimeError("direct unavailable"), stream=_Stream(manifest))
    resolver = TidalSourceResolver(_HandlerFake(tracks=[track]))

    with pytest.raises(SourceResolutionError) as caught:
        await resolver.resolve(SourceReference(SourceKind.TIDAL, "123"))

    assert "secret-key" not in _error_surface(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "http://audio.example/stream",
        "https://user:password@audio.example/stream",
        "https://audio.example:bad/stream",
        "https://audio.example:65536/stream",
        "not-a-url",
        None,
    ],
)
async def test_track_resolution_rejects_invalid_url_without_echoing_it(
    url: object,
) -> None:
    resolver = TidalSourceResolver(_HandlerFake(tracks=[_DirectTrack(url)]))

    with pytest.raises(SourceResolutionError) as caught:
        await resolver.resolve(SourceReference(SourceKind.TIDAL, "123"))

    if isinstance(url, str):
        assert url not in _error_surface(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


@pytest.mark.asyncio
async def test_unavailable_or_provider_failure_is_sanitized() -> None:
    secret = "token=provider-secret"
    for provider_result in (None, RuntimeError(secret)):
        resolver = TidalSourceResolver(_HandlerFake(tracks=[provider_result]))

        with pytest.raises(SourceResolutionError) as caught:
            await resolver.resolve(SourceReference(SourceKind.TIDAL, "123"))

        assert secret not in _error_surface(caught.value)
        assert caught.value.__cause__ is None
        assert caught.value.__context__ is None


@pytest.mark.asyncio
async def test_wrong_kind_and_malformed_reference_are_rejected_safely() -> None:
    resolver = TidalSourceResolver(_HandlerFake())

    for reference in (SourceReference(SourceKind.YOUTUBE, "dQw4w9WgXcQ"), object()):
        with pytest.raises(SourceResolutionError) as caught:
            await resolver.resolve(reference)  # type: ignore[arg-type]

        assert caught.value.__cause__ is None
        assert caught.value.__context__ is None


@pytest.mark.asyncio
async def test_nonpositive_or_unknown_track_hints_are_omitted() -> None:
    track = _DirectTrack(
        "https://audio.example/source",
        duration=-1,
        codec="OPUS-ISH",
    )
    resolver = TidalSourceResolver(_HandlerFake(tracks=[track]))

    source = await resolver.resolve(SourceReference(SourceKind.TIDAL, "123"))

    assert source.duration is None
    assert source.codec is None
    assert source.sample_rate is None
    assert source.channels is None


@pytest.mark.asyncio
async def test_track_provider_cancellation_propagates() -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    class Handler(_HandlerFake):
        async def get_track(self, track_id: str) -> object:
            del track_id
            started.set()
            await release.wait()
            return _DirectTrack("https://audio.example/source")

    resolver = TidalSourceResolver(Handler())
    task = asyncio.create_task(
        resolver.resolve(SourceReference(SourceKind.TIDAL, "123"))
    )
    await started.wait()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_identical_track_and_video_ids_use_distinct_lazy_provider_paths() -> None:
    track = _DirectTrack("https://audio.example/track")
    first_video = _Video("https://video.example/first.m3u8")
    second_video = _Video("https://video.example/second.m3u8")
    handler = _HandlerFake(tracks=[track], videos=[first_video, second_video])
    resolver = TidalSourceResolver(handler)

    track_source = await resolver.resolve(SourceReference(SourceKind.TIDAL, "123"))
    video_first = await resolver.resolve(SourceReference(SourceKind.TIDAL_VIDEO, "123"))
    video_second = await resolver.resolve(
        SourceReference(SourceKind.TIDAL_VIDEO, "123")
    )

    assert track_source.url == "https://audio.example/track"
    assert video_first.url == "https://video.example/first.m3u8"
    assert video_second.url == "https://video.example/second.m3u8"
    assert video_first.codec is video_second.codec is None
    assert handler.track_ids == ["123"]
    assert handler.video_ids == ["123", "123"]
    assert first_video.get_url_calls == second_video.get_url_calls == 1


@pytest.mark.asyncio
async def test_video_url_is_validated_and_failures_are_sanitized() -> None:
    secret = "https://user:provider-secret@video.example/private.m3u8"
    for result in (_Video(secret), RuntimeError(secret), None):
        handler = _HandlerFake(videos=[result])
        resolver = TidalSourceResolver(handler)

        with pytest.raises(SourceResolutionError) as caught:
            await resolver.resolve(SourceReference(SourceKind.TIDAL_VIDEO, "123"))

        assert secret not in _error_surface(caught.value)
        assert caught.value.__cause__ is None
        assert caught.value.__context__ is None


@pytest.mark.asyncio
async def test_tidal_resolver_close_does_not_close_shared_handler() -> None:
    handler = _HandlerFake()
    resolver = TidalSourceResolver(handler)

    await resolver.close()
    await resolver.close()

    assert handler.unload_calls == 0


@pytest.mark.asyncio
async def test_composite_dispatches_and_owns_only_youtube_lifecycle() -> None:
    handler = _HandlerFake(
        tracks=[_DirectTrack("https://audio.example/track")],
        videos=[_Video("https://video.example/video.m3u8")],
    )
    tidal = TidalSourceResolver(handler)
    youtube = _YouTubeFake()
    resolver = CompositeSourceResolver(tidal, youtube)

    tidal_track = await resolver.resolve(SourceReference(SourceKind.TIDAL, "123"))
    tidal_video = await resolver.resolve(SourceReference(SourceKind.TIDAL_VIDEO, "123"))
    youtube_source = await resolver.resolve(
        SourceReference(SourceKind.YOUTUBE, "dQw4w9WgXcQ")
    )
    await resolver.close()
    await resolver.close()

    assert tidal_track.url == "https://audio.example/track"
    assert tidal_video.url == "https://video.example/video.m3u8"
    assert youtube_source.url == "https://youtube.example/fresh"
    assert youtube.references == [SourceReference(SourceKind.YOUTUBE, "dQw4w9WgXcQ")]
    assert youtube.close_calls == 1
    assert handler.unload_calls == 0
    with pytest.raises(PlaybackUnavailable):
        await resolver.resolve(SourceReference(SourceKind.TIDAL, "123"))


@pytest.mark.asyncio
async def test_composite_preserves_youtube_cancellation() -> None:
    started = asyncio.Event()

    class YouTube(_YouTubeFake):
        async def resolve(self, reference: SourceReference) -> ResolvedSource:
            del reference
            started.set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    resolver = CompositeSourceResolver(TidalSourceResolver(_HandlerFake()), YouTube())
    task = asyncio.create_task(
        resolver.resolve(SourceReference(SourceKind.YOUTUBE, "dQw4w9WgXcQ"))
    )
    await started.wait()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_actual_handler_fallback_calls_manifest_get_urls(cog) -> None:
    manifest = _BTSManifest(["https://audio.example/current-shape.flac"])
    track = _DirectTrack(RuntimeError("direct unavailable"), stream=_Stream(manifest))

    with patch.object(type(cog.tidal), "get_track", new=AsyncMock(return_value=track)):
        result = await cog.tidal.get_stream_url(SimpleNamespace(id=123))

    assert result == "https://audio.example/current-shape.flac"
    assert track.get_stream_calls == 1


@pytest.mark.asyncio
async def test_actual_handler_keeps_cancelled_provider_work_bounded(cog) -> None:
    started = threading.Event()
    release = threading.Event()
    lock = threading.Lock()
    active = 0
    peak = 0

    class BlockingTrack:
        duration = 180

        def get_url(self) -> str:
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
                if active == 4:
                    started.set()
            release.wait(timeout=2)
            with lock:
                active -= 1
            return "https://audio.example/bounded"

    async def get_track(_handler: object, track_id: str) -> object:
        del track_id
        return BlockingTrack()

    resolver = TidalSourceResolver(cog.tidal)
    with patch.object(type(cog.tidal), "get_track", new=get_track):
        tasks = [
            asyncio.create_task(
                resolver.resolve(SourceReference(SourceKind.TIDAL, str(index)))
            )
            for index in range(1, 7)
        ]
        await asyncio.wait_for(asyncio.to_thread(started.wait, 1), timeout=2)
        tasks[0].cancel()
        await asyncio.sleep(0.05)
        try:
            assert peak == 4
            assert active == 4
            with pytest.raises(asyncio.CancelledError):
                await tasks[0]
        finally:
            release.set()
            await asyncio.gather(*tasks, return_exceptions=True)
