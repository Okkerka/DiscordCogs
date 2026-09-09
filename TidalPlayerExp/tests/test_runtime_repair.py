from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import stat
import subprocess
import tarfile
import time
import zipfile
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from TidalPlayerExp.playback import runtime_repair


class _FakeProcess:
    def __init__(
        self, returncode: int = 0, *, wait_event: asyncio.Event | None = None, output: bytes = b"",
    ) -> None:
        self.returncode: int | None = None
        self._final_returncode = returncode
        self._wait_event = wait_event
        self.terminated = False
        self.killed = False
        self.stdout = asyncio.StreamReader()
        self.stdout.feed_data(output)
        self.stdout.feed_eof()

    async def wait(self) -> int:
        if self._wait_event is not None:
            await self._wait_event.wait()
        if self.returncode is None:
            self.returncode = self._final_returncode
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15
        if self._wait_event is not None:
            self._wait_event.set()

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9
        if self._wait_event is not None:
            self._wait_event.set()


class _Content:
    def __init__(self, body: bytes, *, gate: asyncio.Event | None = None) -> None:
        self._body = body
        self._gate = gate

    async def iter_chunked(self, size: int):
        if self._gate is not None:
            await self._gate.wait()
        for start in range(0, len(self._body), size):
            yield self._body[start : start + size]


class _Response:
    def __init__(
        self,
        body: bytes,
        *,
        status: int = 200,
        content_length: int | None = None,
        gate: asyncio.Event | None = None,
    ) -> None:
        self.status = status
        self.headers = {}
        if content_length is not None:
            self.headers["Content-Length"] = str(content_length)
        self.content = _Content(body, gate=gate)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None


class _Session:
    def __init__(self, responses: dict[str, _Response], **_kwargs: Any) -> None:
        self._responses = responses

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    def get(self, url: str, **_kwargs: Any) -> _Response:
        return self._responses[url]


def _zip(entries: dict[str, bytes], *, symlink: str | None = None) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, body in entries.items():
            archive.writestr(name, body)
        if symlink is not None:
            info = zipfile.ZipInfo(symlink)
            info.create_system = 3
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(info, "elsewhere")
    return output.getvalue()


def _tar_xz(
    entries: dict[str, bytes],
    *,
    symlink: tuple[str, str] | None = None,
) -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:xz") as archive:
        for name, body in entries.items():
            info = tarfile.TarInfo(name)
            info.size = len(body)
            info.mode = 0o755 if name.endswith("/ffmpeg") else 0o644
            archive.addfile(info, io.BytesIO(body))
        if symlink is not None:
            info = tarfile.TarInfo(symlink[0])
            info.type = tarfile.SYMTYPE
            info.linkname = symlink[1]
            archive.addfile(info)
    return output.getvalue()


def _asset(url: str, body: bytes, kind: str, binary_member: str, license_member: str | None = None):
    return runtime_repair._Asset(
        url=url,
        size=len(body),
        sha256=hashlib.sha256(body).hexdigest(),
        kind=kind,
        binary_member=binary_member,
        license_member=license_member,
    )


def _bundle(ffmpeg_body: bytes, deno_body: bytes):
    ffmpeg_root = "ffmpeg-test-linux64"
    ffmpeg_asset = _asset(
        "https://example.invalid/ffmpeg.tar.xz",
        ffmpeg_body,
        "tar.xz",
        f"{ffmpeg_root}/bin/ffmpeg",
        f"{ffmpeg_root}/LICENSE.txt",
    )
    deno_asset = _asset(
        "https://example.invalid/deno.zip", deno_body, "zip", "deno"
    )
    return runtime_repair._Bundle("linux-x64", ffmpeg_asset, deno_asset)


@pytest.fixture
def archives():
    ffmpeg_root = "ffmpeg-test-linux64"
    ffmpeg = _tar_xz(
        {
            f"{ffmpeg_root}/bin/ffmpeg": b"ffmpeg binary",
            f"{ffmpeg_root}/LICENSE.txt": b"LGPL",
        }
    )
    deno = _zip({"deno": b"deno binary"})
    return ffmpeg, deno


