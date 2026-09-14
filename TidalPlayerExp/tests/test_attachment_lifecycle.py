"""Private upload links live only as long as their admitted playback entries."""
import asyncio
from dataclasses import replace

import pytest

from TidalPlayerExp.playback.errors import SourceResolutionError
from TidalPlayerExp.playback.models import SourceKind, SourceReference
from TidalPlayerExp.playback.session import NativePlaybackSession
from TidalPlayerExp.providers.attachments import AttachmentResolver
from TidalPlayerExp.providers.tidal_source import CompositeSourceResolver
from TidalPlayerExp.tests.test_attachments import _NOW, _attachment
from TidalPlayerExp.tests.test_native_session import Factory, Resolver, Sink, Voice, entry


def player(uploads, guild_id=1):
    voice, factory, sink = Voice(), Factory(), Sink()
    resolver = CompositeSourceResolver(Resolver(), Resolver(), attachments=uploads)
    session = NativePlaybackSession(guild_id, voice, resolver, factory, sink)
    return session, voice, factory, sink


def upload_entry(uploads, number=1):
    reference, metadata = uploads.register(_attachment())
    return replace(entry(number), primary=reference, meta={**metadata, "duration": 20})


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["finished", "failed", "skip", "stop", "close"])
async def test_consumed_upload_frees_registry_capacity(ending):
    uploads = AttachmentResolver(clock=lambda: _NOW, max_entries=1)
    session, voice, factory, sink = player(uploads)
    original = upload_entry(uploads)
    try:
        await session.enqueue(original)
        await sink.expect("started")
        if ending in {"finished", "failed"}:
            voice.callbacks[-1](RuntimeError("audio failed") if ending == "failed" else None)
            await sink.expect("failed" if ending == "failed" else "ended")
        else:
            await getattr(session, ending)()
        # Real admission, not internal counter inspection: a new upload fits.
        upload_entry(uploads, 2)
        with pytest.raises(SourceResolutionError):
            await uploads.resolve(original.primary)
    finally:
        await session.close()
        await uploads.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["remove", "clear_queue", "stop", "close"])
async def test_unstarted_uploads_are_released_when_removed(operation):
    uploads = AttachmentResolver(clock=lambda: _NOW, max_entries=1)
    session, voice, factory, sink = player(uploads)
    try:
        await session.enqueue(upload_entry(uploads), start_if_idle=False)
        if operation == "remove":
            assert await session.remove(1) is not None
        else:
            await getattr(session, operation)()
        upload_entry(uploads, 2)
    finally:
        await session.close()
        await uploads.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["track", "queue"])
async def test_repeat_keeps_upload_until_last_playback_owner_releases_it(mode):
    uploads = AttachmentResolver(clock=lambda: _NOW, max_entries=1)
    session, voice, factory, sink = player(uploads)
    original = upload_entry(uploads)
    try:
        await session.set_repeat(mode)
        await session.enqueue(original)
        await sink.expect("started")
        voice.callbacks[-1](None)
        await sink.expect("started")
        assert session.snapshot().current.primary == original.primary
        await uploads.resolve(original.primary)
        await session.stop()
        upload_entry(uploads, 2)
    finally:
        await session.close()
        await uploads.close()


@pytest.mark.asyncio
async def test_duplicate_upload_survives_other_guild_and_waiting_copy_removal():
    uploads = AttachmentResolver(clock=lambda: _NOW, max_entries=1)
    first, first_voice, factory, first_sink = player(uploads)
    second, second_voice, factory2, second_sink = player(uploads, 2)
    original = upload_entry(uploads)
    try:
        await first.enqueue(original)
        await first_sink.expect("started")
        await second.enqueue(replace(original, entry_id="other-guild"))
        await second_sink.expect("started")
        await second.enqueue(replace(original, entry_id="waiting-copy"))
        await second.remove(1)
        # Existing command-side discard must not invalidate admitted copies.
        uploads.discard(original.primary)
        await first.close()
        await uploads.resolve(original.primary)
        second_voice.callbacks[-1](None)
        await second_sink.expect("ended")
        upload_entry(uploads, 2)
    finally:
        await first.close()
        await second.close()
        await uploads.close()


