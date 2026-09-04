"""Bounded, child-process YouTube extraction with no in-process yt-dlp import."""

from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import json
import os
import signal
import sys
import unicodedata
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

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
_READ_CHUNK = 4096
_ALLOWED_HEADERS = {"user-agent": "User-Agent", "referer": "Referer", "origin": "Origin"}
_YT_DLP_BOOTSTRAP = (
    "import runpy,sys; sys.path.insert(0,sys.argv[1]); "
    "sys.argv=['yt_dlp',*sys.argv[2:]]; runpy.run_module('yt_dlp',run_name='__main__')"
)

ProcessFactory = Callable[..., Awaitable[Any]]


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
        object.__setattr__(self, "duration", _positive_int(self.duration))
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


def _yt_dlp_root() -> str:
    """Find the target-installed package without importing it in this process."""
    try:
        spec = importlib.util.find_spec("yt_dlp")
        locations = spec.submodule_search_locations if spec is not None else None
        package_dir = next(iter(locations)) if locations else None
        if not isinstance(package_dir, str):
            raise TypeError
        package = Path(package_dir)
        root = package.parent
        if not root.is_absolute() or not package.is_dir() or not (package / "__main__.py").is_file():
            raise ValueError
        return str(root)
    except Exception:  # noqa: BLE001 - optional dependency discovery is untrusted
        raise PlaybackUnavailable() from None


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
        self._closed = False
        self._state_lock = asyncio.Lock()

    def _common_args(self, deno: str, dependency_root: str) -> list[str]:
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
            "--no-netrc",
            "--no-download",
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
        try:
            child = await self._process_factory(*args, **kwargs)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - process factories expose platform errors
            raise PlaybackUnavailable() from None
        async with self._state_lock:
            if self._closed:
                await self._terminate(child)
                raise PlaybackUnavailable() from None
            self._children.add(child)
        return child

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
        try:
            child = await self._spawn(args)
            async def _read_and_wait() -> bytes:
                output = await self._read_stdout(child, ceiling)
                returncode = await child.wait()
                if returncode != 0:
                    raise RuntimeError
                return output

            return await asyncio.wait_for(_read_and_wait(), timeout=deadline)
        except asyncio.TimeoutError:
            raise PlaybackUnavailable() from None
        except asyncio.CancelledError:
            raise
        except (PlaybackUnavailable, SourceResolutionError):
            raise
        except Exception:  # noqa: BLE001 - child/process output is untrusted
            raise SourceResolutionError() from None
        finally:
            if child is not None:
                await self._terminate(child)
                async with self._state_lock:
                    self._children.discard(child)
            self._slots.release()

    async def _terminate(self, child: Any) -> None:
        pid = getattr(child, "pid", None)
        try:
            if getattr(child, "returncode", None) is not None:
                return
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
                return
            if isinstance(pid, int) and pid > 0 and os.name != "nt":
                with contextlib.suppress(ProcessLookupError, OSError):
                    _signal_process_group(pid, "SIGKILL")
            elif isinstance(pid, int) and pid > 0 and os.name == "nt":
                await self._force_windows_tree(pid)
            kill = getattr(child, "kill", None)
            if callable(kill):
                with contextlib.suppress(Exception):
                    kill()
            await self._wait_bounded(child)
        except asyncio.CancelledError:
            # Cleanup must finish even when the caller goes away.
            with contextlib.suppress(Exception):
                kill = getattr(child, "kill", None)
                if callable(kill):
                    kill()
                await self._wait_bounded(child)

    async def _wait_bounded(self, child: Any) -> bool:
        try:
            await asyncio.wait_for(child.wait(), timeout=_GRACE_PERIOD)
        except (asyncio.TimeoutError, OSError, ProcessLookupError):
            return False
        except Exception:  # noqa: BLE001 - process wait failures are sanitized
            return False
        return True

    async def _force_windows_tree(self, pid: int) -> None:
        # taskkill.exe is an absolute system tool and is used only for our tracked PID.
        system_root = os.environ.get("SystemRoot", r"C:\\Windows")
        taskkill = os.path.join(system_root, "System32", "taskkill.exe")
        if not os.path.isabs(taskkill) or not os.path.isfile(taskkill):
            return
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
            if not await self._wait_bounded(helper):
                force = getattr(helper, "kill", None)
                if callable(force):
                    with contextlib.suppress(Exception):
                        force()
                await self._wait_bounded(helper)
        except Exception:  # noqa: BLE001 - cleanup helper failures are sanitized
            return
        finally:
            if helper is not None and getattr(helper, "returncode", None) is None:
                force = getattr(helper, "kill", None)
                if callable(force):
                    with contextlib.suppress(Exception):
                        force()
                await self._wait_bounded(helper)

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
                duration=_positive_int(payload.get("duration")),
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
            output = await self._run_child(args, deadline=_PLAYLIST_DEADLINE, ceiling=min(_MAX_STDOUT, limit * 4096))
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
            if self._closed:
                return
            self._closed = True
            children = tuple(self._children)
        await asyncio.gather(*(self._terminate(child) for child in children), return_exceptions=True)


__all__ = ["YouTubeResolver", "YouTubeVideoMetadata"]
