"""YouTube admission keeps stable references and delegates playback to the session."""

from __future__ import annotations

import asyncio
import importlib
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from TidalPlayerExp.playback.models import SourceKind, SourceReference
from TidalPlayerExp.providers.youtube_resolver import YouTubeVideoMetadata
from TidalPlayerExp.ui.embeds import Messages

VIDEO_ID = "dQw4w9WgXcQ"
CANONICAL_URL = f"https://www.youtube.com/watch?v={VIDEO_ID}"


def _context(guild_id: int = 71):
    channel = SimpleNamespace(
        id=22, permissions_for=lambda _member: SimpleNamespace(connect=True, speak=True),
    )
    return SimpleNamespace(
        guild=SimpleNamespace(id=guild_id, me=SimpleNamespace(), voice_client=None),
        author=SimpleNamespace(id=5, voice=SimpleNamespace(channel=channel)),
        channel=channel,
        defer=AsyncMock(),
        send=AsyncMock(return_value=SimpleNamespace(
            id=80, guild=SimpleNamespace(id=guild_id), edit=AsyncMock(), delete=AsyncMock(),
        )),
    )


def _candidate(title: str = "Rivals", artist: str = "AZALI"):
    return SimpleNamespace(
        id=269931027, name=title, full_name=title,
        artist=SimpleNamespace(name=artist), album=None, duration=243,
        audio_quality="HI_RES_LOSSLESS",
    )


def _video(video_id: str = VIDEO_ID, title: str = "AZALI - Rivals"):
    return YouTubeVideoMetadata(
        video_id, title, "AZALI", 243,
        f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg",
    )


def _api_payload(title="AZALI - Rivals (Official Audio)", channel="AZALI - Topic"):
    return {"items": [{"snippet": {
        "title": title, "channelTitle": channel,
        "thumbnails": {"high": {"url": f"https://i.ytimg.com/vi/{VIDEO_ID}/hqdefault.jpg"}},
    }, "contentDetails": {"duration": "PT4M3S"}}]}


def _youtube_client(payload):
    execute = Mock(return_value=payload)
    listing = Mock(return_value=SimpleNamespace(execute=execute))
    return SimpleNamespace(videos=lambda: SimpleNamespace(list=listing))


async def _run_blocking(_handler, operation, **_kwargs):
    return operation()


@pytest.fixture
def youtube_ctx(cog, native_session, monkeypatch):
    cog.yt = None
    cog._initialized = True
    monkeypatch.setattr(type(cog.tidal), "_run_blocking", _run_blocking)
    monkeypatch.setattr(type(cog.tidal), "is_logged_in", AsyncMock(return_value=False))
    monkeypatch.setattr(type(cog.tidal), "search", AsyncMock(return_value=[]))
    cog.youtube_resolver.fetch_metadata = AsyncMock(return_value=_video())
    cog.youtube_resolver.resolve = AsyncMock(side_effect=AssertionError("Admission must not resolve media"))
    return _context()


@pytest.mark.asyncio
async def test_keyless_unauthenticated_video_admits_youtube_once(cog, youtube_ctx, native_session):
    await cog._handle_youtube_video(youtube_ctx, VIDEO_ID)

    cog.youtube_resolver.fetch_metadata.assert_awaited_once_with(SourceReference(SourceKind.YOUTUBE, VIDEO_ID))
    cog.tidal.search.assert_not_awaited()
    native_session.enqueue.assert_awaited_once()
    entry = native_session.entries[0]
    assert entry.primary == SourceReference(SourceKind.YOUTUBE, VIDEO_ID)
    assert entry.fallback is None
    assert entry.meta["source"] == "YouTube"
    assert entry.meta["duration"] == 243
    assert entry.meta["share_url"] == CANONICAL_URL
    assert entry.meta["image"].endswith("hqdefault.jpg")
    assert entry.requester_id == youtube_ctx.author.id
    cog.youtube_resolver.resolve.assert_not_awaited()
    assert not cog._current_meta
    youtube_ctx.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_confident_match_preserves_original_youtube_fallback_metadata(cog, youtube_ctx, native_session):
    cog.yt = _youtube_client(_api_payload())
    cog.tidal.is_logged_in.return_value = True
    cog.tidal.search.return_value = [_candidate()]

    await cog._handle_youtube_video(youtube_ctx, VIDEO_ID)

    entry = native_session.entries[0]
    assert entry.primary == SourceReference(SourceKind.TIDAL, "269931027")
    assert entry.meta["title"] == "Rivals"
    assert entry.fallback == SourceReference(SourceKind.YOUTUBE, VIDEO_ID)
    assert entry.fallback_meta["title"] == "AZALI - Rivals (Official Audio)"
    assert entry.fallback_meta["artist"] == "AZALI - Topic"
    assert entry.fallback_meta["share_url"] == CANONICAL_URL
    assert entry.fallback_meta["source"] == "YouTube"
    assert entry.fallback_meta["track_id"] is None
    assert entry.fallback_meta["audio_resolution"] is None
    assert entry.fallback_meta["duration"] == 243
    cog.youtube_resolver.fetch_metadata.assert_not_awaited()
    cog.youtube_resolver.resolve.assert_not_awaited()
    native_session.enqueue.assert_awaited_once()
    assert not cog._current_meta
    youtube_ctx.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_api_duration_is_requested_and_retained_without_second_extraction(cog, youtube_ctx, native_session):
    cog.yt = _youtube_client(_api_payload())

    await cog._handle_youtube_video(youtube_ctx, VIDEO_ID)

    assert native_session.entries[0].meta["duration"] == 243
    cog.yt.videos().list.assert_called_once_with(part="snippet,contentDetails", id=VIDEO_ID, maxResults=1)
    cog.youtube_resolver.fetch_metadata.assert_not_awaited()


