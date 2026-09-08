"""Owner repair wiring, persistent executable selection, and unload ownership."""

from __future__ import annotations

import asyncio
import importlib
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


def _runtime(**overrides):
    values = {"locate": lambda tool: f"/managed/{tool}", "repair": AsyncMock(), "close": AsyncMock()}
    values.update(overrides)
    return SimpleNamespace(**values)


def test_playback_uses_managed_binaries_without_installing(cog, monkeypatch):
    monkeypatch.delenv("IMAGEIO_FFMPEG_EXE", raising=False)
    cog.runtime = _runtime()

    assert cog.source_factory._locator() == "/managed/ffmpeg"
    assert cog.youtube_resolver._deno_locator() == "/managed/deno"
    cog.runtime.repair.assert_not_called()


def test_explicit_ffmpeg_override_still_takes_precedence(cog, monkeypatch):
    monkeypatch.setenv("IMAGEIO_FFMPEG_EXE", "/configured/ffmpeg")
    cog.runtime = _runtime()

    assert cog.source_factory._locator() == "/configured/ffmpeg"


def test_unrepaired_install_keeps_existing_dependency_discovery(cog, monkeypatch):
    module = importlib.import_module(cog.__class__.__module__)
    monkeypatch.delenv("IMAGEIO_FFMPEG_EXE", raising=False)
    monkeypatch.setattr(module, "_default_ffmpeg_locator", lambda: "/packaged/ffmpeg", raising=False)
    monkeypatch.setattr(module, "_deno_path", lambda: "/packaged/deno", raising=False)
    cog.runtime = _runtime(locate=lambda tool: None)

    assert cog.source_factory._locator() == "/packaged/ffmpeg"
    assert cog.youtube_resolver._deno_locator() == "/packaged/deno"


@pytest.mark.asyncio
async def test_repair_requests_reload_only_after_success(cog):
    cog.runtime = _runtime()
    ctx = SimpleNamespace(send=AsyncMock(), clean_prefix=">")

    await cog.tidalsetup_repair(ctx)

    cog.runtime.repair.assert_awaited_once()
    assert "reload TidalPlayerExp" in ctx.send.call_args.args[0]
    assert "doctor" in ctx.send.call_args.args[0]


@pytest.mark.asyncio
async def test_doctor_uses_same_deno_locator_as_playback(cog, monkeypatch):
    diagnostics = importlib.import_module("TidalPlayerExp.playback.diagnostics")
    collect = AsyncMock(return_value="ready")
    monkeypatch.setattr(diagnostics, "collect_diagnostics", collect)
    cog.runtime = _runtime()
    ctx = SimpleNamespace(send=AsyncMock(), clean_prefix=">", guild=None)
    await cog.tidalsetup_doctor(ctx)
    assert collect.call_args.kwargs["deno_locator"]() == "/managed/deno"
    cog.runtime.repair.assert_not_called()


@pytest.mark.asyncio
async def test_repair_failure_never_exposes_internal_error_or_claims_success(cog, caplog):
    cog.runtime = _runtime(repair=AsyncMock(side_effect=OSError("private-path?token=secret")))
    ctx = SimpleNamespace(send=AsyncMock(), clean_prefix=">")

    await cog.tidalsetup_repair(ctx)

    message = ctx.send.call_args.args[0]
    assert "failed" in message.lower()
    assert "secret" not in message + caplog.text
    assert "reload TidalPlayerExp" not in message


@pytest.mark.asyncio
async def test_repair_propagates_cancellation(cog):
    cog.runtime = _runtime(repair=AsyncMock(side_effect=asyncio.CancelledError))
    ctx = SimpleNamespace(send=AsyncMock(), clean_prefix=">")
    with pytest.raises(asyncio.CancelledError):
        await cog.tidalsetup_repair(ctx)


@pytest.mark.asyncio
async def test_unloading_cog_rejects_new_repair(cog):
    cog.runtime = _runtime()
    cog._closing = True
    ctx = SimpleNamespace(send=AsyncMock(), clean_prefix=">")
    await cog.tidalsetup_repair(ctx)
    cog.runtime.repair.assert_not_called()


@pytest.mark.asyncio
async def test_unload_closes_installer_even_if_playback_cleanup_fails(cog, caplog):
    cog.runtime = _runtime()
    cog.backend = SimpleNamespace(close=AsyncMock(side_effect=OSError("secret")))
    cog.tidal = SimpleNamespace(unload=AsyncMock())
    await cog.cog_unload()
    cog.runtime.close.assert_awaited_once()
    cog.tidal.unload.assert_awaited_once()
    assert "secret" not in caplog.text


def test_real_red_repair_command_rejects_non_owner():
    script = r'''
import asyncio, sys
import discord
from types import SimpleNamespace
sys.path.insert(0, sys.argv[1])
from redbot.core import commands
from redbot.core.commands.requires import PrivilegeLevel
from TidalPlayerExp.tidalplayer import TidalPlayerExp

async def main():
    async def is_owner(user):
        return user.id == 42
    cmd = TidalPlayerExp.tidalsetup_repair
    assert cmd.name == 'repair' and cmd.parent.name == 'tidalsetup'
    assert cmd.requires.privilege_level is PrivilegeLevel.BOT_OWNER
    cmd.requires.ready_event.set()
    ctx = SimpleNamespace(
        bot=SimpleNamespace(is_owner=is_owner), author=SimpleNamespace(id=1),
        cog=None, guild=None, bot_permissions=discord.Permissions.all(),
    )
    assert not await cmd.requires.verify(ctx), 'non-owner can invoke executable installer'
    ctx.author.id = 42
    assert await cmd.requires.verify(ctx)
asyncio.run(main())
'''
    result = subprocess.run(
        [sys.executable, "-I", "-c", script, str(Path(__file__).resolve().parents[2])],
        capture_output=True, text=True, timeout=30, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