@pytest.mark.asyncio
async def test_seek_keeps_upload_across_cleanup_and_stop_releases_pending_restart():
    uploads = AttachmentResolver(clock=lambda: _NOW, max_entries=1)
    session, voice, factory, sink = player(uploads)
    original = upload_entry(uploads)
    try:
        await session.enqueue(original)
        await sink.expect("started")
        assert await session.seek(5)
        await sink.expect("started")
        assert session.snapshot().current.start_time == 5
        await uploads.resolve(original.primary)
        # Hold the state lock until seek is waiting so stop can cancel an
        # unstarted replacement rather than only a fully playing successor.
        async with session._lock:
            seek = asyncio.create_task(session.seek(8))
            stop = asyncio.create_task(session.stop())
            await asyncio.sleep(0)
        await asyncio.gather(seek, stop)
        upload_entry(uploads, 2)
    finally:
        await session.close()
        await uploads.close()


@pytest.mark.asyncio
async def test_stopping_without_queue_clear_retains_waiting_upload():
    uploads = AttachmentResolver(clock=lambda: _NOW, max_entries=2)
    session, voice, factory, sink = player(uploads)
    original, waiting = upload_entry(uploads), upload_entry(uploads, 2)
    try:
        await session.enqueue(original)
        await sink.expect("started")
        await session.enqueue(waiting)
        await session.stop(clear_queue=False)
        with pytest.raises(SourceResolutionError):
            await uploads.resolve(original.primary)
        await uploads.resolve(waiting.primary)
        assert await session.resume_queue()
        await sink.expect("started")
        assert session.snapshot().current.primary == waiting.primary
    finally:
        await session.close()
        await uploads.close()


@pytest.mark.asyncio
async def test_failed_preparation_releases_upload_after_all_retries():
    uploads = AttachmentResolver(clock=lambda: _NOW, max_entries=1)
    session, voice, factory, sink = player(uploads)

    async def fail_create(resolved):
        raise RuntimeError("Decoder unavailable")

    factory.create = fail_create
    try:
        await session.enqueue(upload_entry(uploads))
        await sink.expect("failed")
        upload_entry(uploads, 2)
    finally:
        await session.close()
        await uploads.close()


@pytest.mark.asyncio
async def test_rejected_duplicate_does_not_leak_an_extra_owner():
    uploads = AttachmentResolver(clock=lambda: _NOW, max_entries=1)
    session, voice, factory, sink = player(uploads)
    session._capacity = 1
    original = upload_entry(uploads)
    try:
        assert await session.enqueue(original, start_if_idle=False)
        assert not await session.enqueue(replace(original, entry_id="rejected"))
        await session.close()
        upload_entry(uploads, 2)
    finally:
        await session.close()
        await uploads.close()


@pytest.mark.asyncio
async def test_volume_rebuffer_transfers_ownership_without_extending_signed_expiry():
    now = [_NOW]
    uploads = AttachmentResolver(clock=lambda: now[0], max_entries=1)
    session, voice, factory, sink = player(uploads)
    original = upload_entry(uploads)
    try:
        await session.enqueue(original)
        await sink.expect("started")
        await session.set_volume(50)
        await sink.expect("started")
        await uploads.resolve(original.primary)
        now[0] += 301
        with pytest.raises(SourceResolutionError):
            await uploads.resolve(original.primary)
        # Releasing an expired entry cannot remove a newly registered upload.
        fresh = uploads.register(
            _attachment(url="https://cdn.discordapp.com/attachments/123/456/mix.mp3")
        )[0]
        await session.close()
        await uploads.resolve(fresh)
    finally:
        await session.close()
        await uploads.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("connected", [True, False])
async def test_source_fallback_releases_original_upload_even_if_voice_disconnects(connected):
    uploads = AttachmentResolver(clock=lambda: _NOW, max_entries=1)
    session, voice, factory, sink = player(uploads)
    original = replace(upload_entry(uploads), fallback=SourceReference(SourceKind.YOUTUBE, "dQw4w9WgXcQ"))
    create = factory.create

    async def reject_upload(resolved):
        if resolved.media_only:
            raise RuntimeError("Upload cannot be decoded")
        voice.connected = connected
        return await create(resolved)

    factory.create = reject_upload
    try:
        await session.enqueue(original)
        await sink.expect("started" if connected else "failed")
        upload_entry(uploads, 2)
        with pytest.raises(SourceResolutionError):
            await uploads.resolve(original.primary)
    finally:
        await session.close()
        await uploads.close()
