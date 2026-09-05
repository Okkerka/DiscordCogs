"""Bounded, redacted FFmpeg audio sources for Discord voice playback."""

from __future__ import annotations

import asyncio
import importlib.resources
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Protocol, cast

import discord
from discord import oggparse

from .errors import PlaybackStartError, PlaybackUnavailable
from .models import ResolvedSource

_PROBE_TIMEOUT_SECONDS = 10.0
_CAPABILITY_CLOSE_TIMEOUT_SECONDS = 31.0
_PRIME_TIMEOUT_SECONDS = 20.0
_PROBE_OUTPUT_LIMIT = 256 * 1024
_PROCESS_WAIT_SECONDS = 0.5
_EOF_WAIT_SECONDS = 0.1
_VERSION_PATTERN = re.compile(r"\Affmpeg version ([^\s]+)")
_ALLOWED_HEADERS = (
    ("user-agent", "User-Agent"),
    ("referer", "Referer"),
    ("origin", "Origin"),
)


class _ChildProcess(Protocol):
    """The subprocess operations owned by an audio source."""

    stdout: BinaryIO | None
    returncode: int | None

    def poll(self) -> int | None: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...

    def wait(self, timeout: float | None = None) -> int: ...


class AudioSourceFactory(Protocol):
    """Backend-neutral constructor used by playback sessions."""

    async def create(self, source: ResolvedSource) -> discord.AudioSource: ...

    async def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class FFmpegCapability:
    """Resolved FFmpeg executable and playback features."""

    executable: str
    version: str
    libopus: bool
    passthrough: bool


class _InvalidPacketDuration(Exception):
    """Internal signal that copy mode cannot preserve Discord pacing."""


def _default_locator() -> str:
    """Discover imageio-ffmpeg candidates without executing any of them."""

    configured = os.getenv("IMAGEIO_FFMPEG_EXE")
    if configured:
        return configured

    from imageio_ffmpeg._definitions import (  # type: ignore[import-untyped]
        FNAME_PER_PLATFORM,
        get_platform,
    )

    platform = get_platform()
    packaged_name = FNAME_PER_PLATFORM.get(platform)
    if packaged_name:
        packaged = importlib.resources.files("imageio_ffmpeg.binaries").joinpath(
            packaged_name
        )
        if packaged.is_file() and isinstance(packaged, os.PathLike):
            return os.fspath(packaged)

    if platform.startswith("win"):
        conda = Path(sys.prefix, "Library", "bin", "ffmpeg.exe")
    else:
        conda = Path(sys.prefix, "bin", "ffmpeg")
    if conda.is_file():
        return str(conda)
    return "ffmpeg"


def _resolve_executable(located: str) -> Path | None:
    """Resolve an official locator result as either a path or PATH command."""

    candidate = Path(located)
    if not candidate.is_absolute() and candidate.parent == Path():
        found = shutil.which(located)
        if found is None:
            return None
        candidate = Path(found)
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    if not resolved.is_file() or not os.access(resolved, os.X_OK):
        return None
    return resolved


