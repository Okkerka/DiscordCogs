"""Native registry ownership and concurrency tests without Discord network I/O."""

from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path
from types import MethodType, SimpleNamespace
from unittest.mock import AsyncMock, patch

import discord
import pytest

from TidalPlayerExp.playback import backend as backend_module
from TidalPlayerExp.playback.errors import PlaybackUnavailable
from TidalPlayerExp.playback.models import (
    PlaybackEntry,
    PlaybackSnapshot,
    ResolvedSource,
    SourceKind,
    SourceReference,
)


class Voice:
    def __init__(self, client, channel=None):
        channel = channel or client
        self.channel = channel
        self.guild = channel.guild
        self.connected = True
        self.disconnects = 0
        self.played = []
        self.after = None

    def is_connected(self):
        return self.connected

    def stop(self):
        if self.after is not None:
            self.after(None)

    def play(self, source, *, after):
        self.played.append(source)
        self.after = after

    def pause(self):
        pass

    def resume(self):
        pass

    async def disconnect(self, *, force=False):
        self.disconnects += 1
        self.connected = False
        self.guild.voice_client = None


class Channel:
    def __init__(self, guild, channel_id=10):
        self.guild = guild
        self.id = channel_id
        self.permissions = SimpleNamespace(connect=True, speak=True)
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.release.set()
        self.cancelled = asyncio.Event()
        self.calls = []
        self.late = False
        self.error = None
        self.voice = None

    def permissions_for(self, member):
        assert member is self.guild.me
        return self.permissions

    async def connect(self, *, cls=Voice, **kwargs):
        self.calls.append(kwargs)
        # Discord registers the constructed client before its handshake awaits.
        self.voice = cls(None, self)
        self.guild.voice_client = self.voice
        self.started.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            if not self.late:
                raise
            await self.release.wait()
        if self.error:
            raise self.error
        return self.voice


class Session:
    def __init__(self, guild_id, voice_client, resolver, source_factory, sink):
        self.guild_id = guild_id
        self.voice_client = voice_client
        self.closed = False
        self.failure = False
        self.state = PlaybackSnapshot(None, (), False, voice_client.channel.id)

    def snapshot(self):
        return self.state

    async def close(self):
        if self.failure:
            raise OSError("secret cleanup detail")
        self.closed = True
        if self.voice_client.guild.voice_client is self.voice_client:
            await self.voice_client.disconnect(force=True)


@pytest.fixture
def environment(monkeypatch):
    module = backend_module
    monkeypatch.setattr(discord, "VoiceClient", Voice)
    monkeypatch.setattr(module, "NativePlaybackSession", Session)
    guild = SimpleNamespace(id=1, me=object(), voice_client=None)
    channel = Channel(guild)
    bot = SimpleNamespace(audio=None)
    bot.get_cog = lambda name: bot.audio if name == "Audio" else None
    resolver = SimpleNamespace(close=AsyncMock())
    factory = SimpleNamespace(close=AsyncMock())
    backend = module.NativePlaybackBackend(bot, resolver, factory, SimpleNamespace())
    return SimpleNamespace(
        module=module,
        backend=backend,
        guild=guild,
        channel=channel,
        bot=bot,
        resolver=resolver,
        factory=factory,
    )


@pytest.mark.asyncio
async def test_duplicate_connect_reuses_one_session(environment):
    e = environment
    e.channel.release.clear()
    first = asyncio.create_task(e.backend.connect(e.guild, e.channel))
    await e.channel.started.wait()
    second = asyncio.create_task(e.backend.connect(e.guild, e.channel))
    await asyncio.sleep(0)
    e.channel.release.set()
    one, two = await asyncio.gather(first, second)
    assert one is two is await e.backend.get(1)
    assert e.channel.calls == [{"timeout": 20.0, "reconnect": True, "self_deaf": True}]
    assert await e.backend.connect(e.guild, e.channel) is one
    await e.backend.close()
    assert e.channel.voice.disconnects == 1
    assert await e.backend.get(1) is None
    await e.backend.close()
    e.resolver.close.assert_awaited_once()
    e.factory.close.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "obstacle", ["audio", "foreign", "wrong_guild", "connect", "speak"]
)
async def test_rejections_do_not_connect(environment, obstacle):
    e = environment
    if obstacle == "audio":
        e.bot.audio = object()
    elif obstacle == "foreign":
        e.guild.voice_client = object()
    elif obstacle == "wrong_guild":
        e.channel.guild = SimpleNamespace(id=2)
    else:
        setattr(e.channel.permissions, obstacle, False)
    with pytest.raises(PlaybackUnavailable):
        await e.backend.connect(e.guild, e.channel)
    assert not e.channel.calls
    await e.backend.close()


