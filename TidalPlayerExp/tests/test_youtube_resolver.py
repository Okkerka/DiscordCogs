"""Behavioral tests for the isolated YouTube child-process resolver."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from collections.abc import Mapping
from types import SimpleNamespace

import pytest

from TidalPlayerExp.playback import (
    PlaybackUnavailable,
    ResolvedSource,
    SourceKind,
    SourceReference,
    SourceResolutionError,
)
from TidalPlayerExp.providers.youtube_resolver import YouTubeResolver

VIDEO_ID = "dQw4w9WgXcQ"


class _Stream:
    def __init__(self, data: bytes) -> None:
        self._data = data

    async def read(self, _size: int = -1) -> bytes:
        data, self._data = self._data, b""
        return data


class _Process:
    def __init__(self, output: Mapping[str, object]) -> None:
        self.pid = 1234
        self.stdout = _Stream((json.dumps(output) + "\n").encode())
        self.stderr = SimpleNamespace()
        self.returncode = 0
        self.terminated = False

    async def wait(self) -> int:
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.terminated = True


class _BlockingStream:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def read(self, _size: int = -1) -> bytes:
        self.started.set()
        await self.release.wait()
        return b""


class _RunningProcess:
    _next_pid = 20_000

    def __init__(self) -> None:
        self.pid = _RunningProcess._next_pid
        _RunningProcess._next_pid += 1
        self.stdout = _BlockingStream()
        self.returncode: int | None = None
        self.terminated = False

    async def wait(self) -> int:
        return self.returncode if self.returncode is not None else 0

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = 0

    def kill(self) -> None:
        self.terminated = True
        self.returncode = -9


class _EscalatingProcess:
    def __init__(self, pid: int | None = None) -> None:
        self.pid = pid
        self.stdout = _BlockingStream()
        self.returncode: int | None = None
        self.events: list[str] = []
        self._forced = asyncio.Event()

    def send_signal(self, _value: int) -> None:
        self.events.append("graceful-signal")

    def terminate(self) -> None:
        self.events.append("graceful-terminate")

    def kill(self) -> None:
        self.events.append("force-kill")
        self.returncode = -9
        self._forced.set()

    async def wait(self) -> int:
        if self.returncode is None:
            await self._forced.wait()
        return self.returncode if self.returncode is not None else -9


class _HelperProcess:
    def __init__(self) -> None:
        self.returncode: int | None = None
        self.killed = False

    async def wait(self) -> int:
        return self.returncode if self.returncode is not None else 0

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9


class _DeferredReapProcess:
    def __init__(self, pid: int | None = None) -> None:
        self.pid = pid
        self.stdout = _BlockingStream()
        self.returncode: int | None = None
        self.events: list[str] = []
        self.reaped = asyncio.Event()

    def send_signal(self, _value: int) -> None:
        self.events.append("graceful-signal")

    def terminate(self) -> None:
        self.events.append("graceful-terminate")

    def kill(self) -> None:
        self.events.append("force-kill")

    async def wait(self) -> int:
        await self.reaped.wait()
        self.returncode = 0
        return 0


class _DeferredReapHelper:
    def __init__(self) -> None:
        self.returncode: int | None = None
        self.reaped = asyncio.Event()
        self.killed = False

    async def wait(self) -> int:
        await self.reaped.wait()
        self.returncode = 0
        return 0

    def kill(self) -> None:
        self.killed = True


def _reference() -> SourceReference:
    return SourceReference(SourceKind.YOUTUBE, VIDEO_ID)


def _resolver_for(payload: object, tmp_path, calls: list | None = None) -> YouTubeResolver:
    process = _Process(payload if isinstance(payload, Mapping) else {"value": payload})

    async def factory(*args: object, **kwargs: object) -> _Process:
        if calls is not None:
            calls.append((args, kwargs))
        return process

    deno = tmp_path / "deno"
    deno.write_bytes(b"")
    return YouTubeResolver(process_factory=factory, deno_locator=lambda: str(deno))


@pytest.mark.asyncio
async def test_resolve_uses_canonical_url_explicit_deno_and_safe_child_environment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []
    process = _Process(
        {
            "url": "https://audio.example/stream",
            "http_headers": {"user-agent": "agent", "Authorization": "secret"},
            "acodec": "opus",
            "vcodec": "none",
            "asr": 48000,
            "audio_channels": 2,
            "duration": 120,
        }
    )

    async def factory(*args: object, **kwargs: object) -> _Process:
        calls.append((args, kwargs))
        return process

    parent_env = dict(os.environ)
    monkeypatch.setenv("YTDLP_PARENT_ONLY", "secret")
    deno_path = tmp_path / "deno"
    deno_path.write_bytes(b"")
    dependency_root = tmp_path / "downloader-lib"
    dependency_package = dependency_root / "yt_dlp"
    dependency_package.mkdir(parents=True)
    (dependency_package / "__main__.py").write_text("", encoding="utf-8")
    resolver = YouTubeResolver(
        process_factory=factory,
        deno_locator=lambda: str(deno_path),
        yt_dlp_locator=lambda: str(dependency_root),
    )

    result = await resolver.resolve(SourceReference(SourceKind.YOUTUBE, VIDEO_ID))

    assert isinstance(result, ResolvedSource)
    assert result.url == "https://audio.example/stream"
    assert dict(result.headers) == {"User-Agent": "agent"}
    assert result.sample_rate == 48000
    assert result.channels == 2
    assert result.duration == 120
    assert calls
    args, kwargs = calls[0]
    expected = (
        sys.executable,
        "-I",
        "-c",
        "import runpy,sys; sys.path.insert(0,sys.argv[1]); sys.argv=['yt_dlp',*sys.argv[2:]]; runpy.run_module('yt_dlp',run_name='__main__')",
        str(dependency_root),
        "--ignore-config",
        "--no-plugin-dirs",
        "--no-js-runtimes",
        "--js-runtimes",
        f"deno:{deno_path}",
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
        "--no-playlist",
        "--format",
        "bestaudio[acodec!=none]/bestaudio/best",
        "--print",
        '{"url":%(url)j,"http_headers":%(http_headers)j,"acodec":%(acodec)j,"vcodec":%(vcodec)j,"asr":%(asr)j,"audio_channels":%(audio_channels)j,"duration":%(duration)j}',
        f"https://www.youtube.com/watch?v={VIDEO_ID}",
    )
    assert args == expected
    assert kwargs["shell"] is False
    child_env = kwargs["env"]
    assert child_env["YTDLP_NO_PLUGINS"] == "1"
    assert "YTDLP_PARENT_ONLY" not in child_env
    assert dict(os.environ) == parent_env | {"YTDLP_PARENT_ONLY": "secret"}


@pytest.mark.asyncio
async def test_source_projection_rejects_unsafe_or_non_audio_payloads(tmp_path) -> None:
    base = {
        "url": "https://audio.example:443/stream",
        "http_headers": {},
        "acodec": "opus",
        "vcodec": "none",
    }
    for change in (
        {"url": "http://audio.example/stream"},
        {"url": "https://audio.example:bad/stream"},
        {"url": "https://user:pass@audio.example/stream"},
        {"acodec": "none"},
        {"acodec": " none "},
        {"acodec": " NoNe\t"},
        {"acodec": "   "},
        {"vcodec": "h264"},
        {"http_headers": {"Origin": "https://ok.example\r\nX-Leak: 1"}},
        {"asr": 0},
    ):
        resolver = _resolver_for({**base, **change}, tmp_path)
        with pytest.raises(SourceResolutionError):
            await resolver.resolve(_reference())


@pytest.mark.asyncio
async def test_metadata_is_normalized_bounded_and_has_no_media_fields(tmp_path) -> None:
    calls: list = []
    title = "  A\n  title  " + ("x" * 300)
    resolver = _resolver_for(
        {
            "id": VIDEO_ID,
            "title": title,
            "uploader": "  Channel\tName  ",
            "duration": 42,
            "thumbnail": "https://img.example:443/thumb.jpg",
        },
        tmp_path,
        calls,
    )

    metadata = await resolver.fetch_metadata(_reference())

    assert metadata.title.startswith("A title")
    assert len(metadata.title) == 200
    assert metadata.channel == "Channel Name"
    assert metadata.duration == 42
    assert metadata.thumbnail == "https://img.example:443/thumb.jpg"
    metadata_args = calls[0][0]
    assert "--dump-single-json" not in metadata_args
    assert "%(url)" not in next(value for value in metadata_args if value.startswith("{"))
    assert "http_headers" not in next(value for value in metadata_args if value.startswith("{"))


@pytest.mark.asyncio
async def test_playlist_caps_before_extraction_and_skips_invalid_entries(tmp_path) -> None:
    entries = [
        {"id": VIDEO_ID, "title": "One", "uploader": "Channel", "duration": 1},
        {"id": "private", "title": "Private", "uploader": "Channel"},
        {"id": "a" * 11, "title": "Two", "uploader": "Channel", "duration": 2},
    ]
    process = _Process(entries[0])
    process.stdout = _Stream(
        ("\n".join(json.dumps(entry) for entry in entries) + "\n").encode()
    )
    calls: list = []

    async def factory(*args: object, **kwargs: object) -> _Process:
        calls.append((args, kwargs))
        return process

    deno = tmp_path / "deno"
    deno.write_bytes(b"")
    resolver = YouTubeResolver(process_factory=factory, deno_locator=lambda: str(deno))
    with pytest.raises(SourceResolutionError):
        await resolver.fetch_playlist("PL_valid", 2)
    args = calls[0][0]
    assert args[args.index("--playlist-end") + 1] == "2"
    assert args[-1] == "https://www.youtube.com/playlist?list=PL_valid"


@pytest.mark.asyncio
async def test_playlist_accepts_exactly_limit_nonblank_json_lines(tmp_path) -> None:
    entries = [
        {"id": VIDEO_ID, "title": "One", "uploader": "Channel", "duration": 1},
        {"id": "a" * 11, "title": "Two", "uploader": "Channel", "duration": 2},
    ]
    process = _Process(entries[0])
    process.stdout = _Stream(("\n".join(json.dumps(entry) for entry in entries) + "\n").encode())
    deno = tmp_path / "deno"
    deno.write_bytes(b"")

    async def factory(*args: object, **kwargs: object) -> _Process:
        return process

    resolver = YouTubeResolver(process_factory=factory, deno_locator=lambda: str(deno))
    result = await resolver.fetch_playlist("PL_valid", 2)

    assert [item.video_id for item in result] == [VIDEO_ID, "a" * 11]


@pytest.mark.asyncio
async def test_playlist_counts_nonblank_invalid_excess_line(tmp_path) -> None:
    entries = [
        {"id": VIDEO_ID, "title": "One", "uploader": "Channel", "duration": 1},
        "private-or-deleted",
        {"id": "a" * 11, "title": "Two", "uploader": "Channel", "duration": 2},
    ]
    process = _Process(entries[0])
    process.stdout = _Stream(("\n".join(json.dumps(entry) for entry in entries) + "\n").encode())
    deno = tmp_path / "deno"
    deno.write_bytes(b"")

    async def factory(*args: object, **kwargs: object) -> _Process:
        return process

    resolver = YouTubeResolver(process_factory=factory, deno_locator=lambda: str(deno))
    with pytest.raises(SourceResolutionError):
        await resolver.fetch_playlist("PL_valid", 2)


@pytest.mark.asyncio
async def test_malformed_stdout_and_nonzero_exit_are_sanitized(tmp_path) -> None:
    deno = tmp_path / "deno"
    deno.write_bytes(b"")

    for raw, returncode in ((b"{\"url\":1}\n{\"leak\":\"secret\"}\n", 0), (b"traceback secret\n", 1)):
        process = _Process({})
        process.stdout = _Stream(raw)
        process.returncode = returncode

        async def factory(*args: object, _process=process, **kwargs: object) -> _Process:
            return _process

        resolver = YouTubeResolver(process_factory=factory, deno_locator=lambda: str(deno))
        with pytest.raises(SourceResolutionError) as caught:
            await resolver.resolve(_reference())
        assert "secret" not in str(caught.value)
        assert "traceback" not in repr(caught.value)


@pytest.mark.asyncio
async def test_invalid_deno_fails_before_child_creation(tmp_path) -> None:
    calls: list = []

    async def factory(*args: object, **kwargs: object) -> _Process:
        calls.append((args, kwargs))
        raise AssertionError("must not spawn")

    resolver = YouTubeResolver(process_factory=factory, deno_locator=lambda: "relative/deno")
    with pytest.raises(PlaybackUnavailable):
        await resolver.resolve(_reference())
    assert not calls


@pytest.mark.asyncio
@pytest.mark.parametrize("deno_value", ["missing/deno", "relative/deno"])
async def test_missing_or_non_file_deno_fails_before_child_creation(tmp_path, deno_value: str) -> None:
    calls: list = []

    async def factory(*args: object, **kwargs: object) -> _Process:
        calls.append((args, kwargs))
        raise AssertionError("must not spawn")

    if deno_value == "missing/deno":
        deno_value = str(tmp_path / "does-not-exist")
    resolver = YouTubeResolver(process_factory=factory, deno_locator=lambda: deno_value)
    with pytest.raises(PlaybackUnavailable):
        await resolver.resolve(_reference())
    assert not calls


@pytest.mark.asyncio
async def test_deno_directory_fails_before_child_creation(tmp_path) -> None:
    deno_dir = tmp_path / "deno-dir"
    deno_dir.mkdir()
    calls: list = []

    async def factory(*args: object, **kwargs: object) -> _Process:
        calls.append((args, kwargs))
        raise AssertionError("must not spawn")

    resolver = YouTubeResolver(process_factory=factory, deno_locator=lambda: str(deno_dir))
    with pytest.raises(PlaybackUnavailable):
        await resolver.resolve(_reference())
    assert not calls


@pytest.mark.asyncio
async def test_non_mapping_oversized_and_wrong_kind_outputs_are_sanitized(tmp_path) -> None:
    deno = tmp_path / "deno"
    deno.write_bytes(b"")
    payloads = [b"[]\n", (b"x" * 20_000) + b"\n"]
    for raw in payloads:
        process = _Process({})
        process.stdout = _Stream(raw)

        async def factory(*args: object, _process=process, **kwargs: object) -> _Process:
            return _process

        resolver = YouTubeResolver(process_factory=factory, deno_locator=lambda: str(deno))
        with pytest.raises(SourceResolutionError) as caught:
            await resolver.resolve(_reference())
        assert str(caught.value) == "Source resolution failed"

    resolver = YouTubeResolver(process_factory=factory, deno_locator=lambda: str(deno))
    with pytest.raises(SourceResolutionError):
        await resolver.resolve(SourceReference(SourceKind.TIDAL, "1"))


@pytest.mark.asyncio
async def test_process_factory_traceback_is_sanitized(tmp_path) -> None:
    deno = tmp_path / "deno"
    deno.write_bytes(b"")
    secret = "provider-traceback-secret"

    async def factory(*args: object, **kwargs: object) -> _Process:
        raise RuntimeError(secret)

    resolver = YouTubeResolver(process_factory=factory, deno_locator=lambda: str(deno))
    with pytest.raises(PlaybackUnavailable) as caught:
        await resolver.resolve(_reference())
    assert secret not in str(caught.value)
    assert secret not in repr(caught.value)
    assert caught.value.__cause__ is None


@pytest.mark.asyncio
async def test_bootstrap_runs_dependency_from_red_target_layout_without_parent_import(
    tmp_path,
) -> None:
    package_root = tmp_path / "downloader-lib"
    package = package_root / "yt_dlp"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("import sys\n", encoding="utf-8")
    (package / "__main__.py").write_text(
        "import json, sys\n"
        "print(json.dumps({'url':'https://audio.example/stream','http_headers':{},"
        "'acodec':'opus','vcodec':'none'}))\n",
        encoding="utf-8",
    )
    deno = tmp_path / "deno"
    deno.write_bytes(b"")
    parent_path = list(sys.path)
    parent_loaded = "yt_dlp" in sys.modules
    resolver = YouTubeResolver(
        deno_locator=lambda: str(deno),
        yt_dlp_locator=lambda: str(package_root),
    )

    result = await resolver.resolve(_reference())

    assert result.url == "https://audio.example/stream"
    assert list(sys.path) == parent_path
    assert ("yt_dlp" in sys.modules) == parent_loaded


@pytest.mark.asyncio
async def test_close_is_idempotent_and_rejects_new_work(tmp_path) -> None:
    calls: list = []
    resolver = _resolver_for(
        {
            "url": "https://audio.example/stream",
            "http_headers": {},
            "acodec": "opus",
            "vcodec": "none",
        },
        tmp_path,
        calls,
    )
    await resolver.close()
    await resolver.close()

    with pytest.raises(PlaybackUnavailable):
        await resolver.resolve(_reference())
    assert not calls


@pytest.mark.asyncio
async def test_close_terminates_live_children(tmp_path) -> None:
    process = _RunningProcess()
    deno = tmp_path / "deno"
    deno.write_bytes(b"")

    async def factory(*args: object, **kwargs: object) -> _RunningProcess:
        return process

    resolver = YouTubeResolver(process_factory=factory, deno_locator=lambda: str(deno))
    task = asyncio.create_task(resolver.resolve(_reference()))
    await process.stdout.started.wait()
    await resolver.close()
    assert process.terminated
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert resolver._slots._value == 2


@pytest.mark.asyncio
async def test_two_child_capacity_and_cancelled_waiter_reap_before_release(tmp_path) -> None:
    processes: list[_RunningProcess] = []

    async def factory(*args: object, **kwargs: object) -> _RunningProcess:
        process = _RunningProcess()
        processes.append(process)
        return process

    deno = tmp_path / "deno"
    deno.write_bytes(b"")
    resolver = YouTubeResolver(process_factory=factory, deno_locator=lambda: str(deno))
    first = asyncio.create_task(resolver.resolve(_reference()))
    second = asyncio.create_task(resolver.resolve(_reference()))
    await asyncio.sleep(0)
    while len(processes) < 2:
        await asyncio.sleep(0)
    await asyncio.gather(*(process.stdout.started.wait() for process in processes))

    third = asyncio.create_task(resolver.resolve(_reference()))
    await asyncio.sleep(0)
    assert len(processes) == 2
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    await asyncio.sleep(0)
    while len(processes) < 3:
        await asyncio.sleep(0)
    assert processes[0].terminated
    third.cancel()
    second.cancel()
    await asyncio.gather(second, third, return_exceptions=True)
    await resolver.close()


@pytest.mark.asyncio
async def test_timeout_terminates_owned_child_before_releasing_capacity(tmp_path, monkeypatch) -> None:
    module = __import__("TidalPlayerExp.providers.youtube_resolver", fromlist=["YouTubeResolver"])
    monkeypatch.setattr(module, "_VIDEO_DEADLINE", 0.01)
    process = _RunningProcess()
    deno = tmp_path / "deno"
    deno.write_bytes(b"")

    async def factory(*args: object, **kwargs: object) -> _RunningProcess:
        return process

    resolver = YouTubeResolver(process_factory=factory, deno_locator=lambda: str(deno))
    with pytest.raises(PlaybackUnavailable):
        await resolver.resolve(_reference())
    assert process.terminated


@pytest.mark.asyncio
async def test_timeout_retains_slot_and_close_until_child_is_confirmed_reaped(tmp_path, monkeypatch) -> None:
    module = __import__("TidalPlayerExp.providers.youtube_resolver", fromlist=["YouTubeResolver"])
    monkeypatch.setattr(module, "_VIDEO_DEADLINE", 0.01)
    processes = [_DeferredReapProcess(), _DeferredReapProcess()]
    calls: list = []
    deno = tmp_path / "deno"
    deno.write_bytes(b"")

    async def factory(*args: object, **kwargs: object) -> _DeferredReapProcess:
        calls.append((args, kwargs))
        return processes[len(calls) - 1]

    resolver = YouTubeResolver(process_factory=factory, deno_locator=lambda: str(deno))
    first = asyncio.create_task(resolver.resolve(_reference()))
    second = asyncio.create_task(resolver.resolve(_reference()))
    await asyncio.gather(first, second, return_exceptions=True)
    third = asyncio.create_task(resolver.resolve(_reference()))
    await asyncio.sleep(0.05)
    assert len(calls) == 2
    close_task = asyncio.create_task(resolver.close())
    await asyncio.sleep(0.05)
    assert not close_task.done()
    for process in processes:
        process.reaped.set()
    await close_task
    with pytest.raises(PlaybackUnavailable):
        await third


@pytest.mark.asyncio
async def test_cancellation_retains_cleanup_owner_until_child_is_reaped(tmp_path) -> None:
    processes = [_DeferredReapProcess(), _DeferredReapProcess()]
    calls: list = []
    deno = tmp_path / "deno"
    deno.write_bytes(b"")

    async def factory(*args: object, **kwargs: object) -> _DeferredReapProcess:
        calls.append((args, kwargs))
        return processes[len(calls) - 1]

    resolver = YouTubeResolver(process_factory=factory, deno_locator=lambda: str(deno))
    tasks = [asyncio.create_task(resolver.resolve(_reference())) for _ in processes]
    await asyncio.gather(*(process.stdout.started.wait() for process in processes))
    tasks[0].cancel()
    with pytest.raises(asyncio.CancelledError):
        await tasks[0]
    third = asyncio.create_task(resolver.resolve(_reference()))
    await asyncio.sleep(0.05)
    assert len(calls) == 2
    for process in processes:
        process.reaped.set()
    tasks[1].cancel()
    await asyncio.gather(tasks[1], return_exceptions=True)
    await resolver.close()
    with pytest.raises(PlaybackUnavailable):
        await third


@pytest.mark.asyncio
async def test_close_owns_child_created_after_factory_cancellation(tmp_path) -> None:
    factory_started = asyncio.Event()
    release_factory = asyncio.Event()
    process = _DeferredReapProcess()
    deno = tmp_path / "deno"
    deno.write_bytes(b"")

    async def factory(*args: object, **kwargs: object) -> _DeferredReapProcess:
        factory_started.set()
        await release_factory.wait()
        return process

    resolver = YouTubeResolver(process_factory=factory, deno_locator=lambda: str(deno))
    resolve_task = asyncio.create_task(resolver.resolve(_reference()))
    await factory_started.wait()
    close_task = asyncio.create_task(resolver.close())
    await asyncio.sleep(0.05)
    assert not close_task.done()
    resolve_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await resolve_task
    await asyncio.sleep(0.05)
    assert not close_task.done()
    release_factory.set()
    await asyncio.sleep(0.05)
    assert not close_task.done()
    process.reaped.set()
    await close_task
    assert not resolver._children
    assert not resolver._reapers


@pytest.mark.asyncio
async def test_cancellation_during_child_registration_retains_cleanup_owner(tmp_path) -> None:
    factory_started = asyncio.Event()
    process = _DeferredReapProcess()
    deno = tmp_path / "deno"
    deno.write_bytes(b"")

    async def factory(*args: object, **kwargs: object) -> _DeferredReapProcess:
        factory_started.set()
        return process

    resolver = YouTubeResolver(process_factory=factory, deno_locator=lambda: str(deno))
    resolve_task = asyncio.create_task(resolver.resolve(_reference()))
    await factory_started.wait()
    await resolver._state_lock.acquire()
    await asyncio.sleep(0.05)
    resolve_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await resolve_task
    resolver._state_lock.release()
    await asyncio.sleep(0.05)
    assert process in resolver._children or process in resolver._reapers
    assert resolver._slots._value == 1
    process.reaped.set()
    await resolver.close()
    assert resolver._slots._value == 2


@pytest.mark.asyncio
async def test_close_reports_bounded_cleanup_failure_without_orphaning_reaper(tmp_path, monkeypatch) -> None:
    module = __import__("TidalPlayerExp.providers.youtube_resolver", fromlist=["YouTubeResolver"])
    monkeypatch.setattr(module, "_VIDEO_DEADLINE", 0.01)
    monkeypatch.setattr(module, "_CLOSE_DEADLINE", 0.01)
    process = _DeferredReapProcess()
    deno = tmp_path / "deno"
    deno.write_bytes(b"")

    async def factory(*args: object, **kwargs: object) -> _DeferredReapProcess:
        return process

    resolver = YouTubeResolver(process_factory=factory, deno_locator=lambda: str(deno))
    with pytest.raises(PlaybackUnavailable):
        await resolver.resolve(_reference())
    with pytest.raises(PlaybackUnavailable):
        await resolver.close()
    assert resolver._reapers
    process.reaped.set()
    monkeypatch.setattr(module, "_CLOSE_DEADLINE", 0.25)
    await resolver.close()
    assert not resolver._reapers


@pytest.mark.asyncio
async def test_cleanup_escalates_after_bounded_grace_period(tmp_path) -> None:
    process = _EscalatingProcess()
    deno = tmp_path / "deno"
    deno.write_bytes(b"")
    resolver = YouTubeResolver(process_factory=lambda **kwargs: process, deno_locator=lambda: str(deno))

    started = time.monotonic()
    await resolver._terminate(process)

    assert process.events == ["graceful-terminate", "force-kill"]
    assert time.monotonic() - started >= 0.20


@pytest.mark.asyncio
async def test_windows_cleanup_signals_then_boundedly_reaps_taskkill_helper(tmp_path, monkeypatch) -> None:
    module = __import__("TidalPlayerExp.providers.youtube_resolver", fromlist=["YouTubeResolver"])
    system_root = tmp_path / "Windows"
    taskkill = system_root / "System32" / "taskkill.exe"
    taskkill.parent.mkdir(parents=True)
    taskkill.write_bytes(b"")
    process = _EscalatingProcess(pid=4321)
    helper = _HelperProcess()
    helper_calls: list[tuple[object, ...]] = []

    async def helper_factory(*args: object, **kwargs: object) -> _HelperProcess:
        helper_calls.append(args)
        return helper

    monkeypatch.setattr(module.os, "name", "nt")
    monkeypatch.setenv("SystemRoot", str(system_root))
    monkeypatch.setattr(module.asyncio, "create_subprocess_exec", helper_factory)
    resolver = YouTubeResolver()

    await resolver._terminate(process)

    assert process.events[0] == "graceful-signal"
    assert process.events[-1] == "force-kill"
    assert helper_calls and helper_calls[0][0] == str(taskkill)
    assert helper_calls[0][1:] == ("/PID", "4321", "/T", "/F")
    assert helper.killed


@pytest.mark.asyncio
async def test_windows_helper_reap_failure_retains_child_slot_and_close(tmp_path, monkeypatch) -> None:
    module = __import__("TidalPlayerExp.providers.youtube_resolver", fromlist=["YouTubeResolver"])
    system_root = tmp_path / "Windows"
    taskkill = system_root / "System32" / "taskkill.exe"
    taskkill.parent.mkdir(parents=True)
    taskkill.write_bytes(b"")
    processes = [_DeferredReapProcess(pid=4321), _DeferredReapProcess(pid=4322)]
    helpers = [_DeferredReapHelper(), _DeferredReapHelper()]
    calls: list[tuple[object, ...]] = []
    helper_calls: list[tuple[object, ...]] = []
    deno = tmp_path / "deno"
    deno.write_bytes(b"")

    async def factory(*args: object, **kwargs: object) -> _DeferredReapProcess:
        calls.append(args)
        return processes[len(calls) - 1]

    async def helper_factory(*args: object, **kwargs: object) -> _DeferredReapHelper:
        helper_calls.append(args)
        return helpers[len(helper_calls) - 1]

    monkeypatch.setattr(module, "_VIDEO_DEADLINE", 0.01)
    monkeypatch.setattr(module.os, "name", "nt")
    monkeypatch.setenv("SystemRoot", str(system_root))
    monkeypatch.setattr(module.asyncio, "create_subprocess_exec", helper_factory)
    resolver = YouTubeResolver(process_factory=factory, deno_locator=lambda: str(deno))
    first = asyncio.create_task(resolver.resolve(_reference()))
    second = asyncio.create_task(resolver.resolve(_reference()))
    await asyncio.gather(first, second, return_exceptions=True)
    third = asyncio.create_task(resolver.resolve(_reference()))
    await asyncio.sleep(0.05)
    assert len(calls) == 2
    close_task = asyncio.create_task(resolver.close())
    await asyncio.sleep(0.05)
    assert not close_task.done()
    for helper in helpers:
        helper.reaped.set()
    for process in processes:
        process.reaped.set()
    await close_task
    with pytest.raises(PlaybackUnavailable):
        await third
    assert len(helper_calls) == 2
    assert helper_calls[0][0] == str(taskkill)