@pytest.fixture
def successful_boundaries(monkeypatch: pytest.MonkeyPatch, archives):
    ffmpeg, deno = archives
    bundle = _bundle(ffmpeg, deno)
    responses = {
        bundle.ffmpeg.url: _Response(ffmpeg, content_length=len(ffmpeg)),
        bundle.deno.url: _Response(deno, content_length=len(deno)),
    }
    monkeypatch.setattr(runtime_repair, "_platform_bundle", lambda: bundle)
    monkeypatch.setattr(
        runtime_repair.aiohttp,
        "ClientSession",
        lambda **kwargs: _Session(responses, **kwargs),
    )
    calls: list[tuple[Any, ...]] = []

    async def spawn(*args: Any, **_kwargs: Any) -> _FakeProcess:
        calls.append(args)
        if "-f" in args:
            output = b"OggS\x00OpusHead"
        elif "--version" in args:
            output = b"deno 2.9.6 (stable, release, x86_64-unknown-linux-gnu)\n"
        else:
            output = b"tidalplayerexp-deno-ok\n"
        return _FakeProcess(output=output)

    monkeypatch.setattr(runtime_repair.asyncio, "create_subprocess_exec", spawn)
    return bundle, calls


def test_error_exposes_only_fixed_code() -> None:
    error = runtime_repair.RuntimeRepairError("probe_failed")

    assert error.code == "probe_failed"
    assert str(error) == "probe_failed"
    assert error.args == ("probe_failed",)


def test_locate_is_read_only_and_rejects_untrusted_marker(tmp_path: Path) -> None:
    root = tmp_path / "native-runtime"
    root.mkdir()
    (root / "active").write_text("../../outside\n", encoding="ascii")
    runtime = runtime_repair.ManagedRuntime(root)

    assert runtime.locate("ffmpeg") is None
    assert runtime.locate("deno") is None
    assert sorted(path.name for path in root.iterdir()) == ["active"]


@pytest.mark.asyncio
async def test_repair_installs_and_atomically_activates_pair(
    tmp_path: Path, successful_boundaries
) -> None:
    bundle, calls = successful_boundaries
    runtime = runtime_repair.ManagedRuntime(tmp_path / "native-runtime")

    await runtime.repair()

    ffmpeg = Path(runtime.locate("ffmpeg") or "")
    deno = Path(runtime.locate("deno") or "")
    assert ffmpeg.read_bytes() == b"ffmpeg binary"
    assert deno.read_bytes() == b"deno binary"
    assert ffmpeg.parent == deno.parent
    assert (ffmpeg.parent / "FFMPEG-LICENSE.txt").read_bytes() == b"LGPL"
    assert [Path(call[0]).name for call in calls] == ["ffmpeg", "deno", "deno"]
    assert not list((tmp_path / "native-runtime").glob(".staging-*"))
    assert bundle.ffmpeg.url.startswith("https://")