@pytest.mark.asyncio
async def test_nonmusic_youtube_video_keeps_original_when_catalog_results_are_unrelated(
    cog, youtube_ctx, native_session,
):
    cog.youtube_resolver.fetch_metadata.return_value = _video(title="An Autumn Flip is Coming to Europe...")
    cog.tidal.is_logged_in.return_value = True
    cog.tidal.search.return_value = [_candidate(title="Autumn", artist="Another Artist")]

    await cog._handle_youtube_video(youtube_ctx, VIDEO_ID)

    entry = native_session.entries[0]
    assert entry.primary == SourceReference(SourceKind.YOUTUBE, VIDEO_ID)
    assert entry.fallback is None
    assert entry.meta["title"] == "An Autumn Flip is Coming to Europe..."


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [
    {}, {"items": [{}]}, _api_payload(title={"untrusted": "object"}),
    _api_payload(channel=["invalid"]), _api_payload(title="[Private video]"),
])
async def test_missing_or_malformed_api_metadata_uses_extractor_then_prefers_tidal(
    cog, youtube_ctx, native_session, payload,
):
    cog.yt = _youtube_client(payload)
    cog.tidal.is_logged_in.return_value = True
    cog.tidal.search.return_value = [_candidate()]

    await cog._handle_youtube_video(youtube_ctx, VIDEO_ID)

    cog.youtube_resolver.fetch_metadata.assert_awaited_once()
    assert native_session.entries[0].primary.kind is SourceKind.TIDAL
    assert native_session.entries[0].fallback_meta["title"] == "AZALI - Rivals"
    cog.youtube_resolver.resolve.assert_not_awaited()


@pytest.mark.asyncio
async def test_api_failure_falls_back_without_exposing_exception(cog, youtube_ctx, native_session, caplog):
    cog.yt = _youtube_client(_api_payload())
    cog.yt.videos().list().execute.side_effect = RuntimeError("api-key-and-request-url")
    caplog.set_level(logging.WARNING, logger="red.tidalplayerexp")

    await cog._handle_youtube_video(youtube_ctx, VIDEO_ID)

    assert "api-key-and-request-url" not in caplog.text
    cog.youtube_resolver.fetch_metadata.assert_awaited_once()
    assert native_session.entries[0].primary.kind is SourceKind.YOUTUBE


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["no_match", "search", "metadata", "reference"])
async def test_optional_tidal_failure_keeps_original_youtube(cog, youtube_ctx, native_session, failure):
    cog.tidal.is_logged_in.return_value = True
    candidate = _candidate()
    cog.tidal.search.return_value = [] if failure == "no_match" else [candidate]
    if failure == "search":
        cog.tidal.search.side_effect = RuntimeError("catalog unavailable")
    elif failure == "metadata":
        cog._extract_meta = AsyncMock(side_effect=ValueError("malformed metadata"))
    elif failure == "reference":
        candidate.id = "invalid"

    await cog._handle_youtube_video(youtube_ctx, VIDEO_ID)

    assert len(native_session.entries) == 1
    assert native_session.entries[0].primary == SourceReference(SourceKind.YOUTUBE, VIDEO_ID)
    assert native_session.entries[0].meta["share_url"] == CANONICAL_URL
    cog.youtube_resolver.resolve.assert_not_awaited()
    youtube_ctx.send.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("unavailable_title", [None, "[Private video]", "[Deleted video]"])
