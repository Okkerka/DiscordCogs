"""Lazy, sanitized source resolution for TIDAL tracks and videos."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from typing import Any, Protocol

from ..playback.errors import PlaybackUnavailable, SourceResolutionError
from ..playback.interfaces import SourceResolver
from ..playback.models import ResolvedSource, SourceKind, SourceReference

_FAILED = object()
_PROVIDER_TIMEOUT = 20.0
_KNOWN_CODECS = {
    "aac": "aac",
    "alac": "alac",
    "flac": "flac",
    "mp3": "mp3",
    "mp4a": "aac",
}


class TidalHandler(Protocol):
    """The bounded subset of the cog-owned TIDAL handler used here."""

    api_semaphore: asyncio.Semaphore

    async def get_track(self, track_id: str) -> object | None: ...

    async def get_video(self, video_id: str) -> object | None: ...

    async def _run_with_backoff(
        self, func: Callable[[], object], timeout: float = 10.0
    ) -> object: ...


def _safe_attribute(value: object, name: str) -> object:
    try:
        return getattr(value, name)
    except Exception:  # noqa: BLE001 - provider models are untrusted
        return _FAILED


def _positive_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def _codec_hint(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    return _KNOWN_CODECS.get(value.strip().casefold())


def _manifest_details(track: Any) -> tuple[tuple[object, ...], object, object]:
    stream = track.get_stream()
    manifest = stream.get_stream_manifest()
    if manifest.is_mpd is not False or manifest.is_bts is not True:
        raise ValueError
    encryption_type = manifest.encryption_type
    encryption_key = manifest.encryption_key
    is_encrypted = manifest.is_encrypted
    if (
        not isinstance(encryption_type, str)
        or encryption_type.strip().casefold() != "none"
        or encryption_key not in (None, "")
        or is_encrypted is not False
    ):
        raise ValueError
    urls = manifest.get_urls()
    if not isinstance(urls, Sequence) or isinstance(urls, (str, bytes, bytearray)):
        raise TypeError
    codec = getattr(manifest, "codecs", None)
    sample_rate = getattr(manifest, "sample_rate", getattr(stream, "sample_rate", None))
    return tuple(urls), codec, sample_rate


class TidalSourceResolver:
    """Resolve fresh TIDAL media without owning the shared handler lifecycle."""

    def __init__(self, handler: TidalHandler) -> None:
        self._handler = handler

    async def _catalog_call(self, operation: Callable[[], Any]) -> object:
        try:
            return await operation()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - provider failures are deliberately discarded
            return _FAILED

    async def _provider_call(self, operation: Callable[[], object]) -> object:
        try:
            async with self._handler.api_semaphore:
                return await self._handler._run_with_backoff(
                    operation, timeout=_PROVIDER_TIMEOUT
                )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - provider failures are deliberately discarded
            return _FAILED

    @staticmethod
    def _source(
        url: object,
        *,
        codec: object = None,
        sample_rate: object = None,
        duration: object = None,
    ) -> ResolvedSource | None:
        if not isinstance(url, str):
            return None
        try:
            return ResolvedSource(
                url,
                {},
                codec=_codec_hint(codec),
                sample_rate=_positive_int(sample_rate),
                duration=_positive_int(duration),
            )
        except Exception:  # noqa: BLE001 - malformed provider values are discarded
            return None

    async def _resolve_track(self, reference: SourceReference) -> ResolvedSource | None:
        track = await self._catalog_call(
            lambda: self._handler.get_track(reference.identifier)
        )
        if track is _FAILED or track is None:
            return None

        duration = _safe_attribute(track, "duration")
        codec = _safe_attribute(track, "codec")
        get_url = _safe_attribute(track, "get_url")
        if callable(get_url):
            url = await self._provider_call(get_url)
            if url is not _FAILED:
                return self._source(url, codec=codec, duration=duration)

        details = await self._provider_call(lambda: _manifest_details(track))
        if details is _FAILED or not isinstance(details, tuple) or len(details) != 3:
            return None
        urls, manifest_codec, sample_rate = details
        if not isinstance(urls, tuple):
            return None
        for url in urls:
            source = self._source(
                url,
                codec=manifest_codec,
                sample_rate=sample_rate,
                duration=duration,
            )
            if source is not None:
                return source
        return None

    async def _resolve_video(self, reference: SourceReference) -> ResolvedSource | None:
        video = await self._catalog_call(
            lambda: self._handler.get_video(reference.identifier)
        )
        if video is _FAILED or video is None:
            return None
        get_url = _safe_attribute(video, "get_url")
        if not callable(get_url):
            return None
        url = await self._provider_call(get_url)
        if url is _FAILED:
            return None
        return self._source(url, duration=_safe_attribute(video, "duration"))

    async def resolve(self, reference: SourceReference) -> ResolvedSource:
        """Fetch the current provider object and a new signed media URL."""

        if not isinstance(reference, SourceReference):
            raise SourceResolutionError()
        if reference.kind is SourceKind.TIDAL:
            source = await self._resolve_track(reference)
        elif reference.kind is SourceKind.TIDAL_VIDEO:
            source = await self._resolve_video(reference)
        else:
            source = None
        if source is None:
            raise SourceResolutionError()
        return source

    async def close(self) -> None:
        """Leave the shared cog-owned TIDAL handler open."""


class CompositeSourceResolver:
    """Dispatch provider references and own only the YouTube resolver."""

    def __init__(self, tidal: TidalSourceResolver, youtube: SourceResolver) -> None:
        self._tidal = tidal
        self._youtube = youtube
        self._closed = False

    async def resolve(self, reference: SourceReference) -> ResolvedSource:
        if self._closed:
            raise PlaybackUnavailable()
        if not isinstance(reference, SourceReference):
            raise SourceResolutionError()
        if reference.kind in (SourceKind.TIDAL, SourceKind.TIDAL_VIDEO):
            return await self._tidal.resolve(reference)
        if reference.kind is SourceKind.YOUTUBE:
            return await self._youtube.resolve(reference)
        raise SourceResolutionError()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._youtube.close()


__all__ = ["CompositeSourceResolver", "TidalSourceResolver"]