@pytest.mark.asyncio
async def test_matching_healthy_install_is_reused_without_download(
    tmp_path: Path, successful_boundaries, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = runtime_repair.ManagedRuntime(tmp_path / "native-runtime")
    await runtime.repair()
    original = runtime.locate("ffmpeg")

    class ForbiddenSession:
        def __init__(self, **_kwargs: Any) -> None:
            pytest.fail("matching installation must not download again")

    monkeypatch.setattr(runtime_repair.aiohttp, "ClientSession", ForbiddenSession)
    await runtime.repair()

    assert runtime.locate("ffmpeg") == original


@pytest.mark.asyncio
async def test_corrupt_download_preserves_existing_active_pair(
    tmp_path: Path, successful_boundaries, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = runtime_repair.ManagedRuntime(tmp_path / "native-runtime")
    await runtime.repair()
    original = (runtime.locate("ffmpeg"), runtime.locate("deno"))
    original_bundle, _calls = successful_boundaries
    changed = _bundle(
        _tar_xz(
            {
                "ffmpeg-test-linux64/bin/ffmpeg": b"new ffmpeg",
                "ffmpeg-test-linux64/LICENSE.txt": b"LGPL",
            }
        ),
        _zip({"deno": b"new deno"}),
    )
    changed = runtime_repair._Bundle(
        changed.platform,
        runtime_repair._Asset(
            **{
                **changed.ffmpeg.__dict__,
                "sha256": original_bundle.ffmpeg.sha256,
            }
        ),
        changed.deno,
    )
    responses = {
        changed.ffmpeg.url: _Response(
            _tar_xz(
                {
                    "ffmpeg-test-linux64/bin/ffmpeg": b"new ffmpeg",
                    "ffmpeg-test-linux64/LICENSE.txt": b"LGPL",
                }
            )
        ),
        changed.deno.url: _Response(_zip({"deno": b"new deno"})),
    }
    monkeypatch.setattr(runtime_repair, "_platform_bundle", lambda: changed)
    monkeypatch.setattr(
        runtime_repair.aiohttp,
        "ClientSession",
        lambda **kwargs: _Session(responses, **kwargs),
    )

    with pytest.raises(runtime_repair.RuntimeRepairError) as caught:
        await runtime.repair()

    assert caught.value.code in {"download_size", "checksum_mismatch"}
    assert (runtime.locate("ffmpeg"), runtime.locate("deno")) == original
    assert not list((tmp_path / "native-runtime").glob(".staging-*"))


@pytest.mark.asyncio
async def test_second_concurrent_repair_fails_busy(
    tmp_path: Path, archives, successful_boundaries, monkeypatch: pytest.MonkeyPatch
) -> None:
    ffmpeg, deno = archives
    bundle = _bundle(ffmpeg, deno)
    gate = asyncio.Event()
    entered = asyncio.Event()

    class BlockingContent(_Content):
        async def iter_chunked(self, size: int):
            entered.set()
            async for chunk in super().iter_chunked(size):
                yield chunk

    responses = {
        bundle.ffmpeg.url: _Response(ffmpeg),
        bundle.deno.url: _Response(deno),
    }
    responses[bundle.ffmpeg.url].content = BlockingContent(ffmpeg, gate=gate)
    monkeypatch.setattr(runtime_repair, "_platform_bundle", lambda: bundle)
    monkeypatch.setattr(
        runtime_repair.aiohttp,
        "ClientSession",
        lambda **kwargs: _Session(responses, **kwargs),
    )
    runtime = runtime_repair.ManagedRuntime(tmp_path / "runtime")
    first = asyncio.create_task(runtime.repair())
    await entered.wait()

    with pytest.raises(runtime_repair.RuntimeRepairError) as caught:
        await runtime.repair()
    assert caught.value.code == "busy"

    gate.set()
    await first


@pytest.mark.asyncio
async def test_close_cancels_repair_and_permanently_closes_instance(
    tmp_path: Path, archives, monkeypatch: pytest.MonkeyPatch
) -> None:
    ffmpeg, deno = archives
    bundle = _bundle(ffmpeg, deno)
    gate = asyncio.Event()
    entered = asyncio.Event()

    class BlockingContent(_Content):
        async def iter_chunked(self, size: int):
            entered.set()
            async for chunk in super().iter_chunked(size):
                yield chunk

    response = _Response(ffmpeg)
    response.content = BlockingContent(ffmpeg, gate=gate)
    responses = {
        bundle.ffmpeg.url: response,
        bundle.deno.url: _Response(deno),
    }
    monkeypatch.setattr(runtime_repair, "_platform_bundle", lambda: bundle)
    monkeypatch.setattr(
        runtime_repair.aiohttp,
        "ClientSession",
        lambda **kwargs: _Session(responses, **kwargs),
    )
    runtime = runtime_repair.ManagedRuntime(tmp_path / "runtime")
    task = asyncio.create_task(runtime.repair())
    await entered.wait()

    await runtime.close()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert not list((tmp_path / "runtime").glob(".staging-*"))
    with pytest.raises(runtime_repair.RuntimeRepairError) as caught:
        await runtime.repair()
    assert caught.value.code == "closed"


@pytest.mark.asyncio
async def test_probe_double_cancellation_waits_for_child_reaping(monkeypatch):
    waiting = asyncio.Event()
    release = asyncio.Event()

    class SlowReap(_FakeProcess):
        def __init__(self):
            super().__init__()
            self.stdout = asyncio.StreamReader()  # Probe blocks reading until cancelled.

        def kill(self):
            self.killed = True

        async def wait(self):
            waiting.set()
            await release.wait()
            self.returncode = -9
            return -9

    child = SlowReap()

    async def spawn(*args, **kwargs):
        return child

    monkeypatch.setattr(runtime_repair.asyncio, "create_subprocess_exec", spawn)
    probe = asyncio.create_task(runtime_repair._probe(Path("unused")))
    await asyncio.sleep(0)
    probe.cancel()
    await waiting.wait()
    probe.cancel()
    await asyncio.sleep(0)
    assert not probe.done(), "cleanup must survive repeated cancellation"
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await probe
    assert child.killed and child.returncode == -9


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["checksum", "truncated", "oversized", "header", "http"])
async def test_bad_download_never_executes_or_activates(
    failure, tmp_path, successful_boundaries, archives, monkeypatch,
):
    bundle, calls = successful_boundaries
    ffmpeg, deno = archives
    body = ffmpeg
    kwargs = {}
    if failure == "checksum":
        body = bytes([ffmpeg[0] ^ 1]) + ffmpeg[1:]
    elif failure == "truncated":
        body = ffmpeg[:-1]
    elif failure == "oversized":
        body = ffmpeg + b"extra"
    elif failure == "header":
        kwargs["content_length"] = len(ffmpeg) + 1
    else:
        kwargs["status"] = 403
    responses = {bundle.ffmpeg.url: _Response(body, **kwargs), bundle.deno.url: _Response(deno)}
    monkeypatch.setattr(runtime_repair.aiohttp, "ClientSession", lambda **kw: _Session(responses))
    root = tmp_path / "runtime"
    runtime = runtime_repair.ManagedRuntime(root)
    with pytest.raises(runtime_repair.RuntimeRepairError):
        await runtime.repair()
    assert not calls
    assert runtime.locate("ffmpeg") is None
    assert not list(root.iterdir())


@pytest.mark.parametrize("kind", ["tar.xz", "zip"])
@pytest.mark.parametrize("failure", ["symlink", "duplicate", "missing", "oversized"])
def test_rejects_unsafe_or_missing_selected_archive_member(kind, failure, tmp_path, monkeypatch):
    import threading

    member = "archive/bin/ffmpeg"
    entries = {} if failure in ("symlink", "missing") else {member: b"binary"}
    link = member if failure in ("symlink", "duplicate") else None
    if kind == "tar.xz":
        body = _tar_xz(entries, symlink=(link, "../../../outside") if link else None)
    elif failure == "duplicate":
        # A regular entry followed by a symlink is still an invalid duplicate.
        with pytest.warns(UserWarning, match="Duplicate name"):
            body = _zip(entries, symlink=link)
    else:
        body = _zip(entries, symlink=link)
    if failure == "oversized":
        body = _tar_xz(entries) if kind == "tar.xz" else _zip(entries)
        monkeypatch.setattr(runtime_repair, "_BINARY_LIMIT", 3)
    asset = _asset("https://example.invalid/archive", body, kind, member)
    path = tmp_path / "archive"
    path.write_bytes(body)
    target = tmp_path / "ffmpeg"
    with pytest.raises(runtime_repair.RuntimeRepairError) as error:
        runtime_repair._extract(asset, path, target, threading.Event())
    assert error.value.code == "archive_member_invalid"
    assert not (tmp_path.parent / "outside").exists()


@pytest.mark.parametrize("kind", ["tar.xz", "zip"])
def test_unselected_archive_paths_are_not_extracted(kind, tmp_path):
    import threading

    entries = {"ffmpeg": b"binary", "../../outside": b"do not extract"}
    body = _tar_xz(entries) if kind == "tar.xz" else _zip(entries)
    path = tmp_path / "archive"
    path.write_bytes(body)
    asset = _asset("https://example.invalid/archive", body, kind, "ffmpeg")
    digest = runtime_repair._extract(asset, path, tmp_path / "ffmpeg", threading.Event())
    assert digest == hashlib.sha256(b"binary").hexdigest()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["archive", "ffmpeg"]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["probe", "activation"])
