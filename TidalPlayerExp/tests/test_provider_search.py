"""Bounded provider-search behavior at the shared yt-dlp worker boundary."""

from __future__ import annotations

import asyncio
import json

import pytest

from TidalPlayerExp.playback import SourceKind, SourceResolutionError
from TidalPlayerExp.providers.search import search_provider
from TidalPlayerExp.providers.youtube_resolver import YouTubeResolver

VIDEO_ID = "dQw4w9WgXcQ"
SOUNDCLOUD_URL = "https://soundcloud.com/artist/recording"


def _worker(tmp_path: pytest.TempPathFactory) -> YouTubeResolver:
    package = tmp_path / "yt_dlp"
    package.mkdir()
    (package / "__main__.py").write_text("", encoding="utf-8")
    return YouTubeResolver(yt_dlp_locator=lambda: str(tmp_path))


def _youtube(**changes: object) -> dict[str, object]:
    return {
        "id": VIDEO_ID,
        "title": "A video",
        "uploader": "A channel",
        "duration": 143.2,
        "thumbnail": "https://images.example.test/cover.jpg",
        "availability": "public",
        "has_drm": False,
        **changes,
    }


def _soundcloud(**changes: object) -> dict[str, object]:
    return {
        "webpage_url": SOUNDCLOUD_URL,
        "url": "https://cdn.example.test/private-media-url",
        "title": "A recording",
        "artist": "Artist",
        "uploader": "Uploader",
        "duration": 143.2,
        "thumbnail": "https://images.example.test/cover.jpg",
        "album": None,
        "availability": "public",
        "has_drm": False,
        **changes,
    }


@pytest.mark.asyncio
async def test_youtube_search_queues_stable_metadata_and_only_a_bounded_projected_search(tmp_path, monkeypatch):
    """Removing flat-search limits or allowing a media URL into queue metadata breaks this."""
    worker = _worker(tmp_path)
    calls: list[tuple[list[str], float, int]] = []

    async def run_child(args: list[str], *, deadline: float, ceiling: int) -> bytes:
        calls.append((args, deadline, ceiling))
        return json.dumps(_youtube()).encode()

    monkeypatch.setattr(worker, "_run_child", run_child)
    try:
        result = await search_provider(worker, "  a song  ", "youtube")
        assert result is not None
        reference, meta = result
        assert reference.kind is SourceKind.YOUTUBE and reference.identifier == VIDEO_ID
        assert meta == {
            "title": "A video", "artist": "A channel", "album": None, "duration": 143,
            "quality": "YouTube audio", "image": "https://images.example.test/cover.jpg",
            "share_url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            "audio_resolution": None, "track_id": None, "source": "YouTube",
        }
        args, deadline, ceiling = calls.pop()
        assert deadline == 30.0 and ceiling == 16 * 1024
        assert args[args.index("--use-extractors") + 1] == "^youtube:search$,^youtube$"
        assert args[args.index("--playlist-end") + 1] == "1"
        assert {"--yes-playlist", "--flat-playlist", "--lazy-playlist", "--no-js-runtimes", "--no-download"} <= set(args)
        assert "--js-runtimes" not in args and "--format" not in args
        assert args[-2:] == ["--", "ytsearch1:a song"]
        projection = args[args.index("--print") + 1]
        assert "%(id)j" in projection and "%(url)j" not in projection
    finally:
        await worker.close()


@pytest.mark.asyncio
async def test_soundcloud_search_returns_only_a_canonical_public_track(tmp_path, monkeypatch):
    """Accepting arbitrary hosts or private SoundCloud results would enqueue unsafe references."""
    worker = _worker(tmp_path)
    calls: list[list[str]] = []

    async def run_child(args: list[str], *, deadline: float, ceiling: int) -> bytes:
        calls.append(args)
        assert deadline == 30.0 and ceiling == 16 * 1024
        return json.dumps(_soundcloud()).encode()

    monkeypatch.setattr(worker, "_run_child", run_child)
    try:
        result = await search_provider(worker, "recording", "soundcloud")
        assert result is not None
        reference, meta = result
        assert reference.kind is SourceKind.SOUNDCLOUD and reference.identifier == SOUNDCLOUD_URL
        assert meta["share_url"] == SOUNDCLOUD_URL and meta["source"] == "soundcloud"
        assert "private-media-url" not in repr(meta)
        args = calls.pop()
        assert args[args.index("--use-extractors") + 1] == "^soundcloud:search$,^soundcloud$"
        assert args[-2:] == ["--", "scsearch1:recording"]
        projection = args[args.index("--print") + 1]
        assert "%(webpage_url)j" in projection and "%(url)j" in projection
    finally:
        await worker.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "platform,query",
    [
        ("youtube", ""), ("youtube", " \t "), ("youtube", "x" * 301),
        ("youtube", "song\nnext"), ("youtube", "song\x7fnext"), ("tidal", "song"),
    ],
)
async def test_invalid_platform_or_query_never_starts_a_worker_request(tmp_path, monkeypatch, platform, query):
    """Weak validation would permit unbounded or option-like extractor input."""
    worker = _worker(tmp_path)
    started = False

    async def run_child(*_args, **_kwargs) -> bytes:
        nonlocal started
        started = True
        return b"{}"

    monkeypatch.setattr(worker, "_run_child", run_child)
    try:
        with pytest.raises(SourceResolutionError):
            await search_provider(worker, query, platform)  # type: ignore[arg-type]
        assert not started
    finally:
        await worker.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "platform,payload",
    [
        ("youtube", b""),
        ("youtube", b"{}"),
        ("youtube", b"{}\n{}"),
        ("soundcloud", json.dumps(_soundcloud(webpage_url="https://evil.example.test/artist/recording")).encode()),
        ("soundcloud", json.dumps(_soundcloud(availability="private")).encode()),
        ("soundcloud", json.dumps(_soundcloud(has_drm=True)).encode()),
    ],
)
async def test_empty_multiple_or_untrusted_provider_documents_are_not_queued(tmp_path, monkeypatch, platform, payload):
    """Malformed or non-public search results must not become queue references."""
    worker = _worker(tmp_path)

    async def run_child(*_args, **_kwargs) -> bytes:
        return payload

    monkeypatch.setattr(worker, "_run_child", run_child)
    try:
        if payload == b"":
            assert await search_provider(worker, "song", platform) is None
        else:
            with pytest.raises(SourceResolutionError):
                await search_provider(worker, "song", platform)
    finally:
        await worker.close()


@pytest.mark.asyncio
async def test_search_propagates_cancellation_from_the_shared_worker(tmp_path, monkeypatch):
    """Converting cancellation to a resolution error would strand request cleanup."""
    worker = _worker(tmp_path)
    started = asyncio.Event()

    async def run_child(*_args, **_kwargs) -> bytes:
        started.set()
        await asyncio.Future[bytes]()

    monkeypatch.setattr(worker, "_run_child", run_child)
    try:
        task = asyncio.create_task(search_provider(worker, "song", "youtube"))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        await worker.close()
