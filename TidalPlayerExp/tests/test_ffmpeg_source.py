"""Behavioral tests for the bounded native FFmpeg source factory."""

from __future__ import annotations

import asyncio
import importlib
import io
import os
import shutil
import subprocess
import sys
import threading
import time
import traceback
import types
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

import pytest

from TidalPlayerExp.playback.errors import PlaybackStartError, PlaybackUnavailable
from TidalPlayerExp.playback.models import ResolvedSource


class _AudioSourceStub:
    def cleanup(self) -> None:
        return None


class _OggStreamStub:
    def __init__(self, stream: _PacketStream) -> None:
        self._stream = stream

    def iter_packets(self) -> Iterator[bytes]:
        for packet in self._stream.packets:
            if isinstance(packet, BaseException):
                raise packet
            yield packet


@pytest.fixture()
def ffmpeg_module(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Import the module against the suite's deliberately small Discord stub."""

    import discord

    oggparse = types.ModuleType("discord.oggparse")
    oggparse.OggStream = _OggStreamStub
    monkeypatch.setattr(discord, "AudioSource", _AudioSourceStub, raising=False)
    monkeypatch.setattr(discord, "oggparse", oggparse, raising=False)
    monkeypatch.setitem(sys.modules, "discord.oggparse", oggparse)
    sys.modules.pop("TidalPlayerExp.playback.ffmpeg", None)
    return importlib.import_module("TidalPlayerExp.playback.ffmpeg")


class _PacketStream:
    def __init__(self, packets: Sequence[bytes | BaseException]) -> None:
        self.packets = list(packets)
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _Process:
    def __init__(
        self,
        packets: Sequence[bytes | BaseException] = (
            b"OpusHead-data",
            b"OpusTags-data",
            b"\x08audio",
        ),
        *,
        returncode: int | None = 0,
        terminate_exits: bool = True,
    ) -> None:
        self.stdout = _PacketStream(packets)
        self.returncode = returncode
        self.terminate_exits = terminate_exits
        self.terminate_calls = 0
        self.kill_calls = 0
        self.wait_calls = 0

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.terminate_calls += 1
        if self.terminate_exits:
            self.returncode = -15

    def kill(self) -> None:
        self.kill_calls += 1
        self.returncode = -9

    def wait(self, timeout: float | None = None) -> int:
        del timeout
        self.wait_calls += 1
        if self.returncode is None:
            raise subprocess.TimeoutExpired("redacted", 0)
        return self.returncode


def _probe_success(calls: list[tuple[tuple[str, ...], float]]):
    def probe(argv: Sequence[str], timeout: float) -> bytes:
        calls.append((tuple(argv), timeout))
        if argv[-1] == "-version":
            return b"ffmpeg version 7.1-static Copyright"
        if argv[-1] == "-encoders":
            return b" A..... libopus             libopus Opus"
        if argv[-1] == "-muxers":
            return b" E opus            Ogg Opus"
        raise AssertionError(argv)

    return probe


def _factory(ffmpeg_module: Any, tmp_path: Path, spawn: Any):
    executable = tmp_path / "ffmpeg.exe"
    executable.write_bytes(b"binary")
    executable.chmod(0o755)
    probe_calls: list[tuple[tuple[str, ...], float]] = []
    factory = ffmpeg_module.FFmpegSourceFactory(
        locator=lambda: str(executable),
        probe=_probe_success(probe_calls),
        spawn=spawn,
    )
    return factory, executable.resolve(), probe_calls


def _error_surface(error: BaseException) -> str:
    formatted = "".join(
        traceback.format_exception(type(error), error, error.__traceback__)
    )
    return "\n".join(
        (
            str(error),
            repr(error),
            formatted,
            repr(error.__cause__),
            repr(error.__context__),
        )
    )


def _pid_exists(pid: int) -> bool:
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        open_process = kernel32.OpenProcess
        open_process.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        open_process.restype = wintypes.HANDLE
        get_exit_code = kernel32.GetExitCodeProcess
        get_exit_code.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        get_exit_code.restype = wintypes.BOOL
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = (wintypes.HANDLE,)
        close_handle.restype = wintypes.BOOL

        process_query_limited_information = 0x1000
        error_invalid_parameter = 87
        still_active = 259
        handle = open_process(process_query_limited_information, False, pid)
        if not handle:
            error = ctypes.get_last_error()
            if error == error_invalid_parameter:
                return False
            raise AssertionError(f"OpenProcess failed with WinError {error}") from None

        exit_code = wintypes.DWORD()
        queried = get_exit_code(handle, ctypes.byref(exit_code))
        query_error = ctypes.get_last_error() if not queried else 0
        closed = close_handle(handle)
        close_error = ctypes.get_last_error() if not closed else 0
        if not queried:
            raise AssertionError(
                f"GetExitCodeProcess failed with WinError {query_error}"
            ) from None
        if not closed:
            raise AssertionError(
                f"CloseHandle failed with WinError {close_error}"
            ) from None
        return exit_code.value == still_active
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


@pytest.mark.skipif(os.name != "nt", reason="Win32 process helper positive control")
def test_pid_exists_recognizes_current_windows_process() -> None:
    assert _pid_exists(os.getpid())


def test_default_locator_uses_packaged_files_without_upstream_probe(
    ffmpeg_module: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    import imageio_ffmpeg

    def forbidden(*_args: Any, **_kwargs: Any) -> str:
        pytest.fail("default discovery must not run imageio-ffmpeg validation")

    monkeypatch.delenv("IMAGEIO_FFMPEG_EXE", raising=False)
    monkeypatch.setattr(imageio_ffmpeg, "get_ffmpeg_exe", forbidden)
    monkeypatch.setattr(subprocess, "check_call", forbidden)

    located = Path(ffmpeg_module._default_locator())

    assert located.is_absolute()
    assert located.is_file()
    assert "imageio_ffmpeg" in located.parts
    assert "binaries" in located.parts


def test_default_probe_times_out_and_reaps_hung_candidate(
    ffmpeg_module: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    spawned: list[subprocess.Popen[bytes]] = []
    original_popen = subprocess.Popen

    def record_spawn(*args: Any, **kwargs: Any) -> subprocess.Popen[bytes]:
        process = original_popen(*args, **kwargs)
        spawned.append(process)
        return process

    monkeypatch.setattr(subprocess, "Popen", record_spawn)
    started = time.monotonic()

    with pytest.raises(subprocess.TimeoutExpired):
        ffmpeg_module._default_probe(
            (sys.executable, "-c", "import time; time.sleep(60)"), timeout=0.2
        )

    elapsed = time.monotonic() - started
    assert len(spawned) == 1
    assert elapsed < 2
    assert spawned[0].returncode is not None
    assert not _pid_exists(spawned[0].pid)


@pytest.mark.asyncio
async def test_close_accounts_for_timed_out_owned_probe(
    ffmpeg_module: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    spawned: list[subprocess.Popen[bytes]] = []
    started = asyncio.Event()
    loop = asyncio.get_running_loop()
    original_popen = subprocess.Popen

    def record_spawn(*args: Any, **kwargs: Any) -> subprocess.Popen[bytes]:
        process = original_popen(*args, **kwargs)
        spawned.append(process)
        loop.call_soon_threadsafe(started.set)
        return process

    def hung_probe(_argv: Sequence[str], timeout: float) -> bytes:
        return ffmpeg_module._default_probe(
            (sys.executable, "-c", "import time; time.sleep(60)"), timeout
        )

    monkeypatch.setattr(ffmpeg_module, "_PROBE_TIMEOUT_SECONDS", 0.2)
    monkeypatch.setattr(subprocess, "Popen", record_spawn)
    factory = ffmpeg_module.FFmpegSourceFactory(
        locator=lambda: sys.executable,
        probe=hung_probe,
    )
    checking = asyncio.create_task(factory.check())
    await asyncio.wait_for(started.wait(), timeout=2)

    await factory.close()
    await factory.close()
    with pytest.raises(PlaybackUnavailable):
        await checking

    assert len(spawned) == 1
    assert spawned[0].returncode is not None
    assert not _pid_exists(spawned[0].pid)
    with pytest.raises(PlaybackUnavailable):
        await factory.check()


@pytest.mark.asyncio
async def test_check_coalesces_exact_bounded_probes_and_returns_immutable_capability(
    ffmpeg_module: Any, tmp_path: Path
) -> None:
    gate = threading.Event()
    calls: list[tuple[tuple[str, ...], float]] = []
    executable = tmp_path / "ffmpeg.exe"
    executable.write_bytes(b"binary")
    executable.chmod(0o755)

    def probe(argv: Sequence[str], timeout: float) -> bytes:
        gate.wait(1)
        return _probe_success(calls)(argv, timeout)

    factory = ffmpeg_module.FFmpegSourceFactory(
        locator=lambda: str(executable), probe=probe
    )
    first = asyncio.create_task(factory.check())
    second = asyncio.create_task(factory.check())
    await asyncio.sleep(0)
    gate.set()
    first_result, second_result = await asyncio.gather(first, second)
    again = await factory.check()

    assert first_result is second_result is again
    assert first_result == ffmpeg_module.FFmpegCapability(
        str(executable.resolve()), "7.1-static", True, True
    )
    with pytest.raises((AttributeError, TypeError)):
        first_result.version = "changed"
    assert calls == [
        (
            (
                str(executable.resolve()),
                "-hide_banner",
                "-loglevel",
                "quiet",
                "-version",
            ),
            10.0,
        ),
        (
            (
                str(executable.resolve()),
                "-hide_banner",
                "-loglevel",
                "quiet",
                "-encoders",
            ),
            10.0,
        ),
        (
            (
                str(executable.resolve()),
                "-hide_banner",
                "-loglevel",
                "quiet",
                "-muxers",
            ),
            10.0,
        ),
    ]


@pytest.mark.asyncio
async def test_bare_path_locator_resolves_executable_outside_current_directory(
    ffmpeg_module: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binary_dir = tmp_path / "bin"
    binary_dir.mkdir()
    executable = binary_dir / ("ffmpeg.exe" if os.name == "nt" else "ffmpeg")
    executable.write_bytes(b"binary")
    executable.chmod(0o755)
    working_dir = tmp_path / "working"
    working_dir.mkdir()
    monkeypatch.setenv("PATH", str(binary_dir))
    monkeypatch.chdir(working_dir)
    probe_calls: list[tuple[tuple[str, ...], float]] = []
    spawn_calls: list[tuple[str, ...]] = []

    def spawn(argv: Sequence[str], **_kwargs: Any) -> _Process:
        spawn_calls.append(tuple(argv))
        return _Process()

    factory = ffmpeg_module.FFmpegSourceFactory(
        locator=lambda: "ffmpeg",
        probe=_probe_success(probe_calls),
        spawn=spawn,
    )

    capability = await factory.check()
    audio = await factory.create(ResolvedSource("https://media.example/stream", {}))

    found = shutil.which("ffmpeg")
    assert found is not None
    expected = str(Path(found).resolve())
    assert capability.executable == expected
    assert [call[0][0] for call in probe_calls] == [expected, expected, expected]
    assert spawn_calls[0][0] == expected
    audio.cleanup()


@pytest.mark.asyncio
async def test_close_waits_for_retained_check_then_rejects_new_work(
    ffmpeg_module: Any, tmp_path: Path
) -> None:
    entered = threading.Event()
    release = threading.Event()
    calls: list[tuple[tuple[str, ...], float]] = []
    executable = tmp_path / "ffmpeg.exe"
    executable.write_bytes(b"binary")
    executable.chmod(0o755)

    def probe(argv: Sequence[str], timeout: float) -> bytes:
        entered.set()
        release.wait(2)
        return _probe_success(calls)(argv, timeout)

    factory = ffmpeg_module.FFmpegSourceFactory(
        locator=lambda: str(executable), probe=probe
    )
    checking = asyncio.create_task(factory.check())
    assert await asyncio.to_thread(entered.wait, 1)
    closing = asyncio.create_task(factory.close())
    await asyncio.sleep(0)
    assert not closing.done()

    release.set()
    capability = await checking
    await closing
    await factory.close()

    assert capability.libopus
    with pytest.raises(PlaybackUnavailable):
        await factory.check()
    with pytest.raises(PlaybackUnavailable):
        await factory.create(ResolvedSource("https://media.example/stream", {}))


@pytest.mark.asyncio
async def test_close_before_check_is_idempotent_and_does_not_locate(
    ffmpeg_module: Any,
) -> None:
    located = False

    def locator() -> str:
        nonlocal located
        located = True
        return "unused"

    factory = ffmpeg_module.FFmpegSourceFactory(locator=locator)

    await asyncio.gather(factory.close(), factory.close())

    assert not located
    with pytest.raises(PlaybackUnavailable):
        await factory.check()


@pytest.mark.asyncio
async def test_cancelled_create_during_check_leaves_probe_tracked_for_close(
    ffmpeg_module: Any, tmp_path: Path
) -> None:
    entered = threading.Event()
    release = threading.Event()
    probe_calls: list[tuple[tuple[str, ...], float]] = []
    spawn_calls = 0
    executable = tmp_path / "ffmpeg.exe"
    executable.write_bytes(b"binary")
    executable.chmod(0o755)

    def probe(argv: Sequence[str], timeout: float) -> bytes:
        entered.set()
        release.wait(2)
        return _probe_success(probe_calls)(argv, timeout)

    def spawn(_argv: Sequence[str], **_kwargs: Any) -> _Process:
        nonlocal spawn_calls
        spawn_calls += 1
        return _Process()

    factory = ffmpeg_module.FFmpegSourceFactory(
        locator=lambda: str(executable), probe=probe, spawn=spawn
    )
    creating = asyncio.create_task(
        factory.create(ResolvedSource("https://media.example/stream", {}))
    )
    assert await asyncio.to_thread(entered.wait, 1)
    creating.cancel()
    with pytest.raises(asyncio.CancelledError):
        await creating

    closing = asyncio.create_task(factory.close())
    await asyncio.sleep(0)
    assert not closing.done()
    release.set()
    await closing

    assert len(probe_calls) == 3
    assert spawn_calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["missing", "probe"])
async def test_check_sanitizes_locator_and_probe_failures(
    ffmpeg_module: Any, tmp_path: Path, failure: str
) -> None:
    secret = "signed-token-do-not-retain"
    if failure == "missing":
        locator = lambda: str(tmp_path / secret)
        probe = _probe_success([])
    else:
        executable = tmp_path / "ffmpeg.exe"
        executable.write_bytes(b"binary")
        executable.chmod(0o755)
        locator = lambda: str(executable)

        def probe(_argv: Sequence[str], _timeout: float) -> bytes:
            raise OSError(secret)

    factory = ffmpeg_module.FFmpegSourceFactory(locator=locator, probe=probe)

    with pytest.raises(PlaybackUnavailable) as caught:
        await factory.check()

    assert secret not in _error_surface(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("encoders", "muxers", "source"),
    [
        (
            b" A..... aac AAC",
            b" E opus Ogg Opus",
            ResolvedSource("https://media.example/a", {}),
        ),
        (
            b" A..... libopus libopus Opus",
            b" E ogg Ogg",
            ResolvedSource(
                "https://media.example/b",
                {},
                codec="opus",
                sample_rate=48_000,
                channels=2,
            ),
        ),
    ],
)
async def test_create_rejects_missing_required_encoder_or_muxer(
    ffmpeg_module: Any,
    tmp_path: Path,
    encoders: bytes,
    muxers: bytes,
    source: ResolvedSource,
) -> None:
    executable = tmp_path / "ffmpeg.exe"
    executable.write_bytes(b"binary")
    executable.chmod(0o755)

    def probe(argv: Sequence[str], _timeout: float) -> bytes:
        return {
            "-version": b"ffmpeg version 7.1-static",
            "-encoders": encoders,
            "-muxers": muxers,
        }[argv[-1]]

    factory = ffmpeg_module.FFmpegSourceFactory(
        locator=lambda: str(executable),
        probe=probe,
        spawn=lambda _argv, **_kwargs: pytest.fail(
            "missing capability must prevent spawn"
        ),
    )

    with pytest.raises(PlaybackUnavailable):
        await factory.create(source)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("codec", "sample_rate", "channels", "expected_codec"),
    [
        ("opus", 48_000, 2, ("-c:a", "copy")),
        ("OPUS", 48_000, 2, ("-c:a", "copy")),
        ("opus", 44_100, 2, ("-c:a", "libopus")),
        ("opus", 48_000, 1, ("-c:a", "libopus")),
        ("aac", 48_000, 2, ("-c:a", "libopus")),
        (None, None, None, ("-c:a", "libopus")),
    ],
)
async def test_create_uses_safe_exact_argv_and_copy_transcode_matrix(
    ffmpeg_module: Any,
    tmp_path: Path,
    codec: str | None,
    sample_rate: int | None,
    channels: int | None,
    expected_codec: tuple[str, str],
) -> None:
    calls: list[tuple[tuple[str, ...], dict[str, Any]]] = []
    process = _Process()

    def spawn(argv: Sequence[str], **kwargs: Any) -> _Process:
        calls.append((tuple(argv), kwargs))
        return process

    factory, executable, _ = _factory(ffmpeg_module, tmp_path, spawn)
    source = ResolvedSource(
        "https://media.example/stream?signature=secret",
        {
            "origin": "https://listen.example",
            "AUTHORIZATION": "discard-me",
            "user-agent": "Player/1.0",
            "Referer": "https://app.example/",
            "X-Ignored": "ignored",
        },
        codec=codec,
        sample_rate=sample_rate,
        channels=channels,
    )

    audio = await factory.create(source)

    argv, kwargs = calls[0]
    common = (
        str(executable),
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-rw_timeout",
        "15000000",
        "-reconnect",
        "1",
        "-reconnect_streamed",
        "1",
        "-reconnect_delay_max",
        "5",
        "-probesize",
        "1048576",
        "-analyzeduration",
        "5000000",
        "-protocol_whitelist",
        "https,tls,tcp,crypto",
        "-headers",
        "User-Agent: Player/1.0\r\nReferer: https://app.example/\r\nOrigin: https://listen.example\r\n",
        "-i",
        "https://media.example/stream?signature=secret",
        "-map",
        "0:a:0",
        "-vn",
        "-sn",
        "-dn",
        "-map_metadata",
        "-1",
    )
    assert argv[: len(common)] == common
    assert argv[len(common) : len(common) + 2] == expected_codec
    if expected_codec[1] == "copy":
        assert argv[len(common) :] == ("-c:a", "copy", "-f", "opus", "pipe:1")
    else:
        assert argv[len(common) :] == (
            "-c:a",
            "libopus",
            "-ar",
            "48000",
            "-ac",
            "2",
            "-b:a",
            "128k",
            "-frame_duration",
            "20",
            "-f",
            "opus",
            "pipe:1",
        )
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert kwargs["stdout"] is subprocess.PIPE
    assert kwargs["stderr"] is subprocess.PIPE
    assert kwargs["shell"] is False
    assert kwargs["creationflags"] == (
        subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    )
    assert repr(audio) == "FFmpegAudioSource(<redacted>)"
    assert audio.is_opus() is True
    assert audio.read() == b"\x08audio"
    assert audio.read() == b""
    audio.cleanup()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("url_suffix", "headers"),
    [
        ("\r-injected", {}),
        ("\x00-injected", {}),
        ("", {"User-Agent": "safe\nInjected: true"}),
        ("", {"Origin": "safe\x7funsafe"}),
        ("", {"USER-AGENT": "one", "User-Agent": "two"}),
    ],
)
async def test_create_rejects_controls_and_duplicate_allowed_headers_without_spawning(
    ffmpeg_module: Any, tmp_path: Path, url_suffix: str, headers: dict[str, str]
) -> None:
    spawn_calls = 0

    def spawn(_argv: Sequence[str], **_kwargs: Any) -> _Process:
        nonlocal spawn_calls
        spawn_calls += 1
        return _Process()

    factory, _, _ = _factory(ffmpeg_module, tmp_path, spawn)
    resolved = ResolvedSource(f"https://media.example/{url_suffix}", headers)

    with pytest.raises(PlaybackStartError):
        await factory.create(resolved)

    assert spawn_calls == 0


@pytest.mark.asyncio
async def test_create_redacts_constructor_failure_traceback(
    ffmpeg_module: Any, tmp_path: Path
) -> None:
    secret = "https://media.example/private?token=spawn-secret"

    def spawn(_argv: Sequence[str], **_kwargs: Any) -> _Process:
        raise OSError(secret)

    factory, _, _ = _factory(ffmpeg_module, tmp_path, spawn)

    with pytest.raises(PlaybackStartError) as caught:
        await factory.create(ResolvedSource(secret, {}))

    assert secret not in _error_surface(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


@pytest.mark.asyncio
async def test_prime_returns_first_audio_packet_exactly_once_and_skips_headers(
    ffmpeg_module: Any, tmp_path: Path
) -> None:
    process = _Process(
        (b"OpusHead-private", b"OpusTags-private", b"\x08first", b"\x08second")
    )
    factory, _, _ = _factory(ffmpeg_module, tmp_path, lambda _argv, **_kwargs: process)

    audio = await factory.create(
        ResolvedSource(
            "https://media.example/stream",
            {},
            codec="opus",
            sample_rate=48_000,
            channels=2,
        )
    )

    assert audio.read() == b"\x08first"
    assert audio.read() == b"\x08second"
    assert audio.read() == b""


@pytest.mark.asyncio
async def test_copy_retries_once_with_transcode_when_primed_packet_is_not_20ms(
    ffmpeg_module: Any, tmp_path: Path
) -> None:
    processes = [_Process((b"OpusHead", b"OpusTags", b"\x00ten-ms")), _Process()]
    calls: list[tuple[str, ...]] = []

    def spawn(argv: Sequence[str], **_kwargs: Any) -> _Process:
        calls.append(tuple(argv))
        return processes[len(calls) - 1]

    factory, _, _ = _factory(ffmpeg_module, tmp_path, spawn)
    audio = await factory.create(
        ResolvedSource(
            "https://media.example/stream",
            {},
            codec="opus",
            sample_rate=48_000,
            channels=2,
        )
    )

    assert len(calls) == 2
    assert ("-c:a", "copy") == calls[0][
        calls[0].index("-c:a") : calls[0].index("-c:a") + 2
    ]
    assert ("-c:a", "libopus") == calls[1][
        calls[1].index("-c:a") : calls[1].index("-c:a") + 2
    ]
    assert processes[0].stdout.closed
    assert audio.read() == b"\x08audio"


@pytest.mark.asyncio
async def test_copy_validates_every_later_packet_before_returning_it(
    ffmpeg_module: Any, tmp_path: Path
) -> None:
    process = _Process((b"OpusHead", b"OpusTags", b"\x08first", b"\x00ten-ms"))
    factory, _, _ = _factory(ffmpeg_module, tmp_path, lambda _argv, **_kwargs: process)
    audio = await factory.create(
        ResolvedSource(
            "https://media.example/stream",
            {},
            codec="opus",
            sample_rate=48_000,
            channels=2,
        )
    )

    assert audio.read() == b"\x08first"
    with pytest.raises(PlaybackStartError) as caught:
        audio.read()

    assert str(caught.value) == "Playback start failed"
    assert process.stdout.closed
    assert isinstance(audio._current_error, PlaybackStartError)


@pytest.mark.asyncio
async def test_read_detects_nonzero_exit_that_races_with_stdout_eof(
    ffmpeg_module: Any, tmp_path: Path
) -> None:
    class ExitAfterEofProcess(_Process):
        def __init__(self) -> None:
            super().__init__((b"OpusHead", b"OpusTags", b"\x08first"), returncode=None)

        def wait(self, timeout: float | None = None) -> int:
            del timeout
            self.wait_calls += 1
            self.returncode = 9
            return self.returncode

    process = ExitAfterEofProcess()
    factory, _, _ = _factory(ffmpeg_module, tmp_path, lambda _argv, **_kwargs: process)
    audio = await factory.create(ResolvedSource("https://media.example/stream", {}))

    assert audio.read() == b"\x08first"
    with pytest.raises(PlaybackStartError):
        audio.read()

    assert process.wait_calls >= 1
    assert process.stdout.closed


@pytest.mark.asyncio
async def test_read_treats_unconfirmed_eof_timeout_as_sanitized_failure(
    ffmpeg_module: Any, tmp_path: Path
) -> None:
    process = _Process(
        (b"OpusHead", b"OpusTags", b"\x08first"),
        returncode=None,
    )
    factory, _, _ = _factory(ffmpeg_module, tmp_path, lambda _argv, **_kwargs: process)
    audio = await factory.create(ResolvedSource("https://media.example/stream", {}))

    assert audio.read() == b"\x08first"
    with pytest.raises(PlaybackStartError) as caught:
        audio.read()

    assert str(caught.value) == "Playback start failed"
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert process.terminate_calls == 1
    assert process.stdout.closed


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "process",
    [
        _Process((b"OpusHead", RuntimeError("parser leaked a secret"))),
        _Process((b"OpusHead", b"OpusTags"), returncode=7),
    ],
)
async def test_prime_sanitizes_malformed_ogg_and_nonzero_eof(
    ffmpeg_module: Any, tmp_path: Path, process: _Process
) -> None:
    factory, _, _ = _factory(ffmpeg_module, tmp_path, lambda _argv, **_kwargs: process)

    with pytest.raises(PlaybackStartError) as caught:
        await factory.create(ResolvedSource("https://media.example/stream", {}))

    assert "secret" not in _error_surface(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert process.stdout.closed


@pytest.mark.asyncio
async def test_cancellation_during_delayed_construction_cleans_eventual_source(
    ffmpeg_module: Any, tmp_path: Path
) -> None:
    entered = threading.Event()
    release = threading.Event()
    process = _Process()

    def spawn(_argv: Sequence[str], **_kwargs: Any) -> _Process:
        entered.set()
        release.wait(2)
        return process

    factory, _, _ = _factory(ffmpeg_module, tmp_path, spawn)
    await factory.check()
    task = asyncio.create_task(
        factory.create(ResolvedSource("https://media.example/stream", {}))
    )
    assert await asyncio.to_thread(entered.wait, 1)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    for _ in range(100):
        if process.stdout.closed:
            break
        await asyncio.sleep(0.01)
    assert process.stdout.closed


@pytest.mark.asyncio
async def test_timeout_during_priming_cleans_process(
    ffmpeg_module: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entered = threading.Event()
    priming_finished = threading.Event()
    original_prime = ffmpeg_module._FFmpegAudioSource.prime

    def prime(source):
        try:
            original_prime(source)
        finally:
            priming_finished.set()

    monkeypatch.setattr(ffmpeg_module._FFmpegAudioSource, "prime", prime)

    class BlockingStream(_PacketStream):
        def close(self):
            super().close()
            # Ensure the worker observes our SIGTERM before reporting the timeout.
            assert priming_finished.wait(1)

    class BlockingOggStream:
        def __init__(self, stream: BlockingStream) -> None:
            self.stream = stream

        def iter_packets(self) -> Iterator[bytes]:
            entered.set()
            while not self.stream.closed:
                time.sleep(0.005)
            return
            yield b""  # pragma: no cover

    monkeypatch.setattr(ffmpeg_module.discord.oggparse, "OggStream", BlockingOggStream)
    monkeypatch.setattr(ffmpeg_module, "_PRIME_TIMEOUT_SECONDS", 0.02)
    process = _Process((), returncode=None)
    process.stdout = BlockingStream(())
    factory, _, _ = _factory(ffmpeg_module, tmp_path, lambda _argv, **_kwargs: process)

    with pytest.raises(PlaybackStartError):
        await factory.create(ResolvedSource("https://media.example/stream", {}))

    assert entered.is_set()
    assert process.stdout.closed
    assert process.terminate_calls == 1
    assert factory.last_failure == "prime_timeout (exit=unknown)"


@pytest.mark.asyncio
async def test_cancellation_during_priming_cleans_process(
    ffmpeg_module: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entered = threading.Event()

    class BlockingOggStream:
        def __init__(self, stream: _PacketStream) -> None:
            self.stream = stream

        def iter_packets(self) -> Iterator[bytes]:
            entered.set()
            while not self.stream.closed:
                time.sleep(0.005)
            return
            yield b""  # pragma: no cover

    monkeypatch.setattr(ffmpeg_module.discord.oggparse, "OggStream", BlockingOggStream)
    process = _Process(())
    factory, _, _ = _factory(ffmpeg_module, tmp_path, lambda _argv, **_kwargs: process)
    task = asyncio.create_task(
        factory.create(ResolvedSource("https://media.example/stream", {}))
    )
    assert await asyncio.to_thread(entered.wait, 1)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert process.stdout.closed


def test_cleanup_is_idempotent_when_called_concurrently(ffmpeg_module: Any) -> None:
    process = _Process(returncode=None, terminate_exits=False)
    source = ffmpeg_module._FFmpegAudioSource(process, copied=False)
    threads = [threading.Thread(target=source.cleanup) for _ in range(8)]

    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(1)

    assert all(not thread.is_alive() for thread in threads)
    assert process.terminate_calls == 1
    assert process.kill_calls == 1
    assert process.stdout.closed


@pytest.mark.skipif(
    os.environ.get("CI_NO_FFMPEG_SMOKE") == "1", reason="FFmpeg smoke disabled"
)
def test_packaged_ffmpeg_reads_generated_ogg_packets_in_isolated_process() -> None:
    repo = Path(__file__).resolve().parents[2]
    script = r"""
import subprocess
import asyncio
import imageio_ffmpeg
from TidalPlayerExp.playback.ffmpeg import FFmpegSourceFactory, _FFmpegAudioSource

exe = imageio_ffmpeg.get_ffmpeg_exe()
capability = asyncio.run(FFmpegSourceFactory().check())
assert capability.libopus and capability.passthrough
process = subprocess.Popen(
    [exe, "-hide_banner", "-loglevel", "error", "-nostdin", "-f", "lavfi", "-i",
     "sine=frequency=440:sample_rate=48000:duration=0.08", "-c:a", "libopus", "-ar", "48000",
     "-ac", "2", "-frame_duration", "20", "-f", "opus", "pipe:1"],
    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, shell=False,
)
source = _FFmpegAudioSource(process, copied=False)
source.prime()
packets = []
while True:
    packet = source.read()
    if not packet:
        break
    packets.append(packet)
source.cleanup()
assert packets and all(isinstance(packet, bytes) for packet in packets)
assert process.poll() is not None
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    if (
        completed.returncode != 0
        and "No module named 'imageio_ffmpeg'" in completed.stderr
    ):
        pytest.skip("packaged imageio-ffmpeg is unavailable")
    assert completed.returncode == 0, completed.stderr


@pytest.mark.asyncio
@pytest.mark.parametrize(("stderr", "returncode", "reason"), [
    (b"[https] HTTP error 403 Forbidden https://private.example/?token=secret", 1, "http_403"),
    (b"[tls] Error in the pull function. Authorization: Bearer secret", 1, "tls_error"),
    (b"Failed to resolve hostname private.example: secret", 1, "dns_error"),
    (b"Protocol 'https' not on whitelist 'secret'!", 1, "protocol_blocked"),
    (b"https://private.example: Protocol not found", 1, "protocol_unavailable"),
    (b"Error opening input: Invalid data found when processing input secret", 1, "invalid_media"),
    (b"Unrecognized option 'secret'", 1, "unsupported_option"),
    (b"Unknown encoder 'secret'", 1, "codec_unavailable"),
    (b"Connection timed out secret", 1, "network_timeout"),
    (b"private provider text and secret", 1, "no_audio"),
    (b"", -11, "process_signal_11"),
], ids=["403", "tls", "dns", "whitelist", "protocol", "media", "option", "codec", "timeout", "unknown", "crash"])
async def test_failed_start_reports_safe_ffmpeg_cause_without_provider_output(
    ffmpeg_module, tmp_path, caplog, stderr, returncode, reason,
):
    process = _Process((), returncode=returncode)
    process.stderr = io.BytesIO(stderr)
    factory, _, _ = _factory(ffmpeg_module, tmp_path, lambda *_args, **_kwargs: process)
    source = ResolvedSource("https://private.example/?token=secret", {})
    with pytest.raises(PlaybackStartError) as caught:
        await factory.create(source)
    report = factory.last_failure
    assert reason in report and f"exit={returncode}" in report
    assert reason in caplog.text
    for exposed in (report, caplog.text, _error_surface(caught.value)):
        assert "secret" not in exposed and "private.example" not in exposed
    assert process.stdout.closed and process.stderr.closed
    await factory.close()


@pytest.mark.asyncio
async def test_ffmpeg_stderr_is_drained_but_never_retained_as_unbounded_text(ffmpeg_module, tmp_path):
    class NoisyStream:
        closed = False
        total_read = 0

        def read(self, size):
            assert 0 < size <= 4096
            if self.total_read >= 2 * 1024 * 1024:
                return b""
            self.total_read += size
            return b"x" * size

        def close(self):
            self.closed = True

    process = _Process((), returncode=1)
    process.stderr = NoisyStream()
    factory, _, _ = _factory(ffmpeg_module, tmp_path, lambda *_args, **_kwargs: process)
    with pytest.raises(PlaybackStartError):
        await factory.create(ResolvedSource("https://media.example/stream", {}))
    assert process.stderr.total_read == 2 * 1024 * 1024
    assert process.stderr.closed
    assert len(factory.last_failure) < 100
    await factory.close()


def test_real_ffmpeg_failure_drains_stderr_and_reaps_reader_without_exposing_url():
    script = r"""
import asyncio
import socket
import threading
from TidalPlayerExp.playback import ffmpeg
from TidalPlayerExp.playback.models import ResolvedSource
from TidalPlayerExp.playback.errors import PlaybackStartError

async def main():
    children = []
    def spawn(*args, **kwargs):
        child = ffmpeg._default_spawn(*args, **kwargs)
        children.append(child)
        return child
    factory = ffmpeg.FFmpegSourceFactory(spawn=spawn)
    # An owned, bound but non-listening socket rejects connections locally.
    with socket.socket() as blocked:
        blocked.bind(('127.0.0.1', 0))
        source = ResolvedSource(f'https://127.0.0.1:{blocked.getsockname()[1]}/private-token', {})
        try:
            await factory.create(source)
        except PlaybackStartError:
            pass
        else:
            raise AssertionError('unreachable source started')
    await factory.close()
    assert factory.last_failure.startswith('connection_failed '), factory.last_failure
    assert children and all(child.poll() is not None for child in children)
    assert all(child.stdout.closed and child.stderr.closed for child in children)
    assert not any(thread.name == 'tidal_ffmpeg_stderr' for thread in threading.enumerate())

asyncio.run(main())
"""
    result = subprocess.run(
        [sys.executable, "-c", script], cwd=Path(__file__).resolve().parents[2],
        capture_output=True, text=True, timeout=30, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "private-token" not in result.stdout + result.stderr


def test_stderr_larger_than_pipe_capacity_does_not_deadlock_start():
    script = r"""
import asyncio
import subprocess
import sys
import threading
from TidalPlayerExp.playback.ffmpeg import FFmpegSourceFactory
from TidalPlayerExp.playback.models import ResolvedSource
from TidalPlayerExp.playback.errors import PlaybackStartError

async def main():
    children = []
    def spawn(_argv, **kwargs):
        child = subprocess.Popen(
            [sys.executable, "-c", "import os; os.write(2, b'HTTP error 403 Forbidden\\n' + b'x' * 2097152); raise SystemExit(1)"],
            **kwargs,
        )
        children.append(child)
        return child
    factory = FFmpegSourceFactory(spawn=spawn)
    try:
        try:
            await asyncio.wait_for(factory.create(ResolvedSource("https://media.example/test", {})), 10)
        except PlaybackStartError:
            pass
        else:
            raise AssertionError('empty stream started')
        assert factory.last_failure == "http_403 (exit=1)"
        assert all(child.poll() == 1 and child.stderr.closed for child in children)
        assert not any(thread.name == 'tidal_ffmpeg_stderr' for thread in threading.enumerate())
    finally:
        await factory.close()

asyncio.run(main())
"""
    result = subprocess.run(
        [sys.executable, "-c", script], cwd=Path(__file__).resolve().parents[2],
        capture_output=True, text=True, timeout=30, check=False,
    )
    assert result.returncode == 0, result.stderr