async def test_failure_preserves_previous_pair(failure, tmp_path, successful_boundaries, monkeypatch):
    root = tmp_path / "runtime"
    runtime = runtime_repair.ManagedRuntime(root)
    await runtime.repair()
    original = (runtime.locate("ffmpeg"), runtime.locate("deno"))
    marker = (root / "active").read_bytes()
    # Change the supported bundle identity to force a fresh installation.
    bundle, _calls = successful_boundaries
    monkeypatch.setattr(runtime_repair, "_platform_bundle", lambda: replace(bundle, platform="updated"))
    if failure == "probe":
        async def spawn(*args, **kwargs):
            return _FakeProcess(returncode=-11)
        monkeypatch.setattr(runtime_repair.asyncio, "create_subprocess_exec", spawn)
    else:
        def fail_replace(*args):
            raise OSError("private path")
        monkeypatch.setattr(runtime_repair.os, "replace", fail_replace)
    with pytest.raises(runtime_repair.RuntimeRepairError) as error:
        await runtime.repair()
    assert error.value.code == ("probe_failed" if failure == "probe" else "activation_failed")
    assert (root / "active").read_bytes() == marker
    assert (runtime.locate("ffmpeg"), runtime.locate("deno")) == original
    assert len(list(root.iterdir())) == 2  # Original generation and marker only.


