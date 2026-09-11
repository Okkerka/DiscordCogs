"""Bounded metadata-only provider search through the shared extractor worker."""

from __future__ import annotations

import asyncio
import json
import unicodedata
from collections.abc import Mapping
from typing import Literal

from ..domain.models import TrackMeta
from ..playback.errors import SourceResolutionError
from ..playback.models import SourceKind, SourceReference
from .public_audio import PublicAudioResolver
from .youtube_resolver import (
    YouTubeResolver,
    _metadata_from_mapping,
    _validated_dependency_root,
)

_SEARCH_DEADLINE = 30.0
_SEARCH_OUTPUT_LIMIT = 16 * 1024
_YOUTUBE_TEMPLATE = (
    '{"id":%(id)j,"title":%(title)j,"uploader":%(uploader)j,"channel":%(channel)j,'
    '"duration":%(duration)j,"thumbnail":%(thumbnail)j,"availability":%(availability)j,'
    '"has_drm":%(has_drm)j}'
)
_SOUNDCLOUD_TEMPLATE = (
    '{"webpage_url":%(webpage_url)j,"url":%(url)j,"title":%(title)j,"artist":%(artist)j,'
    '"uploader":%(uploader)j,"duration":%(duration)j,"thumbnail":%(thumbnail)j,'
    '"album":%(album)j,"availability":%(availability)j,"has_drm":%(has_drm)j}'
)


def _validated_query(query: object) -> str:
    if not isinstance(query, str):
        raise TypeError
    query = query.strip()
    if not query or len(query) > 300 or any(unicodedata.category(char) == "Cc" for char in query):
        raise ValueError
    return query


def _search_args(worker: YouTubeResolver, query: str, platform: Literal["youtube", "soundcloud"]) -> list[str]:
    root = _validated_dependency_root(worker._yt_dlp_locator())
    args = worker._common_args("", root)
    index = args.index("--js-runtimes")
    del args[index:index + 2]
    if platform == "youtube":
        extractor = "^youtube:search$,^youtube$"
        template = _YOUTUBE_TEMPLATE
        target = f"ytsearch1:{query}"
    else:
        extractor = "^soundcloud:search$,^soundcloud$"
        template = _SOUNDCLOUD_TEMPLATE
        target = f"scsearch1:{query}"
    return args + [
        "--use-extractors",
        extractor,
        "--yes-playlist",
        "--flat-playlist",
        "--lazy-playlist",
        "--playlist-end",
        "1",
        "--print",
        template,
        "--",
        target,
    ]


def _one_document(output: bytes) -> object | None:
    text = output.decode("utf-8")
    documents = [line for line in text.splitlines() if line.strip()]
    if not documents:
        return None
    if len(documents) != 1:
        raise ValueError
    return json.loads(documents[0])


def _public_youtube(payload: object) -> None:
    if not isinstance(payload, Mapping):
        raise TypeError
    availability = payload.get("availability")
    if availability not in (None, "public", "unlisted") or payload.get("has_drm") is True:
        raise ValueError


def _youtube_result(payload: object) -> tuple[SourceReference, TrackMeta]:
    _public_youtube(payload)
    metadata = _metadata_from_mapping(payload)
    reference = SourceReference(SourceKind.YOUTUBE, metadata.video_id)
    return reference, {
        "title": metadata.title,
        "artist": metadata.channel or "YouTube",
        "album": None,
        "duration": metadata.duration or 0,
        "quality": "YouTube audio",
        "image": metadata.thumbnail,
        "share_url": f"https://www.youtube.com/watch?v={metadata.video_id}",
        "audio_resolution": None,
        "track_id": None,
        "source": "YouTube",
    }


def _soundcloud_result(payload: object) -> tuple[SourceReference, TrackMeta]:
    metadata = PublicAudioResolver._metadata(payload, SourceKind.SOUNDCLOUD)
    return metadata.reference, metadata.meta


async def search_provider(
    worker: YouTubeResolver,
    query: str,
    platform: Literal["youtube", "soundcloud"],
) -> tuple[SourceReference, TrackMeta] | None:
    """Return one safe, metadata-only search result from YouTube or SoundCloud."""
    try:
        if platform not in ("youtube", "soundcloud"):
            raise ValueError
        query = _validated_query(query)
        args = _search_args(worker, query, platform)
        output = await worker._run_child(args, deadline=_SEARCH_DEADLINE, ceiling=_SEARCH_OUTPUT_LIMIT)
        payload = _one_document(output)
        if payload is None:
            return None
        if platform == "youtube":
            return _youtube_result(payload)
        return _soundcloud_result(payload)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - subprocess and provider details are untrusted
        raise SourceResolutionError() from None