def _default_probe(argv: Sequence[str], timeout: float) -> bytes:
    """Run a quiet probe without retaining unbounded child output."""

    with tempfile.TemporaryFile() as output:
        subprocess.run(
            list(argv),
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=subprocess.DEVNULL,
            shell=False,
            timeout=timeout,
            check=False,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        output.seek(0)
        return output.read(_PROBE_OUTPUT_LIMIT)


def _default_spawn(
    argv: Sequence[str],
    *,
    stdin: int,
    stdout: int,
    stderr: int,
    shell: bool,
    creationflags: int,
) -> _ChildProcess:
    """Spawn one owned FFmpeg process without using discord.py's logging wrapper."""

    process = subprocess.Popen(
        list(argv),
        stdin=stdin,
        stdout=stdout,
        stderr=stderr,
        shell=shell,
        creationflags=creationflags,
    )
    return cast(_ChildProcess, process)


def _has_control(value: str) -> bool:
    return any(unicodedata.category(character) == "Cc" for character in value)


def _feature_present(output: str, feature: str) -> bool:
    for line in output.splitlines():
        fields = line.split()
        if len(fields) >= 2 and fields[1] == feature:
            return True
    return False


def _opus_packet_is_20ms(packet: bytes) -> bool:
    """Return whether an RFC 6716 Opus packet contains exactly 960 samples."""

    if not packet:
        return False
    configuration = packet[0] >> 3
    if configuration >= 16:
        samples_per_frame = 120 << (configuration & 0x03)
    elif configuration >= 12:
        samples_per_frame = 480 << (configuration & 0x01)
    elif configuration & 0x03 == 0x03:
        samples_per_frame = 2_880
    else:
        samples_per_frame = 480 << (configuration & 0x03)

    frame_code = packet[0] & 0x03
    if frame_code == 0:
        frame_count = 1
    elif frame_code in (1, 2):
        frame_count = 2
    elif len(packet) >= 2:
        frame_count = packet[1] & 0x3F
    else:
        return False
    return frame_count > 0 and samples_per_frame * frame_count == 960


def _quiet_process_call(operation: Callable[[], object]) -> bool:
    """Run one cleanup action without retaining or exposing child details."""

    try:
        operation()
    except Exception:  # noqa: BLE001 - subprocess cleanup must remain quiet and idempotent
        return False
    return True


def _cleanup_process(process: _ChildProcess) -> None:
    """Terminate, reap, and detach one process using fixed bounded waits."""

    stdout = process.stdout
    try:
        running = process.poll() is None
    except Exception:  # noqa: BLE001 - third-party process implementations are a trust boundary
        running = True
    if running:
        _quiet_process_call(process.terminate)
        reaped = _quiet_process_call(
            lambda: process.wait(timeout=_PROCESS_WAIT_SECONDS)
        )
        if not reaped:
            _quiet_process_call(process.kill)
            _quiet_process_call(lambda: process.wait(timeout=_PROCESS_WAIT_SECONDS))
    else:
        _quiet_process_call(lambda: process.wait(timeout=_PROCESS_WAIT_SECONDS))
    if stdout is not None:
        _quiet_process_call(stdout.close)


class _FFmpegAudioSource(discord.AudioSource):
    """An Opus packet source that owns and redacts its FFmpeg child."""

    def __init__(self, process: _ChildProcess, *, copied: bool) -> None:
        self._process = process
        self._copied = copied
        self._buffered: bytes | None = None
        self._closed = False
        self._state_lock = threading.Lock()
        self._read_lock = threading.Lock()
        self._current_error: PlaybackStartError | None = None
        stdout = process.stdout
        if stdout is None:
            raise PlaybackStartError()
        self._packets = iter(oggparse.OggStream(stdout).iter_packets())

    def __repr__(self) -> str:
        return "FFmpegAudioSource(<redacted>)"

    def is_opus(self) -> bool:
        return True

    def _mark_failed(self) -> PlaybackStartError:
        error = PlaybackStartError()
        with self._state_lock:
            self._current_error = error
        self.cleanup()
        return error

    def _next_audio_packet(self) -> bytes:
        failed = False
        ended = False
        packet = b""
        while True:
            try:
                packet = next(self._packets)
            except StopIteration:
                ended = True
            except Exception:  # noqa: BLE001 - parser errors can contain untrusted stream bytes
                failed = True
            if failed or ended:
                break
            if packet.startswith((b"OpusHead", b"OpusTags")):
                continue
            break

        if failed:
            raise self._mark_failed()
        if ended:
            try:
                returncode = self._process.poll()
            except Exception:  # noqa: BLE001 - process errors are redacted at this boundary
                returncode = -1
            if returncode is None:
                try:
                    returncode = self._process.wait(timeout=_EOF_WAIT_SECONDS)
                except subprocess.TimeoutExpired:
                    returncode = -1
                except Exception:  # noqa: BLE001 - process errors are redacted at this boundary
                    returncode = -1
            if returncode not in (None, 0):
                raise self._mark_failed()
            return b""
        if self._copied and not _opus_packet_is_20ms(packet):
            raise _InvalidPacketDuration
        return packet

    def prime(self) -> None:
        """Read and retain the first real audio packet before playback starts."""

        with self._read_lock:
            packet = self._next_audio_packet()
            if not packet:
                raise self._mark_failed()
            self._buffered = packet

    def read(self) -> bytes:
        duration_failed = False
        with self._read_lock:
            with self._state_lock:
                if self._closed:
                    return b""
            if self._buffered is not None:
                packet = self._buffered
                self._buffered = None
                return packet
            try:
                return self._next_audio_packet()
            except _InvalidPacketDuration:
                duration_failed = True
        if duration_failed:
            raise self._mark_failed()
        return b""  # pragma: no cover - duration_failed is always true on this path

    def cleanup(self) -> None:
        """Close exactly once without waiting for a potentially blocked reader."""

        with self._state_lock:
            if self._closed:
                return
            self._closed = True
        _cleanup_process(self._process)


class FFmpegSourceFactory:
    """Validate one FFmpeg binary and construct bounded native audio sources."""

    def __init__(
        self,
        *,
        locator: Callable[[], str] | None = None,
        probe: Callable[[Sequence[str], float], bytes] | None = None,
        spawn: Callable[..., _ChildProcess] | None = None,
    ) -> None:
        self._locator = locator or _default_locator
        self._probe = probe or _default_probe
        self._spawn = spawn or _default_spawn
        self._capability_task: asyncio.Task[FFmpegCapability] | None = None
        self._cleanup_tasks: set[asyncio.Task[None]] = set()
        self._closed = False

    def _check_sync(self) -> FFmpegCapability | None:
        try:
            located = self._locator()
            if not isinstance(located, str) or _has_control(located):
                return None
            executable_path = _resolve_executable(located)
            if executable_path is None:
                return None
            executable = str(executable_path)
            prefix = (executable, "-hide_banner", "-loglevel", "quiet")
            version_output = self._probe((*prefix, "-version"), _PROBE_TIMEOUT_SECONDS)
            encoders_output = self._probe(
                (*prefix, "-encoders"), _PROBE_TIMEOUT_SECONDS
            )
            muxers_output = self._probe((*prefix, "-muxers"), _PROBE_TIMEOUT_SECONDS)
            version_text = version_output[:_PROBE_OUTPUT_LIMIT].decode(
                "utf-8", "replace"
            )
            encoders_text = encoders_output[:_PROBE_OUTPUT_LIMIT].decode(
                "utf-8", "replace"
            )
            muxers_text = muxers_output[:_PROBE_OUTPUT_LIMIT].decode("utf-8", "replace")
            match = _VERSION_PATTERN.match(version_text)
            if match is None:
                return None
            return FFmpegCapability(
                executable=executable,
                version=match.group(1),
                libopus=_feature_present(encoders_text, "libopus"),
                passthrough=_feature_present(muxers_text, "opus"),
            )
        except Exception:  # noqa: BLE001 - locator/probe failures may contain executable details
            return None

    async def _perform_check(self) -> FFmpegCapability:
        capability = await asyncio.to_thread(self._check_sync)
        if capability is None:
            raise PlaybackUnavailable()
        return capability

    async def check(self) -> FFmpegCapability:
        """Return the factory's single cached and cancellation-safe capability check."""

        if self._closed:
            raise PlaybackUnavailable()
        if self._capability_task is None:
            self._capability_task = asyncio.create_task(self._perform_check())
        return await asyncio.shield(self._capability_task)

    async def close(self) -> None:
        """Reject new work and account for the factory's bounded background work."""

        self._closed = True
        capability_task = self._capability_task
        if capability_task is not None:
            try:
                with suppress(PlaybackUnavailable):
                    await asyncio.wait_for(
                        asyncio.shield(capability_task),
                        timeout=_CAPABILITY_CLOSE_TIMEOUT_SECONDS,
                    )
            except TimeoutError:
                capability_task.cancel()
                await asyncio.gather(capability_task, return_exceptions=True)

        cleanup_tasks = tuple(self._cleanup_tasks)
        if cleanup_tasks:
            await asyncio.gather(*cleanup_tasks, return_exceptions=True)

    @staticmethod
    def _safe_headers(headers: Mapping[str, str]) -> str | None:
        normalized: dict[str, str] = {}
        for name, value in headers.items():
            lowered = name.lower()
            if lowered not in {allowed[0] for allowed in _ALLOWED_HEADERS}:
                continue
            if lowered in normalized or _has_control(name) or _has_control(value):
                raise PlaybackStartError()
            normalized[lowered] = value
        lines = [
            f"{canonical}: {normalized[lowered]}"
            for lowered, canonical in _ALLOWED_HEADERS
            if lowered in normalized
        ]
        if not lines:
            return None
        return "\r\n".join(lines) + "\r\n"

    @staticmethod
    def _argv(
        capability: FFmpegCapability, source: ResolvedSource, *, copied: bool
    ) -> list[str]:
        if _has_control(source.url):
            raise PlaybackStartError()
        headers = FFmpegSourceFactory._safe_headers(source.headers)
        argv = [
            capability.executable,
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
        ]
        if headers is not None:
            argv.extend(("-headers", headers))
        argv.extend(
            (
                "-i",
                source.url,
                "-map",
                "0:a:0",
                "-vn",
                "-sn",
                "-dn",
                "-map_metadata",
                "-1",
            )
        )
        if copied:
            argv.extend(("-c:a", "copy"))
        else:
            argv.extend(
                (
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
                )
            )
        argv.extend(("-f", "opus", "pipe:1"))
        return argv

    def _construct_sync(
        self, capability: FFmpegCapability, source: ResolvedSource, *, copied: bool
    ) -> _FFmpegAudioSource | None:
        process: _ChildProcess | None = None
        try:
            process = self._spawn(
                self._argv(capability, source, copied=copied),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                shell=False,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            return _FFmpegAudioSource(process, copied=copied)
        except Exception:  # noqa: BLE001 - argv, URL, and headers must not survive this boundary
            if process is not None:
                _cleanup_process(process)
            return None

    def _retain_eventual_cleanup(
        self, construction: asyncio.Task[_FFmpegAudioSource | None]
    ) -> None:
        async def cleanup_when_ready() -> None:
            try:
                source = await construction
            except Exception:  # noqa: BLE001 - construction already sanitizes expected failures
                return
            if source is not None:
                await asyncio.to_thread(source.cleanup)

        cleanup_task = asyncio.create_task(cleanup_when_ready())
        self._retain_background_task(cleanup_task)

    def _retain_background_task(self, task: asyncio.Task[None]) -> None:
        """Keep and consume a bounded task whose caller was cancelled."""

        def task_finished(finished: asyncio.Task[None]) -> None:
            self._cleanup_tasks.discard(finished)
            if not finished.cancelled():
                finished.exception()

        self._cleanup_tasks.add(task)
        task.add_done_callback(task_finished)

    async def _construct(
        self, capability: FFmpegCapability, source: ResolvedSource, *, copied: bool
    ) -> _FFmpegAudioSource:
        construction = asyncio.create_task(
            asyncio.to_thread(self._construct_sync, capability, source, copied=copied)
        )
        try:
            result = await asyncio.shield(construction)
        except asyncio.CancelledError:
            self._retain_eventual_cleanup(construction)
            raise
        if result is None:
            raise PlaybackStartError()
        return result

    @staticmethod
    async def _cleanup(source: _FFmpegAudioSource) -> None:
        await asyncio.to_thread(source.cleanup)

    async def _prime(self, source: _FFmpegAudioSource) -> None:
        priming = asyncio.create_task(asyncio.to_thread(source.prime))
        timed_out = False
        invalid_duration = False
        failed = False
        try:
            await asyncio.wait_for(
                asyncio.shield(priming), timeout=_PRIME_TIMEOUT_SECONDS
            )
        except asyncio.CancelledError:
            await asyncio.shield(self._cleanup(source))
            self._retain_background_task(priming)
            raise
        except TimeoutError:
            await self._cleanup(source)
            self._retain_background_task(priming)
            timed_out = True
        except _InvalidPacketDuration:
            await self._cleanup(source)
            invalid_duration = True
        except Exception:  # noqa: BLE001 - unexpected worker failures are sanitized below
            await self._cleanup(source)
            failed = True
        if invalid_duration:
            raise _InvalidPacketDuration
        if timed_out or failed:
            raise PlaybackStartError()

    async def create(self, source: ResolvedSource) -> discord.AudioSource:
        """Spawn and prime one safe Opus source without blocking the event loop."""

        capability = await self.check()
        if self._closed:
            raise PlaybackUnavailable()
        copied = (
            isinstance(source.codec, str)
            and source.codec.lower() == "opus"
            and source.sample_rate == 48_000
            and source.channels == 2
        )
        if not capability.passthrough or (not copied and not capability.libopus):
            raise PlaybackUnavailable()

        audio = await self._construct(capability, source, copied=copied)

        invalid_duration = False
        failed = False
        try:
            await self._prime(audio)
        except asyncio.CancelledError:
            raise
        except _InvalidPacketDuration:
            invalid_duration = True
        except Exception:  # noqa: BLE001 - only a fixed public error leaves this boundary
            failed = True
        if failed:
            raise PlaybackStartError()
        if invalid_duration:
            if not capability.libopus:
                raise PlaybackUnavailable()
            retry = await self._construct(capability, source, copied=False)
            retry_failed = False
            try:
                await self._prime(retry)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - retry errors are sanitized below
                retry_failed = True
            if retry_failed:
                raise PlaybackStartError()
            return retry
        return audio


__all__ = ("AudioSourceFactory", "FFmpegCapability", "FFmpegSourceFactory")