@pytest.mark.asyncio
async def test_read_only_locator_rejects_manifest_escape(tmp_path, successful_boundaries):
    runtime = runtime_repair.ManagedRuntime(tmp_path / "runtime")
    await runtime.repair()
    generation = Path(runtime.locate("ffmpeg")).parent
    manifest = generation / "runtime.json"
    metadata = json.loads(manifest.read_bytes())
    metadata["ffmpeg"] = "../../outside"
    manifest.write_text(json.dumps(metadata))
    assert runtime.locate("ffmpeg") is None
    assert runtime.locate("deno") is None


@pytest.mark.asyncio
async def test_extraction_cancellation_joins_worker_before_removing_staging(
    tmp_path, successful_boundaries, monkeypatch,
):
    import threading

    entered = asyncio.Event()
    loop = asyncio.get_running_loop()
    finished = threading.Event()
    root = tmp_path / "runtime"

    def blocking_extract(asset, archive, destination, stop):
        loop.call_soon_threadsafe(entered.set)
        assert stop.wait(timeout=5), "cancellation did not stop extraction"
        assert archive.exists(), "staging removed while worker was using it"
        finished.set()
        raise runtime_repair.RuntimeRepairError("archive_invalid")

    monkeypatch.setattr(runtime_repair, "_extract", blocking_extract)
    runtime = runtime_repair.ManagedRuntime(root)
    task = asyncio.create_task(runtime.repair())
    await asyncio.wait_for(entered.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert finished.is_set()
    assert not list(root.iterdir())


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["exit", "output", "timeout"])
async def test_probe_bounds_failure_and_reaps_child(failure, monkeypatch):
    child = _FakeProcess(returncode=-11 if failure == "exit" else 0, output=b"" if failure == "exit" else b"x" * 8193)
    if failure == "timeout":
        child.stdout = asyncio.StreamReader()
        monkeypatch.setattr(runtime_repair, "_PROBE_TIMEOUT", 0.01)

    async def spawn(*args, **kwargs):
        return child

    monkeypatch.setattr(runtime_repair.asyncio, "create_subprocess_exec", spawn)
    with pytest.raises(runtime_repair.RuntimeRepairError) as error:
        await runtime_repair._probe(Path("unused"))
    assert error.value.code == "probe_failed"
    assert child.returncode is not None


def _retention_generation(root: Path, number: int, *, age: int) -> Path:
    generation = root / f"runtime-{number:032x}"
    generation.mkdir(parents=True)
    (generation / "ffmpeg").write_bytes(b"ffmpeg")
    (generation / "deno").write_bytes(b"deno")
    (generation / "runtime.json").write_text('{"ffmpeg": "ffmpeg", "deno": "deno"}')
    timestamp = time.time() - age
    os.utime(generation, (timestamp, timestamp))
    return generation


@pytest.mark.asyncio
async def test_cleanup_keeps_active_newest_backup_and_in_use_generation(tmp_path):
    root = tmp_path / "runtime"
    active = _retention_generation(root, 1, age=500)
    backup = _retention_generation(root, 2, age=100)
    protected = _retention_generation(root, 3, age=300)
    obsolete = _retention_generation(root, 4, age=400)
    (root / "active").write_text(active.name)

    await runtime_repair.ManagedRuntime(root).cleanup(protected_paths=(protected / "ffmpeg",))

    assert active.is_dir() and backup.is_dir() and protected.is_dir()
    assert not obsolete.exists()
    assert (root / "active").read_text() == active.name


