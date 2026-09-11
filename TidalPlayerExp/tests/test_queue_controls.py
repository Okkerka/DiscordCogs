"""Queue positions always exclude the currently playing track."""
import asyncio
from dataclasses import replace

import pytest

from TidalPlayerExp.tests.test_native_session import entry, setup


@pytest.mark.asyncio
async def test_remove_indexes_only_waiting_tracks():
    session, voice, resolver, factory, sink = setup()
    try:
        await session.enqueue(entry(1))
        await sink.expect("started")
        for number in range(2, 6):
            await session.enqueue(entry(number))
        assert (await session.remove(1)).meta["track_id"] == 2
        assert (await session.remove(3)).meta["track_id"] == 5
        assert await session.remove(0) is None
        assert await session.remove(-1) is None
        assert await session.remove(3) is None
        assert session.snapshot().current.meta["track_id"] == 1
        assert [value.meta["track_id"] for value in session.snapshot().queued] == [3, 4]
        assert await session.clear_queue() == 2
        assert not session.snapshot().queued
        assert session.snapshot().current.meta["track_id"] == 1
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_move_and_playnext_respect_queue_capacity():
    session, *others = setup(queue_capacity=3)
    try:
        for number in (1, 2):
            await session.enqueue(entry(number), start_if_idle=False)
        assert await session.enqueue(entry(3), start_if_idle=False, next_up=True)
        assert [value.entry_id for value in session.snapshot().queued] == ["3", "1", "2"]
        assert not await session.enqueue(entry(4), start_if_idle=False, next_up=True)
        assert await session.move(3, 1)
        assert [value.entry_id for value in session.snapshot().queued] == ["2", "3", "1"]
        assert not await session.move(0, 2)
        assert not await session.move(1, 4)
        assert await session.shuffle_queue()
        assert {value.entry_id for value in session.snapshot().queued} == {"1", "2", "3"}
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_volume_bounds_apply_to_every_new_source():
    session, voice, resolver, factory, sink = setup()
    seen = []
    original = factory.create
    async def create(source):
        seen.append(source)
        return await original(source)
    factory.create = create
    try:
        for value in (-1, 151, True, 0.5):
            with pytest.raises(ValueError):
                await session.set_volume(value)
        await session.set_volume(150)
        await session.enqueue(entry())
        await sink.expect("started")
        assert seen[-1].volume == 150
        assert session.snapshot().volume == 150
        await session.set_volume(0)
        await sink.expect("started")
        assert seen[-1].volume == 0
        assert session.snapshot().current.meta["track_id"] == 1
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_seek_restarts_same_track_and_preserves_queue_and_pause():
    session, voice, resolver, factory, sink = setup()
    seen = []
    original = factory.create
    async def create(source):
        seen.append(source)
        return await original(source)
    factory.create = create
    try:
        await session.enqueue(entry())
        await sink.expect("started")
        await session.enqueue(entry(2))
        await session.set_paused(True)
        assert await session.seek(10)
        await sink.expect("started")
        assert seen[-1].start_time == 10
        assert session.snapshot().current.replaces_entry_id == "1"
        assert session.snapshot().paused
        assert session.snapshot().position >= 10
        assert [item.entry_id for item in session.snapshot().queued] == ["2"]
        assert not await session.seek(20)
        assert not await session.seek(-1)
    finally:
        await session.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,want", [("track", 1), ("queue", 2), ("off", 2)])
async def test_repeat_modes_and_skip_advance_correctly(mode, want):
    session, voice, resolver, factory, sink = setup()
    try:
        await session.set_repeat(mode)
        await session.enqueue(entry())
        await sink.expect("started")
        await session.enqueue(entry(2))
        voice.callbacks[-1](None)
        await sink.expect("started")
        assert session.snapshot().current.meta["track_id"] == want
        if mode == "track":
            await session.skip()
            await sink.expect("started")
            assert session.snapshot().current.meta["track_id"] == 2
        if mode == "queue":
            assert session.snapshot().queued[0].meta["track_id"] == 1
        await session.stop()
        assert not session.snapshot().queued
        assert session.snapshot().current is None
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_retry_only_resumes_halted_queue():
    session, voice, resolver, factory, sink = setup()
    resolver.fail.add(entry().primary.kind)
    try:
        for number in range(1, 5):
            await session.enqueue(entry(number), start_if_idle=False)
        await session.enqueue(entry(5))
        for _ in range(3):
            await sink.expect("failed")
        assert session.snapshot().halted
        resolver.fail.clear()
        assert await session.resume_queue()
        await sink.expect("started")
        assert not session.snapshot().halted
        assert session.snapshot().current.meta["track_id"] == 4
        assert not await session.resume_queue()
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_queue_repeat_preserves_tracks_when_waiting_queue_is_full():
    session, voice, resolver, factory, sink = setup(queue_capacity=1)
    try:
        await session.set_repeat("queue")
        await session.enqueue(entry(1))
        await sink.expect("started")
        assert await session.enqueue(entry(2))
        for expected in (2, 1, 2):
            voice.callbacks[-1](None)
            await sink.expect("started")
            assert session.snapshot().current.meta["track_id"] == expected
            assert len(session.snapshot().queued) == 1
            assert session.snapshot().queued[0].meta["track_id"] == 3 - expected
    finally:
        await session.close()
