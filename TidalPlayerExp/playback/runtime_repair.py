"""Owner-triggered, checksum-pinned native tools, independent of pip's bin folder.

Discovery is read-only. Repair stages and probes both tools before atomically
switching a small marker; previous generations remain safe for active players.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import platform
import re
import shutil
import stat
import tarfile
import tempfile
import threading
import time
import uuid
import zipfile
from collections.abc import Callable, Collection
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import IO, Literal, TypeVar

import aiohttp

DENO_VERSION = "2.9.6"
_CHUNK = 256 * 1024
_BINARY_LIMIT = 256 * 1024 * 1024
_LICENSE_LIMIT = 1024 * 1024
_ARCHIVE_LIMIT = 1024 * 1024 * 1024
_PROBE_TIMEOUT = 15
_STAGING_MAX_AGE = 24 * 60 * 60
_RUNTIME_FILES = frozenset({
    "ffmpeg", "ffmpeg.exe", "deno", "deno.exe", "runtime.json", "FFMPEG-LICENSE.txt",
    "ffmpeg.download", "deno.download",
})
_T = TypeVar("_T")
Tool = Literal["ffmpeg", "deno"]


class RuntimeRepairError(Exception):
    """A fixed diagnostic code, never subprocess output or a private path."""

    def __init__(self, code: str) -> None:
        allowed = {
            "invalid_tool", "unsupported_platform", "closed", "busy",
            "download_failed", "download_size", "checksum_mismatch",
            "archive_invalid", "archive_member_invalid", "extracted_too_large",
            "probe_failed", "activation_failed", "cleanup_failed",
        }
        self.code = code if code in allowed else "activation_failed"
        super().__init__(self.code)


@dataclass(frozen=True)
class _Asset:
    url: str
    size: int
    sha256: str
    kind: str
    binary_member: str
    license_member: str | None = None


@dataclass(frozen=True)
class _Bundle:
    platform: str
    ffmpeg: _Asset
    deno: _Asset

    def identity(self) -> dict[str, str]:
        return {
            "platform": self.platform,
            "ffmpeg_archive": self.ffmpeg.sha256,
            "deno_archive": self.deno.sha256,
        }


def _platform_bundle() -> _Bundle:
    # Retained monthly LGPL builds, not the moving "latest" release. Hashes
    # match the publisher's release digests. No runtime checksum-file trust.
    builds = {
        ("Linux", "x86_64"): (
            "linux64", 112545684,
            "7d6d93e9c39e0e461feb13c118e91e4eec2515e4da3a01d4ad6790996731bbee",
            "x86_64-unknown-linux-gnu", 41582794,
            "394f07f4da2bebe6ce6f1e7ce0fa16429b29b08c35e3fac3fe25972676dff4b2",
        ),
        ("Linux", "aarch64"): (
            "linuxarm64", 97141720,
            "56b37b6f2832ba37bd4979ae5c4521ae718efa41846a0d3ecfbbe492137c66f6",
            "aarch64-unknown-linux-gnu", 39818676,
            "9a46afc6c392c7cd2ff71a31558935545b46408d0e87f7a86908c712721c046e",
        ),
        ("Windows", "x86_64"): (
            "win64", 146078616,
            "f6274bbd9c247f9e90c1bbed066b03ed4a3907cece2fb91be6dd352393936365",
            "x86_64-pc-windows-msvc", 42601047,
            "15e5300b0ba3c3695a7621d90160a746ec9e710228cee639afa9d580f6e3cd11",
        ),
    }
    machine = platform.machine().lower()
    machine = {"amd64": "x86_64", "arm64": "aarch64"}.get(machine, machine)
    system = platform.system()
    selected = builds.get((system, machine))
    if selected is None:
        raise RuntimeRepairError("unsupported_platform")
    target, size, digest, deno_target, deno_size, deno_digest = selected
    suffix = ".exe" if system == "Windows" else ""
    kind = "zip" if suffix else "tar.xz"
    stem = f"ffmpeg-n8.1.2-50-g1a748fe2cd-{target}-lgpl-8.1"
    return _Bundle(
        f"{system.lower()}-{machine}",
        _Asset(
            "https://github.com/BtbN/FFmpeg-Builds/releases/download/"
            f"autobuild-2026-08-31-13-27/{stem}.{kind}",
            size, digest, kind, f"{stem}/bin/ffmpeg{suffix}", f"{stem}/LICENSE.txt",
        ),
        _Asset(
            f"https://github.com/denoland/deno/releases/download/v{DENO_VERSION}/deno-{deno_target}.zip",
            deno_size, deno_digest, "zip", f"deno{suffix}",
        ),
    )


def _regular(path: Path) -> bool:
    return not path.is_symlink() and path.is_file()


def _plain(path: Path, *, directory: bool) -> bool:
    """Reject symlinks and Windows junctions/reparse points without following them."""
    info = path.lstat()
    return (
        not getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
        and (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))
    )


def _plain_directory_chain(path: Path) -> bool:
    return all(_plain(parent, directory=True) for parent in (*reversed(path.parents), path))


def _digest(path: Path, check: Callable[[], None]) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(_CHUNK):
            check()
            digest.update(chunk)
    return digest.hexdigest()


def _extract(asset: _Asset, archive_path: Path, destination: Path, stop: threading.Event) -> str:
    deadline = time.monotonic() + 120

    def check() -> None:
        if stop.is_set() or time.monotonic() > deadline:
            raise RuntimeRepairError("archive_invalid")

    if _digest(archive_path, check) != asset.sha256:
        raise RuntimeRepairError("checksum_mismatch")
    selected = {asset.binary_member: (destination, _BINARY_LIMIT)}
    if asset.license_member:
        selected[asset.license_member] = (destination.parent / "FFMPEG-LICENSE.txt", _LICENSE_LIMIT)
    found: set[str] = set()

    def copy(name: str, size: int, opener: Callable[[], IO[bytes] | None]) -> None:
        if name in found or size <= 0 or size > selected[name][1]:
            raise RuntimeRepairError("archive_member_invalid")
        found.add(name)
        target, limit = selected[name]
        written = 0
        source = opener()
        if source is None:
            raise RuntimeRepairError("archive_member_invalid")
        with source, target.open("xb") as output:
            while chunk := source.read(_CHUNK):
                check()
                written += len(chunk)
                if written > limit or written > size:
                    raise RuntimeRepairError("extracted_too_large")
                output.write(chunk)
        if written != size:
            raise RuntimeRepairError("archive_invalid")

    try:
        if asset.kind == "zip":
            with zipfile.ZipFile(archive_path) as archive:
                members = archive.infolist()
                if len(members) > 10000 or sum(item.file_size for item in members) > _ARCHIVE_LIMIT:
                    raise RuntimeRepairError("extracted_too_large")
                for member in members:
                    check()
                    if member.filename not in selected:
                        continue
                    mode = stat.S_IFMT(member.external_attr >> 16)
                    if member.is_dir() or mode not in (0, stat.S_IFREG) or member.flag_bits & 1:
                        raise RuntimeRepairError("archive_member_invalid")
                    copy(member.filename, member.file_size, partial(archive.open, member))
        elif asset.kind == "tar.xz":
            with tarfile.open(archive_path, "r|xz") as tar:
                expanded = 0
                for count, entry in enumerate(tar, 1):
                    check()
                    expanded += entry.size
                    if count > 10000 or expanded > _ARCHIVE_LIMIT:
                        raise RuntimeRepairError("extracted_too_large")
                    if entry.name not in selected:
                        continue
                    if not entry.isfile():
                        raise RuntimeRepairError("archive_member_invalid")
                    copy(entry.name, entry.size, partial(tar.extractfile, entry))
        else:
            raise RuntimeRepairError("archive_invalid")
    except (OSError, EOFError, ValueError, tarfile.TarError, zipfile.BadZipFile) as exc:
        raise RuntimeRepairError("archive_invalid") from exc
    if found != selected.keys():
        raise RuntimeRepairError("archive_member_invalid")
    destination.chmod(0o700)
    return _digest(destination, check)


async def _worker(function: Callable[[threading.Event], _T]) -> _T:
    """Cancellation joins cooperative disk work before its directory is removed."""
    stop = threading.Event()
    task = asyncio.create_task(asyncio.to_thread(function, stop))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        stop.set()
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except Exception:  # noqa: BLE001 - finish cancellation after the worker has stopped
                break
        if not task.cancelled():
            task.exception()  # Retrieve a cooperative worker's failure.
        raise


async def _probe(executable: Path, *args: str) -> bytes:
    process = None
    try:
        async with asyncio.timeout(_PROBE_TIMEOUT):
            process = await asyncio.create_subprocess_exec(
                str(executable), *args, stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                env={**os.environ, "DENO_NO_UPDATE_CHECK": "1"},
            )
            assert process.stdout is not None
            output = bytearray()
            while chunk := await process.stdout.read(1024):
                output.extend(chunk)
                if len(output) > 8192:
                    raise RuntimeRepairError("probe_failed")
            if await process.wait() != 0:
                raise RuntimeRepairError("probe_failed")
            return bytes(output)
    except (OSError, TimeoutError) as exc:
        raise RuntimeRepairError("probe_failed") from exc
    finally:
        if process is not None and process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            # A second cancellation (e.g. owner abort followed by cog unload)
            # must not release staging while a child still holds its executable.
            reap = asyncio.create_task(process.wait())
            interrupted = False
            while not reap.done():
                try:
                    await asyncio.shield(reap)
                except asyncio.CancelledError:
                    interrupted = True
            reap.result()
            if interrupted:
                raise asyncio.CancelledError


async def _validate(ffmpeg: Path, deno: Path) -> None:
    opus = await _probe(
        ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
        "-i", "anullsrc=r=48000:cl=stereo", "-t", "0.1", "-c:a", "libopus",
        "-f", "opus", "pipe:1",
    )
    if not opus.startswith(b"OggS") or b"OpusHead" not in opus:
        raise RuntimeRepairError("probe_failed")
    version = await _probe(deno, "--version")
    if not version.startswith(f"deno {DENO_VERSION} ".encode()):
        raise RuntimeRepairError("probe_failed")
    result = await _probe(
        deno, "eval", "--no-config", "--no-lock", "--no-remote", "--no-npm",
        "--cached-only", "--no-prompt", "--deny-net", "--deny-import",
        "if (6 * 7 !== 42) throw new Error('runtime check failed'); console.log('tidalplayerexp-deno-ok')",
    )
    if result.strip() != b"tidalplayerexp-deno-ok":
        raise RuntimeRepairError("probe_failed")


class ManagedRuntime:
    """Own repair work and locate persistent native binaries for this cog."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory.absolute()
        self._task: asyncio.Task[None] | None = None
        self._closed = False

    def _active(self) -> tuple[Path, dict] | None:
        root = self.directory
        marker = root / "active"
        try:
            if root.is_symlink() or not _regular(marker) or marker.stat().st_size > 80:
                return None
            with marker.open("rb") as stream:
                name = stream.read(80).decode("ascii").strip()
            if not re.fullmatch(r"runtime-[0-9a-f]{32}", name):
                return None
            generation = root / name
            if generation.is_symlink() or generation.resolve() != root.resolve() / name:
                return None
            manifest = generation / "runtime.json"
            if not _regular(manifest) or manifest.stat().st_size > 4096:
                return None
            metadata = json.loads(manifest.read_bytes())
            if not isinstance(metadata, dict):
                return None
            for tool in ("ffmpeg", "deno"):
                filename = metadata.get(tool)
                if filename not in (tool, tool + ".exe"):
                    return None
                binary = generation / filename
                if not _regular(binary) or not os.access(binary, os.X_OK):
                    return None
            return generation, metadata
        except (OSError, UnicodeError, ValueError):
            return None

    def locate(self, tool: Tool) -> str | None:
        """Read the validated active pair without installing or executing anything."""
        if tool not in ("ffmpeg", "deno"):
            raise RuntimeRepairError("invalid_tool")
        active = self._active()
        return str(active[0] / active[1][tool]) if active else None

    async def repair(self) -> None:
        """Download, verify and activate pinned tools, or validate a reusable pair."""
        if self._closed:
            raise RuntimeRepairError("closed")
        if self._task is not None:
            raise RuntimeRepairError("busy")
        task = asyncio.create_task(self._repair())
        self._task = task
        try:
            await task
        finally:
            self._task = None

    async def cleanup(self, *, protected_paths: Collection[Path]) -> None:
        """Keep active tools, one newest backup, and every explicitly protected path.

        Call before starting playback/extraction, or supply ALL executable paths
        still used by children or queued work. Repair never prunes generations:
        a child can outlive the marker that originally selected its executable.
        Unknown files, linked directories and invalid active markers fail closed.
        Abandoned staging/marker files are eligible only after 24 hours.
        """
        if self._closed:
            raise RuntimeRepairError("closed")
        if self._task is not None:
            raise RuntimeRepairError("busy")
        try:
            protected = tuple(Path(path).resolve() for path in protected_paths)
        except (OSError, RuntimeError) as exc:
            raise RuntimeRepairError("cleanup_failed") from exc
        task = asyncio.create_task(_worker(partial(self._cleanup, protected)))
        self._task = task
        try:
            await task
        finally:
            self._task = None

    def _cleanup(self, protected_paths: tuple[Path, ...], stop: threading.Event) -> None:
        root = self.directory
        try:
            if not root.exists() or not _plain_directory_chain(root):
                return
            children = list(root.iterdir())
            generations = [
                path for path in children
                if re.fullmatch(r"runtime-[0-9a-f]{32}", path.name)
                and _plain(path, directory=True)
            ]
            marker = root / "active"
            active: Path | None = None
            if marker.exists() and _plain(marker, directory=False) and marker.stat().st_size <= 80:
                name = marker.read_bytes().decode("ascii").strip()
                if re.fullmatch(r"runtime-[0-9a-f]{32}", name) and root / name in generations:
                    active = root / name
            retained = {active} if active is not None else set(generations)
            backups = [path for path in generations if path != active]
            if backups:
                retained.add(max(backups, key=lambda path: (path.stat().st_mtime_ns, path.name)))
            cutoff = time.time() - _STAGING_MAX_AGE
            for candidate in children:
                if stop.is_set():
                    return
                if candidate in retained or any(
                    path == candidate or candidate in path.parents or path in candidate.parents
                    for path in protected_paths
                ):
                    continue
                is_generation = candidate in generations
                is_staging = re.fullmatch(r"\.staging-[a-z0-9_]{8}", candidate.name)
                is_marker = re.fullmatch(r"\.active-[0-9a-f]{32}", candidate.name)
                if not (is_generation or is_staging or is_marker):
                    continue
                if not is_generation and candidate.lstat().st_mtime > cutoff:
                    continue
                # All installer-owned directories are flat. Reject an entire
                # candidate with unknown contents; never recurse into any tree.
                if is_marker:
                    files = [candidate] if _plain(candidate, directory=False) else []
                elif _plain(candidate, directory=True):
                    files = list(candidate.iterdir())
                    if any(
                        path.name not in _RUNTIME_FILES or not _plain(path, directory=False)
                        for path in files
                    ):
                        continue
                else:
                    continue
                for path in files:
                    if stop.is_set():
                        return
                    if not _plain_directory_chain(path.parent) or not _plain(path, directory=False):
                        break
                    path.unlink()
                else:
                    if not is_marker and not stop.is_set() and _plain_directory_chain(candidate):
                        candidate.rmdir()
        except (OSError, UnicodeError, ValueError) as exc:
            raise RuntimeRepairError("cleanup_failed") from exc

    async def close(self) -> None:
        """Stop repairs before cog unload; existing activated binaries stay on disk."""
        self._closed = True
        task = self._task
        if task is not None:
            task.cancel()
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                if not task.done():
                    raise
            except RuntimeRepairError:
                pass

    async def _download(self, session: aiohttp.ClientSession, asset: _Asset, path: Path) -> None:
        try:
            async with session.get(asset.url) as response:
                if response.status != 200:
                    raise RuntimeRepairError("download_failed")
                length = response.headers.get("Content-Length")
                if length is not None and int(length) != asset.size:
                    raise RuntimeRepairError("download_size")
                total = 0
                with path.open("xb") as output:
                    async for chunk in response.content.iter_chunked(_CHUNK):
                        total += len(chunk)
                        if total > asset.size:
                            raise RuntimeRepairError("download_size")
                        output.write(chunk)
                if total != asset.size:
                    raise RuntimeRepairError("download_size")
        except (aiohttp.ClientError, OSError, ValueError, TimeoutError) as exc:
            raise RuntimeRepairError("download_failed") from exc

    async def _repair(self) -> None:
        stage: Path | None = None
        marker: Path | None = None
        try:
            bundle = _platform_bundle()
            active = self._active()
            if active and all(active[1].get(key) == value for key, value in bundle.identity().items()):
                generation, metadata = active

                def intact(stop: threading.Event) -> bool:
                    def check() -> None:
                        if stop.is_set():
                            raise RuntimeRepairError("closed")
                    return all(
                        _digest(generation / metadata[tool], check) == metadata.get(tool + "_sha256")
                        for tool in ("ffmpeg", "deno")
                    )

                if await _worker(intact):
                    try:
                        await _validate(generation / metadata["ffmpeg"], generation / metadata["deno"])
                        return
                    except RuntimeRepairError:
                        pass  # An explicit repair replaces a matching but broken installation.
            root = self.directory
            if root.is_symlink():
                raise RuntimeRepairError("activation_failed")
            root.mkdir(parents=True, exist_ok=True)
            stage = Path(tempfile.mkdtemp(prefix=".staging-", dir=root))
            metadata = bundle.identity()
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=300, connect=30, sock_read=30),
                auto_decompress=False, trust_env=False,
            ) as session:
                for tool, asset in (("ffmpeg", bundle.ffmpeg), ("deno", bundle.deno)):
                    archive = stage / f"{tool}.download"
                    await self._download(session, asset, archive)
                    filename = tool + (".exe" if asset.binary_member.endswith(".exe") else "")
                    destination = stage / filename
                    digest = await _worker(partial(_extract, asset, archive, destination))
                    archive.unlink()
                    metadata[tool] = filename
                    metadata[tool + "_sha256"] = digest
            await _validate(stage / metadata["ffmpeg"], stage / metadata["deno"])
            (stage / "runtime.json").write_text(json.dumps(metadata), encoding="ascii")
            generation = root / f"runtime-{uuid.uuid4().hex}"
            stage.rename(generation)
            stage = generation  # Still disposable until the marker replacement succeeds.
            marker = root / f".active-{uuid.uuid4().hex}"
            with marker.open("x", encoding="ascii") as output:
                output.write(generation.name + "\n")
                output.flush()
                os.fsync(output.fileno())
            os.replace(marker, root / "active")
            stage = None
        except OSError as exc:
            raise RuntimeRepairError("activation_failed") from exc
        finally:
            if marker is not None:
                marker.unlink(missing_ok=True)
            if stage is not None:
                shutil.rmtree(stage)