@pytest.mark.asyncio
async def test_cleanup_removes_only_abandoned_owned_staging_and_markers(tmp_path):
    root = tmp_path / "runtime"
    root.mkdir()
    stale = root / ".staging-abcdefgh"
    stale.mkdir()
    (stale / "ffmpeg.download").write_bytes(b"partial")
    recent = root / ".staging-12345678"
    recent.mkdir()
    unknown = root / ".staging-user-data"
    unknown.mkdir()
    marker = root / (".active-" + "a" * 32)
    marker.write_text("unfinished")
    for path in (stale, marker, unknown):
        timestamp = time.time() - 172800
        os.utime(path, (timestamp, timestamp))

    await runtime_repair.ManagedRuntime(root).cleanup(protected_paths=())

    assert not stale.exists() and not marker.exists()
    assert recent.is_dir() and unknown.is_dir()


@pytest.mark.asyncio
@pytest.mark.parametrize("marker", [None, "../../outside", "runtime-" + "f" * 32])
async def test_cleanup_preserves_generations_without_trusted_active_marker(tmp_path, marker):
    root = tmp_path / "runtime"
    generations = [_retention_generation(root, number, age=number) for number in range(1, 4)]
    if marker is not None:
        (root / "active").write_text(marker)

    await runtime_repair.ManagedRuntime(root).cleanup(protected_paths=())

    assert all(generation.is_dir() for generation in generations)


@pytest.mark.asyncio
async def test_cleanup_preserves_candidates_with_unexpected_files_or_subdirectories(tmp_path):
    root = tmp_path / "runtime"
    active, backup, unknown, nested = [
        _retention_generation(root, number, age=number * 100) for number in range(1, 5)
    ]
    (root / "active").write_text(active.name)
    (unknown / "private-data").write_bytes(b"keep")
    (nested / "nested").mkdir()

    await runtime_repair.ManagedRuntime(root).cleanup(protected_paths=())

    assert (unknown / "ffmpeg").read_bytes() == b"ffmpeg"
    assert (unknown / "private-data").read_bytes() == b"keep"
    assert (nested / "ffmpeg").read_bytes() == b"ffmpeg"
    assert (nested / "nested").is_dir()


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["root", "ancestor", "generation", "file"])
async def test_cleanup_never_follows_symlinks(tmp_path, boundary):
    root = tmp_path / "runtime"
    active, backup, obsolete = [
        _retention_generation(root, number, age=number * 100) for number in range(1, 4)
    ]
    (root / "active").write_text(active.name)
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "ffmpeg"
    sentinel.write_bytes(b"keep")
    try:
        if boundary == "root":
            alias = tmp_path / "alias"
            alias.symlink_to(root, target_is_directory=True)
            root = alias
        elif boundary == "ancestor":
            alias = tmp_path / "alias"
            alias.symlink_to(tmp_path, target_is_directory=True)
            root = alias / "runtime"
        elif boundary == "generation":
            linked = root / ("runtime-" + "f" * 32)
            linked.symlink_to(outside, target_is_directory=True)
        else:
            (obsolete / "ffmpeg").unlink()
            (obsolete / "ffmpeg").symlink_to(sentinel)
    except OSError as exc:
        if getattr(exc, "winerror", None) == 1314:
            pytest.skip("Windows symlink privilege unavailable")
        raise

    await runtime_repair.ManagedRuntime(root).cleanup(protected_paths=())

    assert sentinel.read_bytes() == b"keep"
    assert active.is_dir() and backup.is_dir()
    if boundary != "generation":
        assert obsolete.is_dir()