async def test_missing_metadata_sends_one_safe_error(cog, youtube_ctx, native_session, caplog, unavailable_title):
    secret = "signed-stream-url-secret"
    if unavailable_title is None:
        cog.youtube_resolver.fetch_metadata.side_effect = RuntimeError(secret)
    else:
        cog.youtube_resolver.fetch_metadata.return_value = _video(title=unavailable_title)
    caplog.set_level(logging.WARNING, logger="red.tidalplayerexp")

    await cog._handle_youtube_video(youtube_ctx, VIDEO_ID)

    native_session.enqueue.assert_not_awaited()
    youtube_ctx.send.assert_awaited_once()
    assert youtube_ctx.send.await_args.kwargs["embed"].description == Messages.ERROR_YOUTUBE_FAILED
    assert secret not in caplog.text
    cog.youtube_resolver.resolve.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["api", "metadata", "search", "enqueue"])
async def test_cancellation_propagates_without_error(cog, youtube_ctx, native_session, monkeypatch, stage):
    if stage == "api":
        cog.yt = _youtube_client(_api_payload())
        monkeypatch.setattr(type(cog.tidal), "_run_blocking", AsyncMock(side_effect=asyncio.CancelledError))
    elif stage == "metadata":
        cog.youtube_resolver.fetch_metadata.side_effect = asyncio.CancelledError
    elif stage == "search":
        cog.tidal.is_logged_in.return_value = True
        cog.tidal.search.side_effect = asyncio.CancelledError
    else:
        native_session.enqueue.side_effect = asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await cog._handle_youtube_video(youtube_ctx, VIDEO_ID)

    assert not native_session.entries
    assert not cog._current_meta
    youtube_ctx.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_invalid_voice_is_rejected_before_metadata(cog, youtube_ctx, native_session):
    youtube_ctx.author.voice = None
    await cog._handle_youtube_video(youtube_ctx, VIDEO_ID)
    cog.youtube_resolver.fetch_metadata.assert_not_awaited()
    native_session.enqueue.assert_not_awaited()
    assert "Join a voice channel" in youtube_ctx.send.await_args.kwargs["embed"].description


@pytest.mark.asyncio
@pytest.mark.parametrize("video_id", ["--exec=untrusted", "https://evil.invalid/", "short"])
async def test_invalid_id_is_rejected_before_extraction(cog, youtube_ctx, native_session, video_id):
    await cog._handle_youtube_video(youtube_ctx, video_id)
    cog.youtube_resolver.fetch_metadata.assert_not_awaited()
    native_session.enqueue.assert_not_awaited()
    youtube_ctx.send.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("url,playlist", [
    (f"https://youtu.be/{VIDEO_ID}", False),
    (f"https://youtu.be/{VIDEO_ID}?list=PLexample", False),
    (f"{CANONICAL_URL}&list=PLexample", False),
    ("https://www.youtube.com/playlist?list=PLexample", True),
])
async def test_tplay_routes_explicit_playlist_and_video_links_without_tidal_auth(cog, youtube_ctx, url, playlist):
    cog.check_ready = AsyncMock(side_effect=AssertionError("YouTube does not require TIDAL auth"))
    cog._handle_youtube_video = AsyncMock()
    cog._handle_youtube_playlist = AsyncMock()

    await cog.tplay(youtube_ctx, query=url)

    selected = cog._handle_youtube_playlist if playlist else cog._handle_youtube_video
    other = cog._handle_youtube_video if playlist else cog._handle_youtube_playlist
    selected.assert_awaited_once_with(youtube_ctx, "PLexample" if playlist else VIDEO_ID)
    other.assert_not_awaited()
    cog.check_ready.assert_not_awaited()


@pytest.mark.asyncio
async def test_shared_admission_keeps_entry_metadata_and_deletes_confirmation(cog, youtube_ctx, native_session, monkeypatch):
    module = importlib.import_module(cog.__class__.__module__)
    entry = await cog._youtube_entry(_video(), youtube_ctx.author.id)
    native_session.current = entry
    monkeypatch.setattr(module, "QUEUED_EMBED_DELETE_DELAY", 0)

    assert await cog._admit_entry(youtube_ctx, native_session, entry)
    await asyncio.gather(*cog._tasks)

    assert native_session.entries == [entry]
    youtube_ctx.send.return_value.delete.assert_awaited_once()
    assert not cog._current_meta
    assert not cog._tasks


@pytest.mark.asyncio
async def test_queue_rejection_emits_one_error_without_publishing_playback(cog, youtube_ctx, native_session):
    native_session.enqueue.side_effect = None
    native_session.enqueue.return_value = False
    await cog._handle_youtube_video(youtube_ctx, VIDEO_ID)
    native_session.enqueue.assert_awaited_once()
    assert not native_session.entries
    assert not cog._current_meta
    youtube_ctx.send.assert_awaited_once()
    assert "queue is full" in youtube_ctx.send.await_args.kwargs["embed"].description