@pytest.mark.asyncio
async def test_unrelated_guild_and_different_pending_channel(environment):
    e = environment
    e.channel.release.clear()
    first = asyncio.create_task(e.backend.connect(e.guild, e.channel))
    await e.channel.started.wait()
    wrong = Channel(e.guild, 11)
    with pytest.raises(PlaybackUnavailable):
        await e.backend.connect(e.guild, wrong)
    guild = SimpleNamespace(id=2, me=object(), voice_client=None)
    other = await asyncio.wait_for(e.backend.connect(guild, Channel(guild)), 1)
    assert other.guild_id == 2
    e.channel.release.set()
    await first
    with pytest.raises(PlaybackUnavailable):
        await e.backend.connect(e.guild, wrong)
    await e.backend.close()


@pytest.mark.asyncio
async def test_cancelled_waiter_does_not_cancel_shared_connect(environment):
    e = environment
    e.channel.release.clear()
    first = asyncio.create_task(e.backend.connect(e.guild, e.channel))
    await e.channel.started.wait()
    second = asyncio.create_task(e.backend.connect(e.guild, e.channel))
    await asyncio.sleep(0)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    assert not e.channel.cancelled.is_set()
    e.channel.release.set()
    assert await second is await e.backend.get(1)
    await e.backend.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("shutdown", ["waiter", "guild", "backend"])
async def test_cancelled_attempt_cleans_late_client(environment, shutdown):
    e = environment
    e.channel.release.clear()
    e.channel.late = True
    waiter = asyncio.create_task(e.backend.connect(e.guild, e.channel))
    await e.channel.started.wait()
    if shutdown == "waiter":
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        closing = None
    else:
        operation = (
            e.backend.close_guild(1) if shutdown == "guild" else e.backend.close()
        )
        closing = asyncio.create_task(operation)
    await asyncio.wait_for(e.channel.cancelled.wait(), 1)
    e.channel.release.set()
    if closing:
        await closing
        with pytest.raises((PlaybackUnavailable, asyncio.CancelledError)):
            await waiter
    await e.backend.close()
    assert e.channel.voice.disconnects == 1
    assert await e.backend.get(1) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("shutdown", ["waiter", "guild", "backend"])
@pytest.mark.parametrize("replaced", [False, True])
async def test_cancelled_handshake_cleans_only_its_registered_client(environment, shutdown, replaced):
    e = environment
    e.channel.release.clear()
    waiter = asyncio.create_task(e.backend.connect(e.guild, e.channel))
    await asyncio.wait_for(e.channel.started.wait(), 1)
    owned = e.channel.voice
    assert e.guild.voice_client is owned
    replacement = Voice(e.channel) if replaced else None
    if replaced:
        e.guild.voice_client = replacement
    attempt = e.backend._pending[1].task
    if shutdown == "waiter":
        waiter.cancel()
    elif shutdown == "guild":
        await e.backend.close_guild(1)
    else:
        await e.backend.close()
    await asyncio.wait_for(asyncio.gather(waiter, attempt, return_exceptions=True), 1)
    try:
        assert owned.disconnects == (0 if replaced else 1)
        assert e.guild.voice_client is replacement
        if replacement is not None:
            assert replacement.disconnects == 0
        elif shutdown != "backend":
            e.channel.release.set()
            assert await e.backend.connect(e.guild, e.channel)
    finally:
        await e.backend.close()


@pytest.mark.asyncio
async def test_failed_handshake_cleans_registered_client_before_retry(environment):
    e = environment
    e.channel.error = OSError("private handshake failure")
    try:
        with pytest.raises(PlaybackUnavailable) as caught:
            await e.backend.connect(e.guild, e.channel)
        assert caught.value.__context__ is None
        assert e.channel.voice.disconnects == 1
        assert e.guild.voice_client is None
        e.channel.error = None
        assert await e.backend.connect(e.guild, e.channel)
    finally:
        await e.backend.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("interruption", ["cancel", "error", "timeout"])
