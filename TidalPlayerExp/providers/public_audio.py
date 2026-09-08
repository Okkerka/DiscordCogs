"""Public SoundCloud/Bandcamp audio through the shared isolated extractor."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlsplit

from ..domain.models import TrackMeta
from ..domain.public_audio_urls import canonical_public_audio_url, parse_public_audio_url
from ..playback.errors import SourceResolutionError
from ..playback.models import PlaybackEntry, ResolvedSource, SourceKind, SourceReference
from .youtube_resolver import (
    YouTubeResolver,
    _safe_display,
    _valid_https_url,
    _validated_dependency_root,
)

_KINDS = (SourceKind.SOUNDCLOUD, SourceKind.BANDCAMP)
_METADATA_TEMPLATE = (
    '{"webpage_url":%(webpage_url,url)j,"title":%(title)j,"artist":%(artist)j,'
    '"uploader":%(uploader)j,"duration":%(duration)j,"thumbnail":%(thumbnail)j,'
    '"album":%(album)j,"availability":%(availability)j}'
)
_SOURCE_TEMPLATE = (
    '{"webpage_url":%(webpage_url)j,"url":%(url)j,"http_headers":%(http_headers)j,'
    '"format_id":%(format_id)j,"acodec":%(acodec)j,"vcodec":%(vcodec)j,'
    '"asr":%(asr)j,"audio_channels":%(audio_channels)j,"duration":%(duration)j,'
    '"availability":%(availability)j,"has_drm":%(has_drm)j}'
)


def _optional(value: object) -> object:
    return None if value is None or value == "NA" else value


def _duration(value: object) -> int | None:
    value = _optional(value)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or value < 0:
        raise ValueError("Invalid duration")
    return max(1, int(value)) if value else None


def _public(payload: Mapping[str, object], *, shared: bool = False) -> None:
    availability = _optional(payload.get("availability"))
    allowed = (None, "public", "unlisted", "private") if shared else (None, "public", "unlisted")
    if availability not in allowed or payload.get("has_drm") is True:
        raise ValueError("Audio is not publicly playable")


@dataclass(frozen=True, slots=True)
class PublicAudioMetadata:
    """Stable public reference and detached, read-only display metadata."""

    reference: SourceReference
    meta: TrackMeta

    def __post_init__(self) -> None:
        if not isinstance(self.reference, SourceReference) or self.reference.kind not in _KINDS:
            raise ValueError("Invalid public audio reference")
        validated = PlaybackEntry("metadata", self.reference, None, self.meta, None)
        object.__setattr__(self, "meta", validated.meta)


class PublicAudioResolver:
    """Borrow one worker's process slots; no duplicate subprocess ownership."""

    def __init__(self, worker: YouTubeResolver) -> None:
        self._worker = worker

    @staticmethod
    def _reference(reference: SourceReference) -> None:
        if not isinstance(reference, SourceReference) or reference.kind not in _KINDS:
            raise SourceResolutionError()

    async def _extract(
        self, url: str, kind: SourceKind, *, media: bool = False, limit: int | None = None,
    ) -> bytes:
        output = None
        try:
            root = _validated_dependency_root(self._worker._yt_dlp_locator())
            args = self._worker._common_args("", root)
            # These extractors do not use a JS engine. Retain --no-js-runtimes
            # while removing the YouTube worker's explicit Deno opt-in.
            index = args.index("--js-runtimes")
            del args[index:index + 2]
            extractor = "^soundcloud$" if kind is SourceKind.SOUNDCLOUD else "^Bandcamp$"
            if limit is not None:
                extractor += ",^soundcloud:set$" if kind is SourceKind.SOUNDCLOUD else ",^Bandcamp:album$"
            args += ["--use-extractors", extractor]
            if limit is None:
                args += ["--no-playlist"]
            else:
                args += ["--yes-playlist", "--flat-playlist", "--lazy-playlist", "--playlist-end", str(limit)]
            if media:
                args += ["--format", "bestaudio[acodec!=none][vcodec=none]"]
            args += ["--print", _SOURCE_TEMPLATE if media else _METADATA_TEMPLATE, url]
            output = await self._worker._run_child(
                args, deadline=45.0 if limit is not None else 30.0,
                ceiling=limit * 4096 if limit is not None else 16 * 1024,
            )
        except Exception:  # noqa: BLE001 - discard subprocess/provider exception details
            output = None
        if output is None:
            raise SourceResolutionError()
        return output

    @staticmethod
    def _metadata(
        payload: object, kind: SourceKind, *, flat: bool = False, secret_token: str | None = None,
    ) -> PublicAudioMetadata:
        if not isinstance(payload, Mapping):
            raise TypeError("Invalid metadata")
        _public(payload, shared=kind is SourceKind.SOUNDCLOUD and secret_token is not None)
        raw_url = payload.get("webpage_url") or payload.get("url")
        if not isinstance(raw_url, str):
            raise TypeError("Invalid public audio URL")
        provider, content_type, url, returned_token = parse_public_audio_url(raw_url)
        if returned_token is not None and returned_token != secret_token:
            raise ValueError("Unexpected SoundCloud share token")
        if provider != kind.value or content_type != "track":
            raise ValueError("Unexpected public audio source")
        title = payload.get("title")
        if flat and kind is SourceKind.SOUNDCLOUD and _optional(title) is None:
            # SoundCloud sets expose URL-transparent entries without track titles.
            # Keep imports flat; the public slug is an honest display fallback.
            title = urlsplit(url).path.rsplit("/", 1)[-1].replace("-", " ").replace("_", " ")
        if not isinstance(title, str) or title.strip().casefold() in {"na", "[private track]", "[deleted track]"}:
            raise ValueError("Invalid metadata")
        artist = _optional(payload.get("artist")) or _optional(payload.get("uploader"))
        if artist is None:
            parts = urlsplit(url)
            artist = parts.path.split("/")[1] if kind is SourceKind.SOUNDCLOUD else parts.netloc.split(".")[0]
        if not isinstance(artist, str):
            raise TypeError("Invalid artist")
        album = _optional(payload.get("album"))
        if album is not None and not isinstance(album, str):
            raise ValueError("Invalid album")
        thumbnail = _optional(payload.get("thumbnail"))

        def display(value: str, limit: int) -> str:
            # A provider must not echo a credential into public text fields.
            return _safe_display(value.replace(secret_token, "[redacted]") if secret_token else value, limit)

        if secret_token and isinstance(thumbnail, str) and secret_token in thumbnail:
            thumbnail = None
        meta: TrackMeta = {
            "title": display(title, 200), "artist": display(artist, 100),
            "album": display(album, 200) if album else None,
            "duration": _duration(payload.get("duration")) or 0,
            "quality": "Shared audio" if secret_token else "Public audio", "audio_resolution": None, "track_id": None,
            "image": _valid_https_url(thumbnail, limit=2048) if thumbnail else None,
            "share_url": None if secret_token else url, "source": kind.value,
        }
        return PublicAudioMetadata(SourceReference(kind, url, secret_token=secret_token), meta)

    @staticmethod
    def _request_url(reference: SourceReference) -> str:
        if reference.secret_token is not None:
            return f"{reference.identifier}/{reference.secret_token}"
        return reference.identifier

    async def fetch_metadata(self, reference: SourceReference) -> PublicAudioMetadata:
        """Read display data without retaining or queueing a signed media URL."""
        self._reference(reference)
        output = await self._extract(self._request_url(reference), reference.kind)
        result = None
        try:
            result = self._metadata(
                self._worker._parse_document(output), reference.kind, secret_token=reference.secret_token,
            )
            if result.reference != reference:
                result = None
        except Exception:  # noqa: BLE001 - metadata is an untrusted provider response
            result = None
        if result is None:
            raise SourceResolutionError()
        return result

    async def resolve(self, reference: SourceReference) -> ResolvedSource:
        """Resolve only a public, full-length audio format at playback time."""
        self._reference(reference)
        output = await self._extract(self._request_url(reference), reference.kind, media=True)
        result = None
        try:
            payload = self._worker._parse_document(output)
            if not isinstance(payload, dict):
                raise TypeError("Invalid source")
            _public(payload, shared=reference.kind is SourceKind.SOUNDCLOUD and reference.secret_token is not None)
            raw_url = payload.get("webpage_url")
            if not isinstance(raw_url, str):
                raise TypeError("Invalid public audio URL")
            provider, content_type, url, returned_token = parse_public_audio_url(raw_url)
            if returned_token is not None and returned_token != reference.secret_token:
                raise ValueError("Unexpected SoundCloud share token")
            if provider != reference.kind.value or content_type != "track" or url != reference.identifier:
                raise ValueError("Unexpected public audio source")
            format_id = payload.get("format_id")
            if not isinstance(format_id, str) or "preview" in format_id.casefold():
                raise ValueError("Preview audio is not supported")
            payload["duration"] = _duration(payload.get("duration"))
            result = self._worker._source_from_mapping(payload)
        except Exception:  # noqa: BLE001 - never retain media URL-bearing exception context
            result = None
        if result is None:
            raise SourceResolutionError()
        return result

    async def fetch_collection(self, url: str, limit: int = 100) -> tuple[PublicAudioMetadata, ...]:
        """Read at most 100 flat entries in source order, without audio resolution."""
        validated = None
        try:
            provider, content_type, canonical = canonical_public_audio_url(url)
            if content_type not in {"album", "playlist"}:
                raise ValueError("Not a collection")
            if isinstance(limit, bool) or not isinstance(limit, int) or not 0 < limit <= 100:
                raise ValueError("Invalid collection limit")
            validated = SourceKind(provider), canonical
        except (TypeError, ValueError):
            pass
        if validated is None:
            raise SourceResolutionError()
        kind, canonical = validated
        output = await self._extract(canonical, kind, limit=limit)
        lines = None
        try:
            lines = [line for line in output.decode("utf-8").splitlines() if line.strip()]
        except UnicodeDecodeError:
            pass
        if lines is None or len(lines) > limit:
            raise SourceResolutionError()
        items: dict[str, PublicAudioMetadata] = {}
        for line in lines:
            try:
                metadata = self._metadata(json.loads(line), kind, flat=True)
            except (TypeError, ValueError):
                continue
            items.setdefault(metadata.reference.identifier, metadata)
        return tuple(items.values())

    async def close(self) -> None:
        """The composite resolver, not this adapter, owns the shared worker."""
