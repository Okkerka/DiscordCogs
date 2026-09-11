"""Prefix/slash controls use the same guarded native session operations."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from TidalPlayerExp.providers.attachments import AttachmentResolver
from TidalPlayerExp.tests.test_attachments import _NOW, _attachment
from TidalPlayerExp.tests.test_native_session import entry, setup


def context(channel_id=99):
    channel = SimpleNamespace(id=channel_id)
    return SimpleNamespace(
        guild=SimpleNamespace(id=1), channel=SimpleNamespace(),
        author=SimpleNamespace(id=42, voice=SimpleNamespace(channel=channel)),
        message=SimpleNamespace(attachments=[]), interaction=None,
        defer=AsyncMock(), send=AsyncMock(),
    )


@pytest_asyncio.fixture
async def controls(cog):
    session, voice, resolver, factory, sink = setup()
    cog.backend = SimpleNamespace(get=AsyncMock(return_value=session), close=AsyncMock())
    cog._refresh_controller = AsyncMock()
    cog._initialized = True
    try:
        yield cog, session, sink
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_remove_and_remove_all_preserve_current_and_cancel_imports(controls):
    cog, session, sink = controls
    ctx = context()
    await session.enqueue(entry(1))
    await sink.expect("started")
    for number in (2, 3, 4):
        await session.enqueue(entry(number))
    await cog.remove_command(ctx, 2)
    assert [track.meta["track_id"] for track in session.snapshot().queued] == [2, 4]
    event = cog._claim_batch(1)
    await cog.remove_all(ctx)
    assert event.is_set()
    assert not session.snapshot().queued
    assert session.snapshot().current.meta["track_id"] == 1
    assert cog._stop_generations[1] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("method,args", [("stop_command", ()), ("remove_command", (1,)), ("volume_command", (150,)), ("seek_command", ("1",))])
async def test_controls_reject_members_outside_bot_voice_channel(controls, method, args):
    cog, session, sink = controls
    await session.enqueue(entry(1), start_if_idle=False)
    await getattr(cog, method)(context(channel_id=7), *args)
    assert len(session.snapshot().queued) == 1
    assert session.snapshot().volume == 100
    assert cog._stop_generations[1] == 0


@pytest.mark.asyncio
async def test_stop_clears_native_queue_and_cancels_initial_lookup(controls):
    cog, session, sink = controls
    await session.enqueue(entry(1))
    await sink.expect("started")
    await session.enqueue(entry(2))
    event = cog._claim_batch(1)
    await cog.stop_command(context())
    assert event.is_set()
    assert session.snapshot().current is None
    assert not session.snapshot().queued


@pytest.mark.asyncio
async def test_overlapping_volume_commands_persist_in_application_order(controls, monkeypatch):
    cog, session, sink = controls
    setting = cog.config.guild_from_id(1).volume
    entered, release = asyncio.Event(), asyncio.Event()
    original = setting.set
    async def delayed(value):
        if value == 25:
            entered.set()
            await release.wait()
        await original(value)
    monkeypatch.setattr(setting, "set", delayed)
    first = asyncio.create_task(cog.volume_command(context(), 25))
    await entered.wait()
    second = asyncio.create_task(cog.volume_command(context(), 150))
    await asyncio.sleep(0)
    release.set()
    await asyncio.gather(first, second)
    assert await setting() == session.snapshot().volume == 150


@pytest.mark.asyncio
@pytest.mark.parametrize("slash", [True, False])
async def test_file_playback_bypasses_tidal_and_never_exposes_signed_url(controls, slash):
    cog, session, sink = controls
    ctx = context()
    ctx.interaction = object() if slash else None
    file = _attachment()
    cog.attachment_resolver = AttachmentResolver(clock=lambda: _NOW)
    cog._prepare_playback_session = AsyncMock(return_value=session)
    cog.check_ready = AsyncMock(side_effect=AssertionError("Files must not require TIDAL"))
    if slash:
        await cog.playfile(ctx, file=file)
    else:
        ctx.message.attachments = [file]
        await cog.playfile(ctx)
    await sink.expect("started")
    assert session.snapshot().current.primary.kind.value == "attachment"
    assert file.url not in repr(session.snapshot().current)
    assert file.url not in repr(ctx.send.call_args_list)


@pytest.mark.asyncio
async def test_failed_file_admission_releases_private_registry_slot(controls):
    cog, session, sink = controls
    cog.attachment_resolver = AttachmentResolver(clock=lambda: _NOW)
    cog._prepare_playback_session = AsyncMock(return_value=None)
    await cog.playfile(context(), file=_attachment())
    assert not cog.attachment_resolver._entries


@pytest.mark.asyncio
async def test_playnext_sets_front_admission_and_rejects_collections(controls):
    cog, session, sink = controls
    await session.enqueue(entry(1))
    await sink.expect("started")
    await session.enqueue(entry(2))
    cog.check_ready = AsyncMock(return_value=True)
    cog._prepare_playback_session = AsyncMock(return_value=session)
    async def single(ctx, identifier):
        await cog._admit_entry(ctx, session, entry(3))
    cog._handle_track = single
    await cog.playnext_command(context(), query="https://tidal.com/track/3")
    assert [item.meta["track_id"] for item in session.snapshot().queued] == [3, 2]
    cog._handle_album = AsyncMock()
    await cog.playnext_command(context(), query="https://tidal.com/album/1")
    cog._handle_album.assert_not_awaited()


@pytest.mark.asyncio
async def test_new_voice_session_restores_saved_volume_before_admission(cog):
    session, voice, resolver, factory, sink = setup()
    ctx = context()
    ctx.guild.voice_client = None
    ctx.guild.me = object()
    ctx.author.voice.channel.permissions_for = lambda _: SimpleNamespace(connect=True, speak=True)
    cog.backend = SimpleNamespace(get=AsyncMock(return_value=None), connect=AsyncMock(return_value=session))
    await cog.config.guild(ctx.guild).volume.set(35)
    try:
        assert await cog._prepare_playback_session(ctx) is session
        assert session.snapshot().volume == 35
        assert not factory.sources
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_empty_controller_refresh_does_not_allocate_a_view(cog):
    cog._controller_view = AsyncMock()
    await cog._refresh_controller(1, force=True)
    cog._controller_view.assert_not_awaited()


@pytest.mark.asyncio
async def test_halted_queue_failure_message_explains_retry(controls):
    cog, session, sink = controls
    session._failures = 3
    await session.enqueue(entry(), start_if_idle=False)
    session._failures = 3
    channel = SimpleNamespace(send=AsyncMock())
    cog._playback_channels[1] = channel
    await cog.track_failed(1, entry(), "private exception must not be displayed")
    description = channel.send.await_args.kwargs["embed"].description
    assert "retry" in description.lower()
    assert "private exception" not in description


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["remove_all", "stop_command"])
async def test_cancel_controls_cover_unpublished_voice_session(cog, method):
    cog.backend = SimpleNamespace(get=AsyncMock(return_value=None))
    event = cog._claim_batch(1)
    await getattr(cog, method)(context())
    assert event.is_set()
    assert cog._stop_generations[1] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["remove_all", "stop_command"])
async def test_cancel_controls_respect_handshaking_voice_channel(cog, method):
    ctx = context()
    ctx.guild.voice_client = SimpleNamespace(channel=SimpleNamespace(id=7))
    cog.backend = SimpleNamespace(get=AsyncMock(return_value=None))
    event = cog._claim_batch(1)
    await getattr(cog, method)(ctx)
    assert not event.is_set()


@pytest.mark.parametrize("value,want", [("90", 90), ("1:30", 90), ("1:02:03", 3723), ("0", 0), ("-1", None), ("1:99", None), ("1.5", None), ("", None), ("１", None)])
def test_seek_timestamp_validation(value, want):
    from TidalPlayerExp.commands import parse_position
    assert parse_position(value) == want


@pytest.mark.asyncio
@pytest.mark.parametrize("platform", ["youtube", "soundcloud"])
async def test_selected_platform_search_does_not_require_tidal(controls, monkeypatch, platform):
    import importlib
    from TidalPlayerExp.playback.models import SourceKind, SourceReference
    cog, session, sink = controls
    module = importlib.import_module(cog.__class__.__module__)
    ref = SourceReference(SourceKind.YOUTUBE, "abcdefghijk") if platform == "youtube" else SourceReference(SourceKind.SOUNDCLOUD, "https://soundcloud.com/artist/song")
    search = AsyncMock(return_value=(ref, entry().meta))
    monkeypatch.setattr(module, "search_provider", search, raising=False)
    cog.check_ready = AsyncMock(side_effect=AssertionError("Should not check TIDAL login"))
    cog._prepare_playback_session = AsyncMock(return_value=session)
    await cog.tplay(context(), query="artist song", platform=platform)
    await sink.expect("started")
    assert session.snapshot().current.primary == ref
    search.assert_awaited_once_with(cog.youtube_resolver, "artist song", platform)


@pytest.mark.asyncio
@pytest.mark.parametrize("platform", [None, "tidal"])
async def test_unspecified_or_tidal_search_keeps_catalog_behavior(controls, monkeypatch, platform):
    cog, session, sink = controls
    cog.check_ready = AsyncMock(return_value=True)
    cog._prepare_playback_session = AsyncMock(return_value=session)
    search = AsyncMock(return_value=[object()])
    monkeypatch.setattr(type(cog.tidal), "search", search)
    cog._load_and_queue_track = AsyncMock()
    await cog.tplay(context(), query="artist song", platform=platform)
    search.assert_awaited_once_with("artist song", filter_remixes=True)
    cog._load_and_queue_track.assert_awaited_once()


@pytest.mark.asyncio
async def test_platform_selection_does_not_reroute_explicit_links(controls):
    cog, session, sink = controls
    cog._prepare_playback_session = AsyncMock(return_value=session)
    cog._handle_youtube_video = AsyncMock()
    await cog.tplay(context(), query="https://youtu.be/abcdefghijk", platform="soundcloud")
    cog._handle_youtube_video.assert_awaited_once()


@pytest.mark.asyncio
async def test_defer_is_idempotent_after_global_hook(cog):
    ctx = context()
    responded = False
    async def defer():
        nonlocal responded
        assert not responded
        responded = True
    ctx.interaction = SimpleNamespace(response=SimpleNamespace(is_done=lambda: responded))
    ctx.defer = AsyncMock(side_effect=defer)
    await cog.cog_before_invoke(ctx)
    await cog._defer(ctx)
    ctx.defer.assert_awaited_once()
