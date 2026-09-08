"""Bounded, child-process YouTube extraction with no in-process yt-dlp import."""

from __future__ import annotations

import ast
import asyncio
import contextlib
import importlib.metadata
import importlib.util
import json
import logging
import math
import os
import re
import signal
import sys
import unicodedata
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from packaging.version import Version

from ..playback import ResolvedSource, SourceKind, SourceReference
from ..playback.errors import PlaybackUnavailable, SourceResolutionError

_VIDEO_ID_LENGTH = 11
_MAX_TITLE = 200
_MAX_CHANNEL = 100
_MAX_THUMBNAIL = 2048
_MAX_STDOUT = 16 * 1024
_MAX_PLAYLIST_LIMIT = 100
_VIDEO_DEADLINE = 30.0
_PLAYLIST_DEADLINE = 45.0
_GRACE_PERIOD = 0.25
_CLOSE_DEADLINE = 1.0
_READ_CHUNK = 4096
_ALLOWED_HEADERS = {"user-agent": "User-Agent", "referer": "Referer", "origin": "Origin"}
_MIN_YT_DLP_VERSION = Version("2026.8.19")
_API_DURATION = re.compile(r"P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?\Z", re.ASCII)
_YT_DLP_BOOTSTRAP = (
    "import runpy,sys; sys.path.insert(0,sys.argv[1]); "
    "sys.argv=['yt_dlp',*sys.argv[2:]]; runpy.run_module('yt_dlp',run_name='__main__')"
)

ProcessFactory = Callable[..., Awaitable[Any]]
log = logging.getLogger("red.tidalplayerexp.youtube")


class _SpawnedAfterClose(Exception):
    def __init__(self, child: Any) -> None:
        self.child = child


class _SpawnCancelled(asyncio.CancelledError):
    """The caller cancelled while the resolver still owns a factory task."""


def _safe_display(value: object, limit: int) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    text = " ".join(text.split()).strip()
    if not text:
        raise ValueError
    return text if len(text) <= limit else text[: max(0, limit - 3)].rstrip() + "..."


def _valid_https_url(value: object, *, limit: int | None = None) -> str:
    if (
        not isinstance(value, str)
        or not value
        or (limit is not None and len(value) > limit)
        or "\r" in value
        or "\n" in value
    ):
        raise ValueError
    try:
        parts = urlsplit(value)
        _ = parts.port
        valid = (
            parts.scheme.lower() == "https"
            and bool(parts.hostname)
            and parts.username is None
            and parts.password is None
            # Standard HTTPS URLs may omit an explicit port; ``parts.port``
            # access above still rejects malformed or out-of-range ports.
        )
    except (TypeError, ValueError):
        valid = False
    if not valid:
        raise ValueError
    return value


