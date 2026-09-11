"""Bounded, redacted FFmpeg audio sources for Discord voice playback."""

from __future__ import annotations

import asyncio
import importlib.resources
import logging
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
_METADATA_WAIT_SECONDS = 0.25
_VERSION_PATTERN = re.compile(r"\Affmpeg version ([^\s]+)")
_INPUT_DURATION_PATTERN = re.compile(
    rb"(?:^|\n)  Duration: (N/A|[0-9]{2,6}:[0-5][0-9]:[0-5][0-9](?:\.[0-9]{1,6})?), start:"
)
log = logging.getLogger("red.tidalplayerexp.ffmpeg")
_ALLOWED_HEADERS = (
    ("user-agent", "User-Agent"),
    ("referer", "Referer"),
    ("origin", "Origin"),
)
# Uploaded bytes are untrusted even on Discord's CDN. Do not let an XML/text
# playlist disguised as MP4 trigger arbitrary nested network requests.
_UPLOAD_DEMUXERS = "aac,aiff,asf,avi,flac,matroska,webm,mov,mp4,m4a,3gp,3g2,mj2,mp3,mpeg,mpegts,ogg,wav"


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


def _stderr_reason(output: bytes) -> str | None:
    """Reduce untrusted tool output to a fixed category, never a quoted line."""
    lowered = output.lower()
    status = re.search(rb"\b(?:http error|server returned) ([45][0-9]{2})\b", lowered)
    if status:
        return "http_" + status[1].decode("ascii")
    if b"[tcp @" in lowered and b"connection to" in lowered and b"failed" in lowered:
        return "connection_failed"
    patterns = (
        (b"not on whitelist", "protocol_blocked"),
        (b"protocol not found", "protocol_unavailable"),
        (b"failed to resolve hostname", "dns_error"),
        (b"name or service not known", "dns_error"),
        (b"error in the pull function", "tls_error"),
        (b"certificate verify failed", "tls_error"),
        (b"tls handshake", "tls_error"),
        (b"connection timed out", "network_timeout"),
        (b"connection refused", "connection_failed"),
        (b"connection reset", "connection_failed"),
        (b"invalid data found", "invalid_media"),
        (b"unrecognized option", "unsupported_option"),
        (b"option not found", "unsupported_option"),
        (b"unknown encoder", "codec_unavailable"),
        (b"decoder not found", "codec_unavailable"),
    )
    return next((reason for pattern, reason in patterns if pattern in lowered), None)


