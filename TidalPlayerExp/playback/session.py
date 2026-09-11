"""One serial, cancellation-safe native voice playback worker per guild."""

from __future__ import annotations

import asyncio
import logging
import math
import random
import secrets
import threading
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import replace
from typing import Protocol, cast

import discord

from ..domain.models import TrackMeta
from .errors import PlaybackUnavailable
from .ffmpeg import AudioSourceFactory
from .interfaces import PlaybackEventSink, SourceResolver
from .models import PlaybackEntry, PlaybackSnapshot

log = logging.getLogger(__name__)


class VoiceGuild(Protocol):
    @property
    def voice_client(self) -> object | None: ...


class VoiceChannel(Protocol):
    @property
    def id(self) -> int: ...


class SessionVoiceClient(Protocol):
    """Only the voice operations and ownership identity needed by a session."""

    @property
    def guild(self) -> VoiceGuild: ...

    @property
    def channel(self) -> VoiceChannel: ...

    def is_connected(self) -> bool: ...

    def play(
        self, source: discord.AudioSource, *, after: Callable[[Exception | None], None]
    ) -> None: ...

    def stop(self) -> None: ...

    def pause(self) -> None: ...

    def resume(self) -> None: ...

    async def disconnect(self, *, force: bool = False) -> None: ...


class _OwnedAudio(discord.AudioSource):
    """Serialize Discord's cleanup with our off-loop cleanup and run it once."""

    def __init__(self, audio: discord.AudioSource) -> None:
        self._audio = audio
        self._cleanup_lock = threading.Lock()
        self._cleaned = False

    def read(self) -> bytes:
        return self._audio.read()

    def is_opus(self) -> bool:
        return self._audio.is_opus()

    def cleanup(self) -> None:
        with self._cleanup_lock:
            if not self._cleaned:
                self._cleaned = True
                try:
                    self._audio.cleanup()
                except Exception as error:  # noqa: BLE001 - cleanup must never expose source details
                    log.warning("Audio cleanup failed (%s)", type(error).__name__)


