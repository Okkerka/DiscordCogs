"""Read-only local capability reporting; never resolve media or check login."""

from __future__ import annotations

import importlib
import importlib.metadata
import platform
import re
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .backend import NativePlaybackBackend
    from .ffmpeg import FFmpegSourceFactory


def _safe_version(value: object) -> str:
    if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9.+_-]{1,64}", value):
        return value
    return "unknown"


def _version(distribution: str) -> str | None:
    try:
        return _safe_version(importlib.metadata.version(distribution))
    except Exception:  # noqa: BLE001 - broken optional package metadata is diagnostic input
        return None


def _voice_runtime() -> tuple[bool, bool]:
    try:
        module = importlib.import_module("discord.voice_client")
        return bool(getattr(module, "has_nacl", False)), bool(getattr(module, "has_dave", False))
    except Exception:  # noqa: BLE001 - independent optional capability probe
        return False, False


def _youtube_readiness(*, deno_locator: Callable[[], str] | None = None) -> tuple[str | None, bool]:
    # These filesystem-only helpers do not import yt-dlp or execute/download tools.
    from ..providers.youtube_resolver import (
        _deno_path,
        _validated_deno,
        _yt_dlp_installation,
    )

    try:
        _, version = _yt_dlp_installation()
    except Exception:  # noqa: BLE001 - independent optional capability probe
        version = None
    try:
        _validated_deno((deno_locator or _deno_path)())
    except Exception:  # noqa: BLE001 - report readiness without provider error details
        return version, False
    return version, True


def _voice_status(version: str | None, ready: bool) -> str:
    if ready:
        return f"{version or 'unknown'} (ready)"
    if version is not None:
        return f"{version} (installed but unavailable; update requirements, then reload TidalPlayerExp)"
    return "missing (install/update cog requirements, then reload TidalPlayerExp)"


async def collect_diagnostics(
    bot: Any,
    backend: NativePlaybackBackend,
    source_factory: FFmpegSourceFactory,
    *,
    tidal_authenticated: bool | None,
    guild: Any = None,
    deno_locator: Callable[[], str] | None = None,
    managed_deno_version: str | None = None,
) -> str:
    """Report local readiness without exposing credentials, media URLs, or paths.

    ``tidal_authenticated`` is the handler's cached status, not a fresh remote
    check. FFmpeg's owned, bounded capability probe is the only executable run.
    """
    nacl_ready, dave_ready = _voice_runtime()
    youtube_version, deno_ready = (
        _youtube_readiness() if deno_locator is None else _youtube_readiness(deno_locator=deno_locator)
    )
    lines = [
        f"Python: {platform.python_version()}",
        f"Red: {_version('Red-DiscordBot') or 'unknown'}",
        f"discord.py: {_version('discord.py') or 'unknown'}",
        f"PyNaCl: {_voice_status(_version('PyNaCl'), nacl_ready)}",
        f"DAVE: {_voice_status(_version('davey'), dave_ready)}",
    ]
    try:
        capability = await source_factory.check()
    except Exception:  # noqa: BLE001 - one unavailable tool must not hide other results
        lines.append("FFmpeg: unavailable; install/update cog requirements or check the configured binary")
    else:
        lines.append(
            f"FFmpeg: {_safe_version(capability.version)} (executable available; "
            f"libopus {'yes' if capability.libopus else 'no'}; "
            f"Opus output {'yes' if capability.passthrough else 'no'})"
        )
    last_failure = getattr(source_factory, "last_failure", None)
    if isinstance(last_failure, str) and re.fullmatch(
        r"[a-z][a-z0-9_]{0,63} \(exit=(?:-?[0-9]{1,10}|unknown)\)", last_failure,
    ):
        lines.append(f"FFmpeg last failure: {last_failure}")
        if last_failure.startswith("process_signal_"):
            lines.append("FFmpeg remedy: binary crashed; use [p]setup repair, then reload TidalPlayerExp")
    lines.extend([
        (
            f"yt-dlp: {_safe_version(youtube_version) if youtube_version else 'missing or outdated'} "
            f"({'ready' if youtube_version else 'requires 2026.8.19 or newer'})"
        ),
        f"YouTube EJS: {_version('yt-dlp-ejs') or 'missing; install/update cog requirements'}",
        (
            f"Deno: {_safe_version(managed_deno_version) if managed_deno_version else _version('deno') or 'missing'} "
            f"({'managed; ' if managed_deno_version else ''}"
            f"{'executable ready' if deno_ready else 'executable unavailable'})"
        ),
    ])
    if not youtube_version or not deno_ready:
        lines.append("YouTube remedy: install/update cog requirements, then reload TidalPlayerExp")
    if not deno_ready:
        lines.append("Deno remedy: [p]setup repair installs a persistent cog-local executable")
    if tidal_authenticated is None:
        auth = "not checked (offline diagnostic)"
    elif tidal_authenticated:
        auth = "cached authenticated (not revalidated)"
    else:
        auth = "not authenticated; use [p]setup login for TIDAL"
    lines.append(f"TIDAL: {auth}; not required for direct YouTube")
    lines.append("Audio: conflict; unload Audio" if bot.get_cog("Audio") is not None else "Audio: no conflict")
    if guild is None:
        ownership = "run in a server to inspect ownership"
    elif guild.voice_client is None:
        ownership = "none"
    else:
        try:
            session = await backend.get(guild.id)
        except Exception:  # noqa: BLE001 - reporting must never mutate voice state to recover
            ownership = "unknown"
        else:
            ownership = "native" if session is not None and session.voice_client is guild.voice_client else "foreign"
    lines.append(f"Voice: {ownership}")
    return "\n".join(lines)
