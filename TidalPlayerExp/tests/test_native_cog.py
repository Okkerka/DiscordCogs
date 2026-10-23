"""Native command admission and effective playback event integration."""
import asyncio
from types import SimpleNamespace
from dataclasses import replace
from unittest.mock import AsyncMock, Mock

import pytest


@pytest.mark.asyncio
@pytest.mark.parametrize("rebuffer", [True, False])
async def test_seek_leaves_controller_untouched_but_new_tracks_resend(cog, native_session, rebuffer):
    from TidalPlayerExp.tests.test_native_session import entry
    from TidalPlayerExp.playback.models import PlaybackSnapshot

    previous = entry(1)
    current = replace(previous, entry_id="new", start_time=10,
                      replaces_entry_id=previous.entry_id if rebuffer else None)
    cog._current_entries[1] = previous
    native_session.snapshot.return_value = PlaybackSnapshot(current, (), False, 22, position=10)
    cog._refresh_controller = AsyncMock()
    cog._resend_controller_for_track_start = AsyncMock()
    cog._schedule_controller_recommendations = Mock()
    await cog.track_started(1, current)
    assert cog._current_entries[1] is current
    if rebuffer:
        cog._resend_controller_for_track_start.assert_not_awaited()
        cog._refresh_controller.assert_not_awaited()
    else:
        cog._resend_controller_for_track_start.assert_awaited_once_with(guild_id=1)


@pytest.fixture
def native_ctx(cog):
    channel = SimpleNamespace(id=22, permissions_for=lambda member: SimpleNamespace(connect=True, speak=True))
    guild = SimpleNamespace(id=1, voice_client=None, me=SimpleNamespace(voice=None))
    ctx = SimpleNamespace(guild=guild, author=SimpleNamespace(id=5, voice=SimpleNamespace(channel=channel)), channel=SimpleNamespace(send=AsyncMock()), send=AsyncMock())
    return ctx


@pytest.mark.asyncio
async def test_uploaded_file_does_not_schedule_autoplay_or_lookup_recommendations(cog):
    from TidalPlayerExp.tests.test_native_session import entry
    from TidalPlayerExp.playback.models import SourceKind, SourceReference

    upload = replace(entry(), primary=SourceReference(SourceKind.ATTACHMENT, "a" * 32),
                     meta={**entry().meta, "source": "Uploaded file", "track_id": None})
    await cog.config.guild_from_id(1).autoplay_enabled.set(True)
    cog._radio_candidates = AsyncMock(side_effect=AssertionError("No file recommendations"))
    assert await cog._get_recommendations(1, upload.meta) == []
    cog._radio_candidates.assert_not_awaited()
    cog._schedule_autoplay(1, upload, 0)
    assert not cog._autoplay_tasks
    assert await cog.config.guild_from_id(1).autoplay_enabled() is True


@pytest.fixture
def native_session(cog):
    from TidalPlayerExp.playback.models import PlaybackSnapshot
    session = SimpleNamespace(snapshot=Mock(return_value=PlaybackSnapshot(None, (), False, 22)), enqueue=AsyncMock(return_value=True), skip=AsyncMock(return_value=True), set_paused=AsyncMock(return_value=True), stop=AsyncMock())
    cog.backend = SimpleNamespace(get=AsyncMock(return_value=session), connect=AsyncMock(return_value=session), close_guild=AsyncMock(), close=AsyncMock())
    return session


@pytest.mark.asyncio
async def test_keyless_unauthenticated_youtube_admits_stable_reference(cog, native_ctx, native_session):
    from TidalPlayerExp.providers.youtube_resolver import YouTubeVideoMetadata
    cog.tidal = SimpleNamespace(is_logged_in=AsyncMock(return_value=False))
    cog.youtube_resolver.fetch_metadata = AsyncMock(return_value=YouTubeVideoMetadata("abcdefghijk", "A video", "A channel", 30, None))
    cog.youtube_resolver.resolve = AsyncMock()
    await cog._handle_youtube_video(native_ctx, "abcdefghijk")
    entry = native_session.enqueue.call_args.args[0]
    assert entry.primary.kind.value == "youtube"
    assert entry.meta["source"] == "YouTube"
    cog.youtube_resolver.resolve.assert_not_called()
    assert not cog._current_meta


@pytest.mark.asyncio
async def test_invalid_voice_does_not_fetch_youtube_metadata(cog, native_ctx, native_session):
    native_ctx.author.voice = None
    cog.youtube_resolver.fetch_metadata = AsyncMock()
    await cog._handle_youtube_video(native_ctx, "abcdefghijk")
    cog.youtube_resolver.fetch_metadata.assert_not_called()
    native_session.enqueue.assert_not_called()


@pytest.mark.asyncio
async def test_stale_autoplay_lookup_is_cancelled_by_a_fresh_schedule(cog, native_session):
    from TidalPlayerExp.tests.test_native_session import entry

    entered, release = asyncio.Event(), asyncio.Event()

    async def slow_lookup(guild_id, meta):
        entered.set()
        await release.wait()
        return []

    cog._get_recommendations = slow_lookup
    cog._autoplay_allowed = AsyncMock(return_value=True)
    cog.tidal = SimpleNamespace(is_logged_in=AsyncMock(return_value=True))

    cog._schedule_autoplay(1, entry(), 0)
    await asyncio.wait_for(entered.wait(), 2)
    first = cog._autoplay_tasks[1]

    # A second queue end while the old lookup is parked replaces the task
    # instead of being silently dropped.
    cog._schedule_autoplay(1, entry(2), 5)
    second = cog._autoplay_tasks[1]
    assert second is not first
    with pytest.raises(asyncio.CancelledError):
        await first

    release.set()
    await asyncio.wait_for(second, 2)
    assert cog._autoplay_tasks.get(1) is not second


@pytest.mark.asyncio
async def test_audio_conflict_rejected_before_connect(cog, native_ctx, native_session):
    cog.bot.get_cog = Mock(return_value=object())
    assert await cog._prepare_playback_session(native_ctx) is None
    cog.backend.connect.assert_not_called()
    assert "Audio" in native_ctx.send.call_args.kwargs["embed"].description


@pytest.mark.asyncio
async def test_skip_without_a_current_track_is_a_no_op_transition(cog, native_ctx, native_session):
    interaction = SimpleNamespace(guild=native_ctx.guild, response=SimpleNamespace(defer=AsyncMock()), followup=SimpleNamespace(send=AsyncMock()))
    await cog.controller_skip(interaction)
    # An empty snapshot is decided without touching the session: no skip
    # transition can be spawned for a queue with nothing playing.
    native_session.skip.assert_not_awaited()
    interaction.followup.send.assert_awaited_once_with("Nothing is playing.", ephemeral=True)
    assert not cog._current_meta


@pytest.mark.asyncio
async def test_stop_playback_cancels_import_and_stops_native_session(cog, native_ctx, native_session):
    event = cog._claim_batch(1)
    await cog._stop_playback(native_ctx.guild.id)
    assert event.is_set()
    native_session.stop.assert_awaited_once_with(clear_queue=True)