def _positive_int(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError
    return value


def _duration_seconds(value: object) -> int | None:
    """Normalize extractor seconds without treating live/unknown zero as failure."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        raise ValueError
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError
    return max(1, int(value)) if value else None


def parse_youtube_api_duration(value: object) -> int | None:
    """Read YouTube contentDetails.duration (ISO 8601 days/hours/minutes/seconds)."""
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > 64:
        raise ValueError
    match = _API_DURATION.fullmatch(value)
    if match is None or not any(match.groups()) or value.endswith("T"):
        raise ValueError
    seconds = sum(int(part or 0) * scale for part, scale in zip(match.groups(), (86400, 3600, 60, 1)))
    return seconds or None


@dataclass(frozen=True, slots=True)
class YouTubeVideoMetadata:
    """Safe display metadata; playback URLs and headers are intentionally absent."""

    video_id: str
    title: str
    channel: str | None
    duration: int | None
    thumbnail: str | None

    def __post_init__(self) -> None:
        if not isinstance(self.video_id, str) or len(self.video_id) != _VIDEO_ID_LENGTH:
            raise ValueError
        if any(char not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-" for char in self.video_id):
            raise ValueError
        object.__setattr__(self, "title", _safe_display(self.title, _MAX_TITLE))
        if self.channel is not None:
            object.__setattr__(self, "channel", _safe_display(self.channel, _MAX_CHANNEL))
        object.__setattr__(self, "duration", _duration_seconds(self.duration))
        if self.thumbnail is not None:
            object.__setattr__(self, "thumbnail", _valid_https_url(self.thumbnail, limit=_MAX_THUMBNAIL))


def _metadata_from_mapping(payload: object) -> YouTubeVideoMetadata:
    if not isinstance(payload, Mapping):
        raise TypeError
    try:
        video_id = payload.get("id")
        title = payload.get("title")
        channel = payload.get("uploader") or payload.get("channel")
        if not isinstance(video_id, str) or not isinstance(title, str):
            raise TypeError
        if channel is not None and not isinstance(channel, str):
            raise TypeError
        return YouTubeVideoMetadata(video_id, title, channel, payload.get("duration"), payload.get("thumbnail"))
    except Exception:  # noqa: BLE001 - provider output is untrusted
        raise ValueError from None


def _deno_path() -> str:
    try:
        import deno  # type: ignore[import-not-found,import-untyped]

        value = deno.find_deno_bin()
    except Exception:  # noqa: BLE001 - optional dependency can fail arbitrarily
        raise PlaybackUnavailable() from None
    if not isinstance(value, str) or not os.path.isabs(value) or not os.path.isfile(value):
        raise PlaybackUnavailable() from None
    if os.name != "nt" and not os.access(value, os.X_OK):
        raise PlaybackUnavailable() from None
    return value


def _validated_deno(value: object) -> str:
    if not isinstance(value, str) or not os.path.isabs(value) or not os.path.isfile(value):
        raise ValueError
    if os.name != "nt" and not os.access(value, os.X_OK):
        raise ValueError
    return value


def _yt_dlp_installation() -> tuple[str, str]:
    """Select the newest compatible installed code, including Downloader's lib.

    Red appends its target directory after global site-packages, so find_spec
    alone can select an obsolete global copy. Read the package's literal version
    without importing it: pip --target can leave stale dist-info directories.
    Only the isolated extractor child receives the selected path.
    """
    candidates: list[Path] = []
    try:
        candidates.extend(Path(str(dist.locate_file("yt_dlp"))) for dist in importlib.metadata.distributions(name="yt-dlp"))
    except Exception as error:  # noqa: BLE001 - allow normal discovery if optional metadata is broken
        log.debug("Extractor metadata discovery failed (%s); trying import path", type(error).__name__)
    try:
        spec = importlib.util.find_spec("yt_dlp")
        locations = spec.submodule_search_locations if spec is not None else None
    except Exception:  # noqa: BLE001 - optional dependency discovery is untrusted
        locations = None
    if locations:
        candidates.extend(Path(location) for location in locations)

    selected: tuple[Version, Path] | None = None
    for package in dict.fromkeys(candidates):
        try:
            if not package.is_absolute() or not (package / "__main__.py").is_file():
                continue
            with (package / "version.py").open("rb") as stream:
                source = stream.read(16385)
            if len(source) > 16384:
                continue
            versions = [
                node.value.value
                for node in ast.parse(source.decode("utf-8")).body
                if isinstance(node, ast.Assign)
                and any(isinstance(target, ast.Name) and target.id == "__version__" for target in node.targets)
                and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)
            ]
            if len(versions) != 1:
                continue
            version = Version(versions[0])
            if version >= _MIN_YT_DLP_VERSION and (selected is None or version > selected[0]):
                selected = version, package.parent
        except (OSError, ValueError, SyntaxError, UnicodeError):
            continue
    if selected is None:
        raise PlaybackUnavailable()
    return str(selected[1]), str(selected[0])


def _yt_dlp_root() -> str:
    return _yt_dlp_installation()[0]


def _validated_dependency_root(value: object) -> str:
    if not isinstance(value, str) or not os.path.isabs(value) or not os.path.isdir(value):
        raise ValueError
    package = Path(value) / "yt_dlp"
    if not package.is_dir() or not (package / "__main__.py").is_file():
        raise ValueError
    return value


def _canonical_video_url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"


def _canonical_playlist_url(playlist_id: str) -> str:
    return f"https://www.youtube.com/playlist?list={playlist_id}"


def _signal_process_group(pid: int, name: str) -> bool:
    killpg = getattr(os, "killpg", None)
    getpgid = getattr(os, "getpgid", None)
    value = getattr(signal, name, None)
    if callable(killpg) and callable(getpgid) and isinstance(value, int):
        try:
            killpg(getpgid(pid), value)
        except (OSError, ProcessLookupError):
            return False
        return True
    return False


class YouTubeResolver:
    """Resolve validated YouTube references through short-lived yt-dlp children."""

    def __init__(
        self,
        *,
        process_factory: ProcessFactory | None = None,
        deno_locator: Callable[[], str] | None = None,
        yt_dlp_locator: Callable[[], str] | None = None,
    ) -> None:
        self._process_factory = process_factory or asyncio.create_subprocess_exec
        self._deno_locator = deno_locator or _deno_path
        self._yt_dlp_locator = yt_dlp_locator or _yt_dlp_root
        self._slots = asyncio.Semaphore(2)
        self._children: set[Any] = set()
        self._reapers: dict[Any, asyncio.Task[None]] = {}
        self._cleanup_futures: dict[Any, asyncio.Future[bool]] = {}
        self._cleanup_tasks: set[asyncio.Task[Any]] = set()
        self._factory_tasks: set[asyncio.Task[Any]] = set()
        self._late_factory_tasks: set[asyncio.Task[Any]] = set()
        self._closed = False
        self._state_lock = asyncio.Lock()

    def _common_args(self, deno: str, dependency_root: str) -> list[str]:
        # netrc is opt-in in yt-dlp. Ignore configuration and never enable it;
        # there is no corresponding --no-netrc CLI option.
        return [
            sys.executable,
            "-I",
            "-c",
            _YT_DLP_BOOTSTRAP,
            dependency_root,
            "--ignore-config",
            "--no-plugin-dirs",
            "--no-js-runtimes",
            "--js-runtimes",
            f"deno:{deno}",
            "--no-cache-dir",
            "--no-update",
            "--no-download",
            # Optional projected fields must be JSON null, not yt-dlp's bare NA.
            "--output-na-placeholder",
            "null",
            "--quiet",
            "--no-warnings",
            "--no-progress",
            "--socket-timeout",
            "15",
            "--extractor-retries",
            "1",
            "--fragment-retries",
            "0",
            "--file-access-retries",
            "0",
        ]

    def _child_env(self) -> dict[str, str]:
        env = {key: value for key, value in os.environ.items() if not key.upper().startswith("YTDLP_")}
        env["YTDLP_NO_PLUGINS"] = "1"
        return env

    async def _spawn(self, args: list[str]) -> Any:
        async with self._state_lock:
            if self._closed:
                raise PlaybackUnavailable() from None
        kwargs: dict[str, Any] = {
            "stdin": asyncio.subprocess.DEVNULL,
            "stdout": asyncio.subprocess.PIPE,
            "stderr": asyncio.subprocess.DEVNULL,
            "env": self._child_env(),
            "shell": False,
        }
        if os.name == "nt":
            kwargs["creationflags"] = getattr(__import__("subprocess"), "CREATE_NEW_PROCESS_GROUP", 0) | getattr(
                __import__("subprocess"), "CREATE_NO_WINDOW", 0
            )
        else:
            kwargs["start_new_session"] = True
        async def _invoke_factory() -> Any:
            return await self._process_factory(*args, **kwargs)

        factory_task = asyncio.create_task(_invoke_factory())
        self._factory_tasks.add(factory_task)
        try:
            child = await asyncio.shield(factory_task)
        except asyncio.CancelledError:
            self._late_factory_tasks.add(factory_task)
            factory_task.add_done_callback(self._adopt_late_factory_child)
            raise _SpawnCancelled() from None
        except Exception:  # noqa: BLE001 - process factories expose platform errors
            raise PlaybackUnavailable() from None
        finally:
            if factory_task.done():
                self._factory_tasks.discard(factory_task)
        registration_task = asyncio.create_task(self._register_spawned_child(child))
        self._track_cleanup_task(registration_task)
        try:
            await asyncio.shield(registration_task)
        except asyncio.CancelledError:
            adoption_task = asyncio.create_task(self._adopt_after_registration(child, registration_task))
            self._track_cleanup_task(adoption_task)
            raise _SpawnCancelled() from None
        return child

    async def _register_spawned_child(self, child: Any) -> None:
        async with self._state_lock:
            if self._closed:
                self._children.add(child)
                raise _SpawnedAfterClose(child)
            self._children.add(child)

    async def _adopt_after_registration(self, child: Any, registration_task: asyncio.Task[Any]) -> None:
        try:
            await asyncio.shield(registration_task)
        except asyncio.CancelledError:
            registration_task.cancelled()
        except Exception:  # noqa: BLE001 - registration failure must still clean up the child
            registration_task.exception()
        # Registration already owns the child; close may have reaped it since.
        await self._cleanup_child(child, release_slot=True)

    def _adopt_late_factory_child(self, factory_task: asyncio.Task[Any]) -> None:
        self._factory_tasks.discard(factory_task)
        if factory_task not in self._late_factory_tasks:
            return
        self._late_factory_tasks.discard(factory_task)
        if factory_task.cancelled():
            self._slots.release()
            return
        try:
            child = factory_task.result()
        except Exception:  # noqa: BLE001 - late factory failures are sanitized
            self._slots.release()
            return
        cleanup_task = asyncio.create_task(self._adopt_and_cleanup_child(child))
        self._track_cleanup_task(cleanup_task)

    async def _adopt_and_cleanup_child(self, child: Any) -> None:
        async with self._state_lock:
            self._children.add(child)
        await self._cleanup_child(child, release_slot=True)

    def _track_cleanup_task(self, task: asyncio.Task[Any]) -> None:
        self._cleanup_tasks.add(task)
        task.add_done_callback(self._cleanup_tasks.discard)

    async def _read_stdout(self, child: Any, ceiling: int) -> bytes:
        stream = getattr(child, "stdout", None)
        if stream is None:
            raise ValueError
        output = bytearray()
        while True:
            chunk = await stream.read(min(_READ_CHUNK, ceiling + 1 - len(output)))
            if not chunk:
                break
            if not isinstance(chunk, bytes) or len(output) + len(chunk) > ceiling:
                raise ValueError
            output.extend(chunk)
        return bytes(output)

    async def _run_child(self, args: list[str], *, deadline: float, ceiling: int) -> bytes:
        await self._slots.acquire()
        child: Any | None = None
        slot_handed_off = False
        try:
            child = await self._spawn(args)
            running_child = child

            async def _read_and_wait() -> bytes:
                output = await self._read_stdout(running_child, ceiling)
                returncode = await running_child.wait()
                if returncode != 0:
                    raise RuntimeError
                return output

            return await asyncio.wait_for(_read_and_wait(), timeout=deadline)
        except asyncio.TimeoutError:
            raise PlaybackUnavailable() from None
        except _SpawnedAfterClose as error:
            child = error.child
            raise PlaybackUnavailable() from None
        except _SpawnCancelled:
            slot_handed_off = True
            raise
        except asyncio.CancelledError:
            raise
        except (PlaybackUnavailable, SourceResolutionError):
            raise
        except Exception:  # noqa: BLE001 - child/process output is untrusted
            raise SourceResolutionError() from None
        finally:
            if child is not None:
                reaped = await self._cleanup_child(child, release_slot=True)
                slot_handed_off = slot_handed_off or not reaped
            if not slot_handed_off and child is None:
                self._slots.release()

    async def _cleanup_child(self, child: Any, *, release_slot: bool) -> bool:
        async with self._state_lock:
            if child in self._reapers:
                return False
            if self._closed and child not in self._children:
                return False
            existing = self._cleanup_futures.get(child)
            if existing is None:
                existing = asyncio.get_running_loop().create_future()
                self._cleanup_futures[child] = existing
                owner = True
            else:
                owner = False
        if not owner:
            return await asyncio.shield(existing)
        reaped = False
        try:
            reaped = await self._terminate(child)
            if reaped:
                async with self._state_lock:
                    self._children.discard(child)
                if release_slot:
                    self._slots.release()
                return True
            self._register_reaper(child, release_slot=release_slot)
            return False
        finally:
            async with self._state_lock:
                current = self._cleanup_futures.pop(child, None)
            if current is not None and not current.done():
                current.set_result(reaped)

    def _register_reaper(self, child: Any, *, release_slot: bool) -> None:
        if child in self._reapers:
            return
        task = asyncio.create_task(self._reap_owned(child, release_slot=release_slot))
        self._reapers[child] = task
        self._track_cleanup_task(task)

    async def _reap_owned(self, child: Any, *, release_slot: bool) -> None:
        while not await self._wait_bounded(child):
            force = getattr(child, "kill", None)
            if callable(force):
                with contextlib.suppress(Exception):
                    force()
        async with self._state_lock:
            self._children.discard(child)
            self._reapers.pop(child, None)
        if release_slot:
            self._slots.release()

    async def _terminate(self, child: Any) -> bool:
        pid = getattr(child, "pid", None)
        try:
            if getattr(child, "returncode", None) is not None:
                return True
            if isinstance(pid, int) and pid > 0 and os.name != "nt":
                with contextlib.suppress(ProcessLookupError, OSError):
                    signaled = _signal_process_group(pid, "SIGTERM")
                    if not signaled:
                        terminate = getattr(child, "terminate", None)
                        if callable(terminate):
                            terminate()
            elif isinstance(pid, int) and pid > 0 and os.name == "nt":
                signal_value = getattr(signal, "CTRL_BREAK_EVENT", None)
                send_signal = getattr(child, "send_signal", None)
                if callable(send_signal) and isinstance(signal_value, int):
                    with contextlib.suppress(Exception):
                        send_signal(signal_value)
                else:
                    terminate = getattr(child, "terminate", None)
                    if callable(terminate):
                        with contextlib.suppress(Exception):
                            terminate()
            else:
                terminate = getattr(child, "terminate", None)
                if callable(terminate):
                    with contextlib.suppress(Exception):
                        terminate()
            if await self._wait_bounded(child):
                return True
            helper_reaped = True
            if isinstance(pid, int) and pid > 0 and os.name != "nt":
                with contextlib.suppress(ProcessLookupError, OSError):
                    _signal_process_group(pid, "SIGKILL")
            elif isinstance(pid, int) and pid > 0 and os.name == "nt":
                helper_reaped = await self._force_windows_tree(pid)
            kill = getattr(child, "kill", None)
            if callable(kill):
                with contextlib.suppress(Exception):
                    kill()
            child_reaped = await self._wait_bounded(child)
            return child_reaped and helper_reaped
        except asyncio.CancelledError:
            # Cleanup must finish even when the caller goes away.
            with contextlib.suppress(Exception):
                kill = getattr(child, "kill", None)
                if callable(kill):
                    kill()
                return await self._wait_bounded(child)
            return False

    async def _wait_bounded(self, child: Any) -> bool:
        try:
            await asyncio.wait_for(child.wait(), timeout=_GRACE_PERIOD)
        except (asyncio.TimeoutError, OSError, ProcessLookupError):
            return False
        except Exception:  # noqa: BLE001 - process wait failures are sanitized
            return False
        return True

    async def _force_windows_tree(self, pid: int) -> bool:
        # taskkill.exe is an absolute system tool and is used only for our tracked PID.
        system_root = os.environ.get("SystemRoot", r"C:\\Windows")
        taskkill = os.path.join(system_root, "System32", "taskkill.exe")
        if not os.path.isabs(taskkill) or not os.path.isfile(taskkill):
            return True
        helper: Any | None = None
        try:
            helper = await asyncio.create_subprocess_exec(
                taskkill,
                "/PID",
                str(pid),
                "/T",
                "/F",
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            helper_reaped = await self._wait_bounded(helper)
            if not helper_reaped:
                force = getattr(helper, "kill", None)
                if callable(force):
                    with contextlib.suppress(Exception):
                        force()
                helper_reaped = await self._wait_bounded(helper)
            if not helper_reaped:
                self._register_reaper(helper, release_slot=False)
            return helper_reaped
        except Exception:  # noqa: BLE001 - cleanup helper failures are sanitized
            return True
        finally:
            if helper is not None and getattr(helper, "returncode", None) is None:
                force = getattr(helper, "kill", None)
                if callable(force):
                    with contextlib.suppress(Exception):
                        force()

    @staticmethod
    def _parse_document(output: bytes) -> object:
        text = output.decode("utf-8")
        decoder = json.JSONDecoder()
        trimmed = text.lstrip()
        value, end = decoder.raw_decode(trimmed)
        if trimmed[end:].strip():
            raise ValueError
        return value

    async def resolve(self, reference: SourceReference) -> ResolvedSource:
        if not isinstance(reference, SourceReference) or reference.kind is not SourceKind.YOUTUBE:
            raise SourceResolutionError() from None
        try:
            deno = _validated_deno(self._deno_locator())
            dependency_root = self._yt_dlp_locator()
            dependency_root = _validated_dependency_root(dependency_root)
            args = self._common_args(deno, dependency_root) + [
                "--no-playlist",
                "--format",
                "bestaudio[acodec!=none]/bestaudio/best",
                "--print",
                '{"url":%(url)j,"http_headers":%(http_headers)j,"acodec":%(acodec)j,"vcodec":%(vcodec)j,"asr":%(asr)j,"audio_channels":%(audio_channels)j,"duration":%(duration)j}',
                _canonical_video_url(reference.identifier),
            ]
        except Exception:  # noqa: BLE001 - validation errors are deliberately sanitized
            raise PlaybackUnavailable() from None
        try:
            payload = self._parse_document(await self._run_child(args, deadline=_VIDEO_DEADLINE, ceiling=_MAX_STDOUT))
            return self._source_from_mapping(payload)
        except (PlaybackUnavailable, SourceResolutionError):
            raise
        except Exception:  # noqa: BLE001 - malformed output and provider failures are sanitized
            raise SourceResolutionError() from None

    @staticmethod
    def _source_from_mapping(payload: object) -> ResolvedSource:
        if not isinstance(payload, Mapping):
            raise TypeError
        try:
            url = _valid_https_url(payload.get("url"))
            acodec = payload.get("acodec")
            vcodec = payload.get("vcodec")
            if not isinstance(acodec, str) or vcodec != "none":
                raise ValueError
            codec = acodec.strip()
            if not codec or codec.casefold() == "none":
                raise ValueError
            raw_headers = payload.get("http_headers")
            if not isinstance(raw_headers, Mapping):
                raise TypeError
            headers: dict[str, str] = {}
            for key, value in dict(raw_headers).items():
                if not isinstance(key, str):
                    continue
                canonical = _ALLOWED_HEADERS.get(key.casefold())
                if canonical is None:
                    continue
                if not isinstance(value, str) or "\r" in key or "\n" in key or "\r" in value or "\n" in value:
                    raise ValueError
                headers[canonical] = value
            return ResolvedSource(
                url,
                headers,
                codec=codec,
                sample_rate=_positive_int(payload.get("asr")),
                channels=_positive_int(payload.get("audio_channels")),
                duration=_duration_seconds(payload.get("duration")),
            )
        except Exception:  # noqa: BLE001 - malformed provider mappings are untrusted
            raise ValueError from None

    async def fetch_metadata(self, reference: SourceReference) -> YouTubeVideoMetadata:
        if not isinstance(reference, SourceReference) or reference.kind is not SourceKind.YOUTUBE:
            raise SourceResolutionError() from None
        try:
            deno = _validated_deno(self._deno_locator())
            dependency_root = self._yt_dlp_locator()
            dependency_root = _validated_dependency_root(dependency_root)
            args = self._common_args(deno, dependency_root) + [
                "--no-playlist",
                "--print",
                '{"id":%(id)j,"title":%(title)j,"uploader":%(uploader)j,"duration":%(duration)j,"thumbnail":%(thumbnail)j}',
                _canonical_video_url(reference.identifier),
            ]
            payload = self._parse_document(await self._run_child(args, deadline=_VIDEO_DEADLINE, ceiling=_MAX_STDOUT))
            metadata = _metadata_from_mapping(payload)
            if metadata.video_id != reference.identifier:
                raise ValueError
            return metadata
        except (PlaybackUnavailable, SourceResolutionError):
            raise
        except Exception:  # noqa: BLE001 - provider output is untrusted
            raise SourceResolutionError() from None

    async def fetch_playlist(self, playlist_id: str, limit: int) -> tuple[YouTubeVideoMetadata, ...]:
        if not isinstance(playlist_id, str) or not playlist_id.isascii() or not playlist_id or len(playlist_id) > 128 or any(
            char not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-" for char in playlist_id
        ):
            raise SourceResolutionError() from None
        if isinstance(limit, bool) or not isinstance(limit, int) or not 0 < limit <= _MAX_PLAYLIST_LIMIT:
            raise SourceResolutionError() from None
        try:
            deno = _validated_deno(self._deno_locator())
            dependency_root = self._yt_dlp_locator()
            dependency_root = _validated_dependency_root(dependency_root)
            args = self._common_args(deno, dependency_root) + [
                "--yes-playlist",
                "--flat-playlist",
                "--lazy-playlist",
                "--playlist-end",
                str(limit),
                "--print",
                '{"id":%(id)j,"title":%(title)j,"uploader":%(uploader)j,"duration":%(duration)j,"thumbnail":%(thumbnail)j}',
                _canonical_playlist_url(playlist_id),
            ]
            # A playlist contains one projected document per item, not one
            # video's document. The validated 100-item cap bounds this to 400 KiB.
            output = await self._run_child(args, deadline=_PLAYLIST_DEADLINE, ceiling=limit * 4096)
            text = output.decode("utf-8")
            if sum(1 for line in text.splitlines() if line.strip()) > limit:
                raise ValueError
            results: list[YouTubeVideoMetadata] = []
            for line in text.splitlines():
                if len(results) >= limit:
                    break
                try:
                    metadata = _metadata_from_mapping(json.loads(line))
                except (ValueError, TypeError, json.JSONDecodeError):
                    continue
                results.append(metadata)
            return tuple(results)
        except (PlaybackUnavailable, SourceResolutionError):
            raise
        except Exception:  # noqa: BLE001 - provider output is untrusted
            raise SourceResolutionError() from None

    async def close(self) -> None:
        async with self._state_lock:
            self._closed = True
        while True:
            async with self._state_lock:
                children = tuple(
                    child for child in self._children if child not in self._reapers and child not in self._cleanup_futures
                )
                reapers = tuple(self._reapers.values())
                factories = tuple(self._factory_tasks)
                cleanup = tuple(self._cleanup_tasks)
                terminating = tuple(self._cleanup_futures.values())
            if children:
                cleanup_tasks = [
                    asyncio.create_task(self._cleanup_child(child, release_slot=True))
                    for child in children
                ]
                for task in cleanup_tasks:
                    self._track_cleanup_task(task)
                await asyncio.shield(asyncio.gather(*cleanup_tasks, return_exceptions=True))
                continue
            pending = tuple(dict.fromkeys((*reapers, *factories, *cleanup, *terminating)))
            if not pending:
                return
            try:
                await asyncio.wait_for(
                    asyncio.shield(asyncio.gather(*pending, return_exceptions=True)),
                    timeout=_CLOSE_DEADLINE,
                )
            except asyncio.TimeoutError:
                raise PlaybackUnavailable() from None


__all__ = ["YouTubeResolver", "YouTubeVideoMetadata", "parse_youtube_api_duration"]