@pytest.mark.asyncio
async def test_cleanup_cancellation_joins_worker_and_excludes_repair(tmp_path, monkeypatch):
    import threading

    runtime = runtime_repair.ManagedRuntime(tmp_path / "runtime")
    entered = asyncio.Event()
    finished = threading.Event()
    loop = asyncio.get_running_loop()

    def blocked_cleanup(protected_paths, stop):
        loop.call_soon_threadsafe(entered.set)
        assert stop.wait(timeout=5)
        finished.set()

    monkeypatch.setattr(runtime, "_cleanup", blocked_cleanup, raising=False)
    task = asyncio.create_task(runtime.cleanup(protected_paths=()))
    await asyncio.wait_for(entered.wait(), timeout=5)
    with pytest.raises(runtime_repair.RuntimeRepairError, match="busy"):
        await runtime.repair()
    await runtime.close()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert finished.is_set()
    with pytest.raises(runtime_repair.RuntimeRepairError, match="closed"):
        await runtime.cleanup(protected_paths=())


@pytest.mark.asyncio
@pytest.mark.parametrize("protection", ["normalized", "root"])
async def test_cleanup_normalizes_protected_paths_and_honors_protected_root(tmp_path, protection):
    root = tmp_path / "runtime"
    active, backup, in_use = [
        _retention_generation(root, number, age=number * 100) for number in range(1, 4)
    ]
    (root / "active").write_text(active.name)
    path = root if protection == "root" else root / "unused" / ".." / in_use.name / "ffmpeg"

    await runtime_repair.ManagedRuntime(root).cleanup(protected_paths=(path,))

    assert (in_use / "ffmpeg").read_bytes() == b"ffmpeg"


@pytest.mark.asyncio
async def test_cleanup_reports_file_errors_with_fixed_diagnostic(tmp_path, monkeypatch):
    root = tmp_path / "runtime"
    active, backup, obsolete = [
        _retention_generation(root, number, age=number * 100) for number in range(1, 4)
    ]
    (root / "active").write_text(active.name)
    unlink = Path.unlink

    def locked(path, *args, **kwargs):
        if path.parent == obsolete:
            raise PermissionError("private path or locked executable")
        return unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", locked)
    with pytest.raises(runtime_repair.RuntimeRepairError, match="^cleanup_failed$"):
        await runtime_repair.ManagedRuntime(root).cleanup(protected_paths=())
    assert (active / "ffmpeg").read_bytes() == b"ffmpeg"
    assert (backup / "ffmpeg").read_bytes() == b"ffmpeg"


@pytest.mark.asyncio
async def test_repair_never_prunes_generations_that_players_might_still_use(
    tmp_path, successful_boundaries, monkeypatch,
):
    runtime = runtime_repair.ManagedRuntime(tmp_path / "runtime")
    bundle, _calls = successful_boundaries
    installed = []
    for number in range(3):
        changed = replace(bundle, platform=f"updated-{number}")
        monkeypatch.setattr(runtime_repair, "_platform_bundle", lambda: changed)
        await runtime.repair()
        installed.append(Path(runtime.locate("ffmpeg")))
    assert len(set(installed)) == 3
    assert all(path.read_bytes() == b"ffmpeg binary" for path in installed)


@pytest.mark.asyncio
@pytest.mark.skipif(os.name != "nt", reason="Windows junction boundary")
@pytest.mark.parametrize("boundary", ["root", "generation"])
async def test_cleanup_preserves_windows_junction_targets(tmp_path, boundary):
    root = tmp_path / "runtime"
    active, backup, obsolete = [
        _retention_generation(root, number, age=number * 100) for number in range(1, 4)
    ]
    (root / "active").write_text(active.name)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "ffmpeg").write_bytes(b"keep")
    alias = tmp_path / "alias" if boundary == "root" else root / ("runtime-" + "f" * 32)
    target = root if boundary == "root" else outside
    subprocess.run(
        ["cmd.exe", "/c", "mklink", "/J", str(alias), str(target)],
        check=True, capture_output=True,
    )
    try:
        await runtime_repair.ManagedRuntime(alias if boundary == "root" else root).cleanup(
            protected_paths=(),
        )
        assert (outside / "ffmpeg").read_bytes() == b"keep"
        assert (active / "ffmpeg").read_bytes() == b"ffmpeg"
        if boundary == "root":
            assert (obsolete / "ffmpeg").read_bytes() == b"ffmpeg"
        assert alias.is_dir()
    finally:
        # Removing the junction itself does not traverse or remove its target.
        alias.rmdir()