async def test_real_discord_connect_cleans_interrupted_handshake(environment, monkeypatch, interruption):
    """Exercise Discord's actual register-before-handshake contract without I/O."""
    e = environment

    class HandshakingVoice(Voice, discord.VoiceProtocol):
        def __init__(self, client, channel):
            super().__init__(client, channel)
            channel.voice = self

        async def connect(self, **kwargs):
            e.channel.started.set()
            await e.channel.release.wait()
            if e.channel.error is not None:
                raise e.channel.error

    monkeypatch.setattr(discord, "VoiceClient", HandshakingVoice)
    e.channel._get_voice_client_key = lambda: (1, 1)
    e.channel._state = SimpleNamespace(
        _get_voice_client=lambda _: e.guild.voice_client,
        _get_client=lambda: None,
        _add_voice_client=lambda _, voice: setattr(e.guild, "voice_client", voice),
    )
    e.channel.connect = MethodType(discord.abc.Connectable.connect, e.channel)
    e.channel.release.clear()
    waiter = asyncio.create_task(e.backend.connect(e.guild, e.channel))
    await asyncio.wait_for(e.channel.started.wait(), 1)
    owned = e.guild.voice_client
    attempt = e.backend._pending[1].task
    if interruption == "cancel":
        waiter.cancel()
    else:
        e.channel.error = TimeoutError("private") if interruption == "timeout" else OSError("private")
        e.channel.release.set()
    await asyncio.wait_for(asyncio.gather(waiter, attempt, return_exceptions=True), 1)
    try:
        assert owned.disconnects == 1
        assert e.guild.voice_client is None
        e.channel.error = None
        e.channel.release.set()
        assert await e.backend.connect(e.guild, e.channel)
    finally:
        await e.backend.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [TimeoutError("secret"), OSError("secret")])
async def test_connect_failures_are_sanitized_and_retryable(environment, failure):
    e = environment
    e.channel.error = failure
    with pytest.raises(PlaybackUnavailable) as caught:
        await e.backend.connect(e.guild, e.channel)
    assert caught.value.__context__ is None
    e.channel.error = None
    assert await e.backend.connect(e.guild, e.channel)
    await e.backend.close()


@pytest.mark.asyncio
async def test_replacement_is_never_disconnected_and_stale_session_removed(environment):
    e = environment
    old = await e.backend.connect(e.guild, e.channel)
    replacement = Voice(e.channel)
    e.guild.voice_client = replacement
    await e.backend.close_guild(1)
    assert old.closed
    assert old.voice_client.disconnects == replacement.disconnects == 0
    assert e.guild.voice_client is replacement
    await e.backend.close()


@pytest.mark.asyncio
async def test_disconnected_owned_session_is_cleaned_before_reconnect(environment):
    e = environment
    old = await e.backend.connect(e.guild, e.channel)
    old.voice_client.connected = False
    new = await e.backend.connect(e.guild, e.channel)
    assert old.closed and new is not old
    await e.backend.close()


@pytest.mark.asyncio
async def test_audio_loaded_during_connect_prevents_publication(environment):
    e = environment
    e.channel.release.clear()
    waiter = asyncio.create_task(e.backend.connect(e.guild, e.channel))
    await e.channel.started.wait()
    e.bot.audio = object()
    e.channel.release.set()
    with pytest.raises(PlaybackUnavailable):
        await waiter
    assert e.channel.voice.disconnects == 1
    assert await e.backend.get(1) is None
    await e.backend.close()


@pytest.mark.asyncio
async def test_close_failure_retries_without_skipping_other_resources(environment):
    e = environment
    session = await e.backend.connect(e.guild, e.channel)
    session.failure = True
    e.resolver.close.side_effect = OSError("private")
    with pytest.raises(PlaybackUnavailable):
        await e.backend.close()
    e.factory.close.assert_awaited_once()
    with pytest.raises(PlaybackUnavailable):
        await e.backend.connect(e.guild, e.channel)
    session.failure = False
    e.resolver.close.side_effect = None
    await e.backend.close()
    assert session.closed
    assert e.resolver.close.await_count == 2
    e.factory.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_housekeeping_resets_idle_clock_for_activity(environment):
    e = environment
    now = [0.0]
    ticks = asyncio.Queue()
    slept = asyncio.Event()

    async def sleep(interval):
        assert interval == 30.0
        slept.set()
        await ticks.get()

    async def tick(at):
        await slept.wait()
        slept.clear()
        now[0] = at
        ticks.put_nowait(None)
        await slept.wait()

    e.backend._clock = lambda: now[0]
    e.backend._sleep = sleep
    session = await e.backend.connect(e.guild, e.channel)
    await tick(100)
    assert not session.closed
    session.state = PlaybackSnapshot(object(), (), True, 10)
    await tick(200)
    assert not session.closed
    session.state = PlaybackSnapshot(None, (object(),), False, 10)
    await tick(300)
    assert not session.closed
    session.state = PlaybackSnapshot(None, (), False, 10)
    await tick(400)
    await tick(519)
    assert not session.closed
    await tick(520)
    assert session.closed
    await e.backend.close()


