"""Guild registry and lifecycle ownership for native Discord playback."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import discord

from .errors import PlaybackUnavailable
from .interfaces import PlaybackEventSink, SourceResolver
from .session import NativePlaybackSession

if TYPE_CHECKING:
    from .ffmpeg import AudioSourceFactory

log = logging.getLogger(__name__)


@dataclass
class _Connection:
    channel_id: int
    task: asyncio.Task[NativePlaybackSession] | None = None
    waiters: int = 0
    invalid: bool = False


class NativePlaybackBackend:
    """Own sessions and shared providers without locking across network I/O.

    Registry mutations run on the bot event loop. Pending tasks reserve their
    guild before the first await; unrelated guilds can connect independently.
    """

    def __init__(
        self,
        bot: Any,
        resolver: SourceResolver,
        source_factory: AudioSourceFactory,
        sink: PlaybackEventSink,
        *,
        connect_timeout: float = 20.0,
        idle_timeout: float = 120.0,
    ) -> None:
        self._bot = bot
        self._resolver = resolver
        self._source_factory = source_factory
        self._sink = sink
        self._connect_timeout = connect_timeout
        self._idle_timeout = idle_timeout
        self._sessions: dict[int, NativePlaybackSession] = {}
        self._pending: dict[int, _Connection] = {}
        self._retired: set[NativePlaybackSession] = set()
        # Each removal records its strictness: a weaker (idle-sweep) close must
        # not satisfy a joining full shutdown by inheritance.
        self._removals: dict[int, tuple[asyncio.Task[None], bool]] = {}
        self._empty_since: dict[int, float] = {}
        self._closing = False
        self._resolver_closed = False
        self._factory_closed = False
        self._close_lock = asyncio.Lock()
        self._housekeeping: asyncio.Task[None] | None = None
        self._clock = time.monotonic
        self._sleep = asyncio.sleep
        self._housekeeping_interval = 30.0

    async def get(self, guild_id: int) -> NativePlaybackSession | None:
        """Return a live, still-owned session, never stale or closing work."""
        if self._closing or guild_id in self._removals:
            return None
        session = self._sessions.get(guild_id)
        if session is None or not self._live(session):
            return None
        return session

    @staticmethod
    def _live(session: NativePlaybackSession) -> bool:
        voice = session.voice_client
        return bool(
            not session.closed
            and voice.guild.voice_client is voice
            and voice.is_connected()
        )

    def _validate(self, guild: Any, channel: Any) -> None:
        if (
            self._closing
            or guild.id in self._removals
            or self._bot.get_cog("Audio") is not None
            or channel.guild.id != guild.id
            or guild.me is None
        ):
            raise PlaybackUnavailable()
        permissions = channel.permissions_for(guild.me)
        if not permissions.connect or not permissions.speak:
            raise PlaybackUnavailable()

    async def connect(self, guild: Any, channel: Any) -> NativePlaybackSession:
        """Coalesce compatible callers and publish only an owned connection."""
        self._validate(guild, channel)
        attempt = self._pending.get(guild.id)
        if attempt is not None and attempt.invalid and attempt.task is not None and attempt.task.done():
            # A connector cancelled before its first instruction cannot run its
            # own finally; reclaim the dead attempt instead of failing every
            # later connect for this guild until teardown.
            if self._pending.get(guild.id) is attempt:
                self._pending.pop(guild.id, None)
            attempt = None
        if attempt is not None:
            if attempt.invalid or attempt.channel_id != channel.id:
                raise PlaybackUnavailable()
        else:
            session = self._sessions.get(guild.id)
            owned = session.voice_client if session is not None else None
            if guild.voice_client is not None and guild.voice_client is not owned:
                raise PlaybackUnavailable()
            if session is not None and self._live(session):
                if session.snapshot().channel_id != channel.id:
                    raise PlaybackUnavailable()
                return session
            if any(item.guild_id == guild.id for item in self._retired):
                raise PlaybackUnavailable()
            attempt = _Connection(channel.id)
            self._pending[guild.id] = attempt
            attempt.task = asyncio.create_task(self._connect(guild, channel, attempt))
            attempt.task.add_done_callback(self._consume_result)
        attempt.waiters += 1
        assert attempt.task is not None
        try:
            return await asyncio.shield(attempt.task)
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if attempt.invalid and current is not None and current.cancelling() == 0:
                # The shared attempt was cancelled out from under this waiter
                # (voice-state change/close); report an actionable failure
                # instead of a bare cancellation.
                raise PlaybackUnavailable() from None
            raise
        finally:
            attempt.waiters -= 1
            if attempt.waiters == 0 and not attempt.task.done():
                self._invalidate(attempt)

    @staticmethod
    def _consume_result(task: asyncio.Task[NativePlaybackSession]) -> None:
        # Last-waiter cancellation leaves the owned task responsible for late
        # cleanup, including retrieving its sanitized failure.
        if not task.cancelled():
            task.exception()

    @staticmethod
    def _invalidate(attempt: _Connection) -> None:
        if not attempt.invalid:
            attempt.invalid = True
            if attempt.task is not None:
                attempt.task.cancel()

    async def _connect(
        self, guild: Any, channel: Any, attempt: _Connection
    ) -> NativePlaybackSession:
        created: NativePlaybackSession | None = None

        def create_voice(client: discord.Client, connectable: discord.abc.Connectable) -> discord.VoiceClient:
            nonlocal created
            voice = discord.VoiceClient(client, connectable)
            # Connectable.connect registers this client before awaiting the
            # handshake. Retain its exact identity even if that await is cancelled.
            created = NativePlaybackSession(
                guild.id, voice, self._resolver, self._source_factory, self._sink
            )
            self._retired.add(created)
            return voice

        try:
            old = self._sessions.get(guild.id)
            if old is not None:
                await self._dispose(old)
            self._validate(guild, channel)
            if attempt.invalid or guild.voice_client is not None:
                raise PlaybackUnavailable()
            voice = await channel.connect(
                timeout=self._connect_timeout, reconnect=True, self_deaf=True,
                cls=create_voice,
            )
            if created is None or created.voice_client is not voice:
                raise PlaybackUnavailable()
            self._validate(guild, channel)
            if (
                attempt.invalid
                or not voice.is_connected()
                or guild.voice_client is not voice
            ):
                raise PlaybackUnavailable()
            self._sessions[guild.id] = created
            self._retired.discard(created)
            self._empty_since[guild.id] = self._clock()
            self._ensure_housekeeping()
            return created
        except asyncio.CancelledError:
            if created is not None:
                await self._try_dispose(created)
            raise
        except Exception:  # noqa: BLE001 - Discord/provider errors must never escape
            if created is not None:
                await self._try_dispose(created)
        finally:
            if self._pending.get(guild.id) is attempt:
                self._pending.pop(guild.id, None)
        # Raise outside the handler so raw network exceptions are not retained.
        raise PlaybackUnavailable()

    async def _dispose(self, session: NativePlaybackSession, *, only_if_idle: bool = False) -> None:
        self._retired.add(session)
        guild_id = session.guild_id
        if only_if_idle and not await session.close(only_if_idle=True):
            # The idle sweep decided from a snapshot; the session re-checked
            # under its own lock and found a new track. Keep it owned instead
            # of discarding a playback that just started.
            self._retired.discard(session)
            return
        if self._sessions.get(guild_id) is session:
            self._sessions.pop(guild_id, None)
            self._empty_since.pop(guild_id, None)
        # Session.close also checks exact voice ownership before disconnecting:
        # disconnecting an old Discord client can disrupt a replacement client.
        await session.close()
        self._retired.discard(session)

    async def _try_dispose(self, session: NativePlaybackSession, *, only_if_idle: bool = False) -> bool:
        try:
            await self._dispose(session, only_if_idle=only_if_idle)
        except Exception:  # noqa: BLE001 - retain failed cleanup for explicit retry
            self._ensure_housekeeping()
            return False
        return True

    def _ensure_housekeeping(self) -> None:
        """Retry retained cleanup even when no connection has succeeded yet."""
        if self._closing:
            return
        current = self._housekeeping
        if current is not None and not current.done():
            return
        if current is not None and not current.cancelled():
            current.exception()
        self._housekeeping = asyncio.create_task(self._housekeep())

    async def close_guild(self, guild_id: int, *, only_if_idle: bool = False) -> None:
        """Invalidate connects and close only this guild's owned resources."""
        running = self._removals.get(guild_id)
        if running is not None:
            await self._join_removal(guild_id, running[0])
            if only_if_idle or not running[1]:
                # The joined removal is at least as strict as this request.
                return
            # A weaker idle-sweep removal may have skipped a session that just
            # became active again; a full shutdown must close what it left.
        attempt = self._pending.get(guild_id)
        if attempt is not None:
            self._invalidate(attempt)
        removal = asyncio.create_task(self._close_guild(guild_id, attempt, only_if_idle))
        self._removals[guild_id] = (removal, only_if_idle)
        removal.add_done_callback(
            lambda task: self._removal_finished(guild_id, task)
        )
        await self._join_removal(guild_id, removal)

    async def _join_removal(self, guild_id: int, removal: asyncio.Task[None]) -> None:
        try:
            await asyncio.shield(removal)
        finally:
            entry = self._removals.get(guild_id)
            if removal.done() and entry is not None and entry[0] is removal:
                self._removals.pop(guild_id, None)

    def _removal_finished(self, guild_id: int, task: asyncio.Task[None]) -> None:
        entry = self._removals.get(guild_id)
        if entry is not None and entry[0] is task:
            self._removals.pop(guild_id, None)
        if not task.cancelled():
            task.exception()

    async def _close_guild(
        self, guild_id: int, attempt: _Connection | None, only_if_idle: bool = False
    ) -> None:
        if attempt is not None and attempt.task is not None:
            await asyncio.gather(attempt.task, return_exceptions=True)
            # A task cancelled before its first instruction cannot run finally.
            if self._pending.get(guild_id) is attempt:
                self._pending.pop(guild_id, None)
        sessions = {item for item in self._retired if item.guild_id == guild_id}
        if guild_id in self._sessions:
            sessions.add(self._sessions[guild_id])
        results = await asyncio.gather(
            *(self._try_dispose(item, only_if_idle=only_if_idle) for item in sessions)
        )
        if not all(results):
            raise PlaybackUnavailable()

    async def _housekeep(self) -> None:
        while True:
            await self._sleep(self._housekeeping_interval)
            try:
                await self._sweep()
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001 - one bad sweep must not end housekeeping
                log.warning("Housekeeping sweep failed (%s)", type(error).__name__)

    async def _sweep(self) -> None:
        # Failed disposal removes a session from _sessions but retains
        # ownership here. Retry it without touching an active handshake.
        for retired in tuple(self._retired):
            guild_id = retired.guild_id
            # Another guild's cleanup can yield long enough for this one
            # to recover and reconnect. Never act on its old identity.
            if (
                retired not in self._retired
                or guild_id in self._pending
                or guild_id in self._removals
            ):
                continue
            try:
                await self.close_guild(guild_id)
            except PlaybackUnavailable:
                continue
        now = self._clock()
        for guild_id, session in tuple(self._sessions.items()):
            state = session.snapshot()
            # A halted queue never advances on its own; holding voice open for
            # it would bypass the idle timeout indefinitely.
            if state.current is not None or (state.queued and not state.halted):
                self._empty_since.pop(guild_id, None)
                continue
            empty_since = self._empty_since.setdefault(guild_id, now)
            if now - empty_since >= self._idle_timeout:
                try:
                    # The decision comes from a snapshot, so let the session
                    # re-check under its own lock: a track that arrived in the
                    # meantime must not be discarded by a stale idle verdict.
                    await self.close_guild(guild_id, only_if_idle=True)
                except PlaybackUnavailable:
                    # Failed disposal remains owned and retryable by close.
                    continue

    async def close(self) -> None:
        """Fail closed, attempt every cleanup, and retry only unfinished work."""
        self._closing = True
        async with self._close_lock:
            if self._housekeeping is not None:
                self._housekeeping.cancel()
                await asyncio.gather(self._housekeeping, return_exceptions=True)
                self._housekeeping = None
            guild_ids = set(self._sessions) | set(self._pending) | set(self._removals)
            guild_ids.update(item.guild_id for item in self._retired)
            results = await asyncio.gather(
                *(self.close_guild(gid) for gid in guild_ids), return_exceptions=True
            )
            failed = any(isinstance(result, BaseException) for result in results)
            for resource, attribute in (
                (self._resolver, "_resolver_closed"),
                (self._source_factory, "_factory_closed"),
            ):
                if getattr(self, attribute):
                    continue
                try:
                    await resource.close()
                except Exception:  # noqa: BLE001 - sibling cleanup must still run
                    failed = True
                else:
                    setattr(self, attribute, True)
            if failed:
                raise PlaybackUnavailable()