class _StderrReader:
    """Continuously drain a pipe; retain only a safe category and bounded chunks."""

    def __init__(self, stream: BinaryIO | None, *, inspect_duration: bool = False) -> None:
        self.reason: str | None = None
        self.duration: int | None = None
        self._metadata_ready = threading.Event()
        if not inspect_duration or stream is None:
            self._metadata_ready.set()
        self._thread: threading.Thread | None = None
        if stream is not None:
            self._thread = threading.Thread(
                target=self._drain, args=(stream,), name="tidal_ffmpeg_stderr", daemon=True,
            )
            self._thread.start()

    def _drain(self, stream: BinaryIO) -> None:
        tail = b""
        try:
            # Buffered read(n) can wait to fill n bytes even though FFmpeg has
            # already printed its short input header. read1 drains it promptly.
            read_chunk = getattr(stream, "read1", stream.read)
            while chunk := read_chunk(4096):
                if self.reason is None or not self._metadata_ready.is_set():
                    combined = tail + chunk
                    if self.reason is None:
                        self.reason = _stderr_reason(combined)
                    if not self._metadata_ready.is_set():
                        match = _INPUT_DURATION_PATTERN.search(combined)
                        if match is not None:
                            if match[1] != b"N/A":
                                hours, minutes, seconds = match[1].split(b":")
                                total = int(hours) * 3600 + int(minutes) * 60 + float(seconds)
                                if total > 0:
                                    self.duration = max(1, int(total))
                            self._metadata_ready.set()
                        elif b"\nStream mapping:" in combined or b"\nOutput #0" in combined:
                            self._metadata_ready.set()
                    tail = combined[-256:]
        except Exception:  # noqa: BLE001 - never let pipe errors print raw provider output
            # Cleanup can close the pipe while its final bytes are being read.
            return
        finally:
            self._metadata_ready.set()
            _quiet_process_call(stream.close)

    def wait_metadata(self) -> None:
        """Briefly synchronize header parsing; absent duration never blocks audio."""
        self._metadata_ready.wait(timeout=_METADATA_WAIT_SECONDS)

    def finish(self) -> None:
        """Allow the reaped process's final diagnostics to drain without hanging."""
        if self._thread is not None:
            self._thread.join(timeout=_PROCESS_WAIT_SECONDS)


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

    def __init__(self, process: _ChildProcess, *, copied: bool, inspect_duration: bool = False) -> None:
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
        self._stderr = _StderrReader(getattr(process, "stderr", None), inspect_duration=inspect_duration)
        self._failure_exit: int | None = None
        self._packets = iter(oggparse.OggStream(stdout).iter_packets())

    def __repr__(self) -> str:
        return "FFmpegAudioSource(<redacted>)"

    def is_opus(self) -> bool:
        return True

    @property
    def duration(self) -> int | None:
        """Input duration discovered during priming, never elapsed output time."""
        return self._stderr.duration

    def _mark_failed(self) -> PlaybackStartError:
        error = PlaybackStartError()
        with self._state_lock:
            # Cleanup sets _closed before sending SIGTERM/SIGKILL. A worker
            # observing that induced EOF must not report it as an external crash.
            if not self._closed:
                try:
                    self._failure_exit = self._process.poll()
                except Exception:  # noqa: BLE001 - only a bounded exit code is diagnostic input
                    self._failure_exit = None
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
        self._stderr.wait_metadata()

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
        self._stderr.finish()

    def failure_summary(self, default: str) -> str:
        """Return a safe failure category, excluding provider text and arguments."""
        code = self._failure_exit
        if not isinstance(code, int) or not -2147483648 <= code <= 4294967295:
            code = None
        reason = self._stderr.reason or default
        if code is not None and -127 <= code < 0:
            reason = f"process_signal_{-code}"
        return f"{reason} (exit={code if code is not None else 'unknown'})"


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
        self._last_failure: str | None = None

    @property
    def last_failure(self) -> str | None:
        """Most recent safe startup failure, for the owner-only doctor command."""
        return self._last_failure

    def _record_failure(self, summary: str) -> None:
        self._last_failure = summary
        log.warning("FFmpeg startup failed: %s", summary)

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
            "info" if source.media_only else "error",
            "-nostats",
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
        if source.media_only:
            argv.extend(("-format_whitelist", _UPLOAD_DEMUXERS))
        if source.start_time:
            argv.extend(("-ss", f"{source.start_time:.3f}"))
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
            if source.volume != 100:
                # The limiter prevents amplification above full scale; disable
                # its automatic makeup gain so quiet settings stay quiet.
                gain = f"volume={source.volume / 100:.2f}"
                if source.volume > 100:
                    gain += ",alimiter=limit=1:level=false:latency=true"
                argv.extend(("-af", gain))
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
                stderr=subprocess.PIPE,
                shell=False,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            return _FFmpegAudioSource(process, copied=copied, inspect_duration=source.media_only)
        except Exception:  # noqa: BLE001 - argv, URL, and headers must not survive this boundary
            if process is not None:
                _cleanup_process(process)
                stderr = getattr(process, "stderr", None)
                if stderr is not None:
                    _quiet_process_call(stderr.close)
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
            self._record_failure("spawn_failed (exit=unknown)")
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
            self._record_failure(source.failure_summary("prime_timeout" if timed_out else "no_audio"))
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
            and source.volume == 100
            and source.start_time == 0
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