@pytest.mark.asyncio
async def test_one_session_cleanup_failure_does_not_skip_other_guild(environment):
    e = environment
    broken = await e.backend.connect(e.guild, e.channel)
    guild = SimpleNamespace(id=2, me=object(), voice_client=None)
    healthy = await e.backend.connect(guild, Channel(guild))
    broken.failure = True
    with pytest.raises(PlaybackUnavailable):
        await e.backend.close()
    assert healthy.closed
    broken.failure = False
    await e.backend.close()
    assert broken.closed


@pytest.mark.asyncio
async def test_replacement_during_connect_is_untouched(environment):
    e = environment
    replacement = Voice(e.channel)
    original_connect = e.channel.connect

    async def replaced_connect(**kwargs):
        voice = await original_connect(**kwargs)
        e.guild.voice_client = replacement
        return voice

    e.channel.connect = replaced_connect
    with pytest.raises(PlaybackUnavailable):
        await e.backend.connect(e.guild, e.channel)
    assert e.channel.voice.disconnects == replacement.disconnects == 0
    assert e.guild.voice_client is replacement
    assert await e.backend.get(1) is None
    await e.backend.close()


@pytest.mark.asyncio
async def test_cancelled_removal_can_retry_a_cleanup_failure(environment):
    e = environment
    session = await e.backend.connect(e.guild, e.channel)
    entered = asyncio.Event()
    release = asyncio.Event()
    original_close = session.close

    async def failing_close():
        entered.set()
        await release.wait()
        raise OSError("private")

    session.close = failing_close
    removal = asyncio.create_task(e.backend.close_guild(1))
    await entered.wait()
    owned_removal = e.backend._removals[1]
    removal.cancel()
    with pytest.raises(asyncio.CancelledError):
        await removal
    release.set()
    await asyncio.gather(owned_removal, return_exceptions=True)
    session.close = original_close
    await e.backend.close()
    assert session.closed


async def _real_session_integration():
    """Use actual native modules and Discord types, with fake network/audio."""
    import discord

    from TidalPlayerExp.playback.backend import NativePlaybackBackend
    from TidalPlayerExp.playback.session import NativePlaybackSession

    class Audio(discord.AudioSource):
        def __init__(self):
            self.cleanups = 0

        def cleanup(self):
            self.cleanups += 1

        def read(self):
            return b"audio"

        def is_opus(self):
            return True

    audio = Audio()
    started = asyncio.Event()
    resolver = SimpleNamespace(
        resolve=AsyncMock(return_value=ResolvedSource("https://example.com/audio", {})),
        close=AsyncMock(),
    )
    factory = SimpleNamespace(create=AsyncMock(return_value=audio), close=AsyncMock())
    sink = SimpleNamespace(
        track_started=AsyncMock(side_effect=lambda *_: started.set()),
        track_failed=AsyncMock(),
        queue_ended=AsyncMock(),
    )
    guild = SimpleNamespace(id=1, me=object(), voice_client=None)
    channel = Channel(guild)
    backend = NativePlaybackBackend(
        SimpleNamespace(get_cog=lambda _: None), resolver, factory, sink
    )
    with patch.object(backend_module.discord, "VoiceClient", Voice):
        session = await backend.connect(guild, channel)
    assert isinstance(session, NativePlaybackSession)
    entry = PlaybackEntry(
        "one",
        SourceReference(SourceKind.TIDAL, "1"),
        None,
        {
            "title": "One",
            "artist": "Artist",
            "album": None,
            "duration": 10,
            "quality": "LOSSLESS",
            "image": None,
            "share_url": None,
            "audio_resolution": None,
            "track_id": 1,
        },
        42,
    )
    assert await session.enqueue(entry)
    await asyncio.wait_for(started.wait(), 2)
    assert session.snapshot().current == entry
    assert len(channel.voice.played) == 1
    await backend.close()
    assert audio.cleanups == 1
    assert channel.voice.disconnects == 1
    assert guild.voice_client is None
    assert await backend.get(1) is None
    resolver.close.assert_awaited_once()
    factory.close.assert_awaited_once()


def test_real_session_fake_voice_integration():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import asyncio; from TidalPlayerExp.tests.test_native_backend import "
                "_real_session_integration; asyncio.run(_real_session_integration())"
            ),
        ],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