class NativePlaybackSession:
    """Own an exact voice client and resolve queued tracks just before playing."""

    def __init__(
        self,
        guild_id: int,
        voice_client: SessionVoiceClient,
        resolver: SourceResolver,
        source_factory: AudioSourceFactory,
        sink: PlaybackEventSink,
        *,
        queue_capacity: int = 1000,
        resolve_timeout: float = 30.0,
        create_timeout: float = 45.0,
        disconnect_timeout: float = 10.0,
        sink_timeout: float = 10.0,
    ) -> None:
        if queue_capacity < 1:
            raise ValueError("Queue capacity must be positive")
        if min(resolve_timeout, create_timeout, disconnect_timeout, sink_timeout) <= 0:
            raise ValueError("Session deadlines must be positive")
        self._guild_id = guild_id
        self._voice_client = voice_client
        self._resolver = resolver
        self._factory = source_factory
        self._sink = sink
        self._capacity = queue_capacity
        self._resolve_timeout = resolve_timeout
        self._create_timeout = create_timeout
        self._disconnect_timeout = disconnect_timeout
        self._sink_timeout = sink_timeout
        self._lock = asyncio.Lock()
        self._queue: deque[PlaybackEntry] = deque()
        self._current: PlaybackEntry | None = None
        self._source: _OwnedAudio | None = None
        self._paused = False
        self._generation = 0
        self._closed = False
        self._running = False
        self._failures = 0
        self._volume = 100
        self._repeat = "off"
        self._next_entry: PlaybackEntry | None = None
        self._pause_on_start = False
        self._started_at: float | None = None
        self._paused_at: float | None = None
        self._runner: asyncio.Task[None] | None = None
        self._cleanup_barrier: asyncio.Future[list[object]] | None = None
        self._close_task: asyncio.Task[None] | None = None

    @property
    def guild_id(self) -> int:
        return self._guild_id

    @property
    def voice_client(self) -> SessionVoiceClient:
        return self._voice_client

    @property
    def closed(self) -> bool:
        return self._closed

    def snapshot(self) -> PlaybackSnapshot:
        """Return detached immutable state; all mutations occur on the event loop."""
        channel_id = self._voice_client.channel.id if self._owned() else None
        return PlaybackSnapshot(
            self._current, tuple(self._queue), self._paused, channel_id,
            self._volume, self._repeat, self._position(), self._failures >= 3 and not self._running,
        )

    def _position(self) -> float:
        if self._current is None:
            return 0.0
        elapsed = 0.0 if self._started_at is None else (self._paused_at or time.monotonic()) - self._started_at
        return max(0.0, self._current.start_time + elapsed)

    def _owned(self) -> bool:
        return self._voice_client.guild.voice_client is self._voice_client

    async def enqueue(
        self, entry: PlaybackEntry, *, start_if_idle: bool = True, next_up: bool = False
    ) -> bool:
        """Admit one waiting entry promptly without awaiting media preparation."""
        async with self._lock:
            if self._closed or len(self._queue) >= self._capacity:
                return False
            if next_up:
                self._queue.appendleft(entry)
            else:
                self._queue.append(entry)
            self._failures = 0
            if start_if_idle:
                self._running = True
                if self._runner is None or self._runner.done():
                    self._runner = asyncio.create_task(self._run())
            return True

    async def remove(self, index: int) -> PlaybackEntry | None:
        """Remove a one-based waiting position, never the current track."""
        async with self._lock:
            if self._closed or isinstance(index, bool) or not isinstance(index, int) or not 1 <= index <= len(self._queue):
                return None
            removed = self._queue[index - 1]
            del self._queue[index - 1]
            return removed

    async def clear_queue(self) -> int:
        """Clear waiting tracks without interrupting current audio."""
        async with self._lock:
            count = len(self._queue)
            self._queue.clear()
            return count

    async def move(self, index: int, destination: int) -> bool:
        """Move between one-based waiting positions atomically."""
        async with self._lock:
            if self._closed or any(isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= len(self._queue) for value in (index, destination)):
                return False
            moved = self._queue[index - 1]
            del self._queue[index - 1]
            self._queue.insert(destination - 1, moved)
            return True

    async def shuffle_queue(self) -> bool:
        """Shuffle waiting tracks, leaving playback untouched."""
        async with self._lock:
            if self._closed or len(self._queue) < 2:
                return False
            items = list(self._queue)
            random.shuffle(items)
            self._queue = deque(items)
            return True

    async def set_repeat(self, mode: str) -> None:
        """Repeat successful tracks only; skips and failures never repeat."""
        if mode not in {"off", "track", "queue"}:
            raise ValueError("Repeat must be off, track, or queue")
        async with self._lock:
            self._repeat = mode

    async def resume_queue(self) -> bool:
        """Explicitly retry waiting tracks after the repeated-failure guard halted."""
        async with self._lock:
            if self._closed or self._running or self._current is not None or not self._queue:
                return False
            self._failures = 0
            self._running = True
            if self._runner is None or self._runner.done():
                self._runner = asyncio.create_task(self._restart(self._cleanup_barrier, None))
            return True

    def _replace_current(self, position: float) -> Awaitable[object] | None:
        """Called under the state lock; the predecessor retains source cleanup."""
        assert self._current is not None
        current, paused = self._current, self._paused
        self._interrupt()
        self._next_entry = replace(
            current, entry_id=secrets.token_hex(12), start_time=position,
            replaces_entry_id=current.entry_id,
        )
        self._pause_on_start = paused
        self._running = True
        self._runner = asyncio.create_task(self._restart(self._cleanup_barrier, None))
        return self._cleanup_barrier

    async def seek(self, seconds: float) -> bool:
        """Restart the same finite track at an absolute offset, preserving its queue."""
        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not math.isfinite(seconds) or seconds < 0:
            return False
        async with self._lock:
            if self._closed or self._current is None or not 0 <= seconds < self._current.meta["duration"]:
                return False
            barrier = self._replace_current(float(seconds))
        await self._wait_predecessor(barrier)
        return True

    async def set_volume(self, percent: int) -> None:
        """Set 0–150 percent gain, rebuffering current audio at its existing position."""
        if isinstance(percent, bool) or not isinstance(percent, int) or not 0 <= percent <= 150:
            raise ValueError("Volume must be between 0 and 150")
        async with self._lock:
            if self._closed:
                raise PlaybackUnavailable()
            if percent == self._volume:
                return
            self._volume = percent
            barrier = None
            if self._current is not None:
                position = self._position() if self._current.meta["duration"] > 0 else 0.0
                barrier = self._replace_current(position)
        await self._wait_predecessor(barrier)

    async def set_paused(self, paused: bool) -> bool:
        """Pause or resume only an active source owned by this session."""
        async with self._lock:
            if self._closed or self._source is None or not self._owned():
                return False
            try:
                if paused:
                    self._voice_client.pause()
                    if not self._paused:
                        self._paused_at = time.monotonic()
                else:
                    self._voice_client.resume()
                    if self._paused_at is not None and self._started_at is not None:
                        self._started_at += time.monotonic() - self._paused_at
                    self._paused_at = None
            except Exception as error:  # noqa: BLE001 - voice boundary may raise arbitrary errors
                log.warning("Voice pause failed (%s)", type(error).__name__)
                return False
            self._paused = paused
            return True

    def _interrupt(self) -> asyncio.Task[None] | None:
        """Invalidate callbacks under the state lock; the old worker owns cleanup."""
        self._generation += 1
        self._paused = False
        previous = self._runner
        if (
            previous is not None
            and previous is not asyncio.current_task()
            and not previous.done()
        ):
            previous.cancel()
        if self._source is not None and self._owned():
            try:
                self._voice_client.stop()
            except Exception as error:  # noqa: BLE001 - still clean the owned source on stop failure
                log.warning("Voice stop failed (%s)", type(error).__name__)
        self._current = None
        self._next_entry = None
        self._pause_on_start = False
        self._started_at = self._paused_at = None
        # Register the dependency now, not inside the successor coroutine: a
        # successor can be cancelled before its first instruction executes.
        pending: list[Awaitable[object]] = []
        if self._cleanup_barrier is not None and not self._cleanup_barrier.done():
            pending.append(self._cleanup_barrier)
        if previous is not None and not previous.done():
            pending.append(previous)
        self._cleanup_barrier = asyncio.gather(*pending, return_exceptions=True)
        return previous

    async def _restart(
        self, previous: Awaitable[object] | None, ended: PlaybackEntry | None
    ) -> None:
        try:
            await self._wait_predecessor(previous)
        except asyncio.CancelledError:
            # A later stop/close cancels this successor, but it must preserve
            # the cleanup barrier for every earlier worker in the chain.
            await self._wait_predecessor(previous)
            raise
        await self._run(ended)

    async def skip(self) -> bool:
        """Cancel the current preparation/playback and advance once after cleanup."""
        async with self._lock:
            if self._closed or self._current is None:
                return False
            ended = self._current
            previous = self._interrupt()
            barrier = self._cleanup_barrier
            self._running = True
            self._runner = asyncio.create_task(self._restart(barrier, ended))
        # A sink can reenter skip from the worker itself. It must return so that
        # the old worker can unwind and let its successor run.
        if previous is not asyncio.current_task():
            await self._wait_predecessor(barrier)
            await asyncio.sleep(0)
        return True

    @staticmethod
    async def _wait_predecessor(previous: Awaitable[object] | None) -> None:
        if previous is not None:
            await asyncio.shield(asyncio.gather(previous, return_exceptions=True))

    async def stop(self, *, clear_queue: bool = True) -> None:
        """Stop without queue-end events and optionally retain waiting entries."""
        async with self._lock:
            if clear_queue:
                self._queue.clear()
            self._repeat = "off"
            self._failures = 0
            self._running = False
            previous = self._interrupt()
            barrier = self._cleanup_barrier
            self._runner = asyncio.create_task(self._restart(barrier, None))
        if previous is not asyncio.current_task():
            await self._wait_predecessor(barrier)

    async def close(self) -> None:
        """Idempotently stop and disconnect only while the client is still owned."""
        async with self._lock:
            if self._close_task is None or (
                self._close_task.done()
                and not self._close_task.cancelled()
                and self._close_task.exception() is not None
            ):
                self._closed = True
                self._running = False
                self._queue.clear()
                previous = self._interrupt()
                self._close_task = asyncio.create_task(self._finish_close(self._cleanup_barrier))
            else:
                previous = self._runner
            close_task = self._close_task
        if previous is not asyncio.current_task():
            await asyncio.shield(close_task)

    async def _finish_close(self, previous: Awaitable[object] | None) -> None:
        await self._wait_predecessor(previous)
        failed = False
        if self._owned():
            try:
                await asyncio.wait_for(
                    self._voice_client.disconnect(force=True), self._disconnect_timeout
                )
            except Exception as error:  # noqa: BLE001 - shutdown must remain bounded and redacted
                log.warning("Voice disconnect failed (%s)", type(error).__name__)
                failed = True
        if failed:
            raise PlaybackUnavailable()

    async def _notify(
        self, event: str, entry: PlaybackEntry | None, generation: int
    ) -> None:
        if self._closed or self._generation != generation:
            return
        try:
            async with asyncio.timeout(self._sink_timeout):
                if event == "started" and entry is not None:
                    await self._sink.track_started(self._guild_id, entry)
                elif event == "failed" and entry is not None:
                    await self._sink.track_failed(
                        self._guild_id, entry, "Playback failed"
                    )
                elif event == "ended":
                    await self._sink.queue_ended(self._guild_id, entry)
        except Exception as error:  # noqa: BLE001 - user-facing notifications cannot wedge playback
            log.warning("Playback notification failed (%s)", type(error).__name__)

    @staticmethod
    def _fallback_entry(entry: PlaybackEntry) -> PlaybackEntry:
        assert entry.fallback is not None
        if entry.fallback_meta is not None:
            metadata = dict(entry.fallback_meta)
        else:
            metadata = {
                "title": "YouTube video",
                "artist": "YouTube",
                "album": None,
                "duration": 0,
                "image": None,
                "share_url": f"https://www.youtube.com/watch?v={entry.fallback.identifier}",
            }
        metadata.update(
            source=entry.fallback.kind.value,
            quality="YouTube",
            track_id=None,
            audio_resolution=None,
        )
        return replace(
            entry,
            primary=entry.fallback,
            fallback=None,
            fallback_meta=None,
            meta=cast(TrackMeta, metadata),
        )

    async def _create(self, entry: PlaybackEntry) -> tuple[PlaybackEntry, _OwnedAudio]:
        resolved = await asyncio.wait_for(
            self._resolver.resolve(entry.primary), self._resolve_timeout
        )
        resolved = replace(resolved, start_time=entry.start_time, volume=self._volume)
        # Flat playlist/API metadata can omit duration. Publish the actual
        # resolved duration without mutating the queue entry or known values.
        if entry.meta["duration"] <= 0 and resolved.duration is not None:
            entry = replace(entry, meta={**entry.meta, "duration": resolved.duration})
        # Shield creation so cancellation cannot leave the factory constructing
        # an unaccounted source while the successor starts another one.
        creation = asyncio.create_task(
            asyncio.wait_for(self._factory.create(resolved), self._create_timeout)
        )
        try:
            return entry, _OwnedAudio(await asyncio.shield(creation))
        except asyncio.CancelledError:
            try:
                audio = await creation
            except Exception as error:  # noqa: BLE001 - factory owns failed creation cleanup
                log.warning(
                    "Cancelled source creation failed (%s)", type(error).__name__
                )
            else:
                await asyncio.to_thread(_OwnedAudio(audio).cleanup)
            raise

    async def _prepare(
        self, entry: PlaybackEntry
    ) -> tuple[PlaybackEntry, _OwnedAudio] | None:
        attempts = [entry, entry]
        if entry.fallback is not None:
            attempts.append(self._fallback_entry(entry))
        for effective in attempts:
            try:
                if not self._owned() or not self._voice_client.is_connected():
                    return None
                return await self._create(effective)
            except Exception as error:  # noqa: BLE001 - retry with fresh resolution, never retain signed errors
                log.warning("Playback preparation failed (%s)", type(error).__name__)
        return None

    def _after_callback(
        self, generation: int, completion: asyncio.Future[bool]
    ) -> Callable[[Exception | None], None]:
        loop = completion.get_loop()

        def complete(failed: bool) -> None:
            if generation == self._generation and not completion.done():
                completion.set_result(failed)

        def after(error: Exception | None) -> None:
            try:
                loop.call_soon_threadsafe(complete, error is not None)
            except RuntimeError:
                pass  # The event loop has already closed; no coroutine was created.

        return after

    async def _run(self, previous: PlaybackEntry | None = None) -> None:
        while True:
            async with self._lock:
                if self._closed or not self._running:
                    return
                generation = self._generation
                if not self._queue and self._next_entry is None:
                    self._running = False
                    self._current = None
                    ended = previous
                    entry = None
                else:
                    ended = None
                    entry = self._next_entry if self._next_entry is not None else self._queue.popleft()
                    self._next_entry = None
                    self._current = entry
                    self._generation += 1
                    generation = self._generation
            if entry is None:
                if ended is not None:
                    await self._notify("ended", ended, generation)
                if self._generation != generation:
                    return
                previous = None
                continue
            audio: _OwnedAudio | None = None
            failed = False
            try:
                prepared = await self._prepare(entry)
                if prepared is None:
                    failed = True
                else:
                    entry, audio = prepared
                    completion: asyncio.Future[bool] = (
                        asyncio.get_running_loop().create_future()
                    )

                    async with self._lock:
                        if self._generation != generation or self._closed:
                            return
                        if not self._owned() or not self._voice_client.is_connected():
                            failed = True
                        else:
                            self._current = entry
                            self._source = audio
                            self._voice_client.play(
                                audio,
                                after=self._after_callback(generation, completion),
                            )
                            self._started_at = time.monotonic()
                            if self._pause_on_start:
                                self._voice_client.pause()
                                self._paused = True
                                self._paused_at = self._started_at
                            self._pause_on_start = False
                    if not failed:
                        await self._notify("started", entry, generation)
                        if self._generation != generation:
                            return
                        failed = await completion
            except Exception as error:  # noqa: BLE001 - sync voice errors follow the normal failure path
                log.warning("Playback failed (%s)", type(error).__name__)
                failed = True
            finally:
                if audio is not None:
                    cleanup = asyncio.create_task(asyncio.to_thread(audio.cleanup))
                    try:
                        await asyncio.shield(cleanup)
                    except asyncio.CancelledError:
                        await cleanup
                        raise
                    finally:
                        self._source = None
            async with self._lock:
                if generation != self._generation or self._closed:
                    return
                self._current = None
                self._paused = False
                self._started_at = self._paused_at = None
                self._failures = self._failures + 1 if failed else 0
                if self._failures >= 3:
                    self._running = False
                elif not failed and self._repeat != "off":
                    repeated = replace(entry, entry_id=secrets.token_hex(12), start_time=0, replaces_entry_id=None)
                    if self._repeat == "track":
                        self._next_entry = repeated
                    else:
                        # Transfer the next waiting track into the active slot
                        # before recycling this one; total ownership stays at
                        # capacity + one and no repeat item is silently lost.
                        if len(self._queue) >= self._capacity:
                            self._next_entry = self._queue.popleft()
                        self._queue.append(repeated)
            if failed:
                await self._notify("failed", entry, generation)
                previous = None
            else:
                previous = entry
            if self._generation != generation:
                return


__all__ = ("NativePlaybackSession", "SessionVoiceClient")
