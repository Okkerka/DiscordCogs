"""Offline diagnostics report independent failures without exposing secrets."""

from __future__ import annotations

import asyncio
import importlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


@pytest.fixture
def doctor(monkeypatch):
    module = importlib.import_module("TidalPlayerExp.playback.diagnostics")
    monkeypatch.setattr(module, "_version", lambda name: "1.2.3")
    monkeypatch.setattr(module, "_voice_runtime", lambda: (True, True))
    monkeypatch.setattr(module, "_youtube_readiness", lambda: (True, True))
    return module


def components(*, conflict=False, voice=None, owned=False):
    guild = SimpleNamespace(id=123, voice_client=voice)
    bot = SimpleNamespace(get_cog=lambda name: object() if conflict else None)
    session = SimpleNamespace(voice_client=voice) if owned else None
    backend = SimpleNamespace(get=AsyncMock(return_value=session))
    factory = SimpleNamespace(check=AsyncMock(return_value=SimpleNamespace(
        executable="C:/private/owner/ffmpeg.exe", version="7.1", libopus=True, passthrough=True,
    )))
    return bot, backend, factory, guild


@pytest.mark.asyncio
async def test_doctor_reports_native_capabilities_without_network_or_paths(doctor):
    bot, backend, factory, guild = components(voice=object(), owned=True)
    report = await doctor.collect_diagnostics(
        bot, backend, factory, tidal_authenticated=True, guild=guild,
    )
    assert "FFmpeg: 7.1" in report
    assert "libopus yes" in report
    assert "Opus output yes" in report
    assert "PyNaCl: 1.2.3 (ready)" in report
    assert "DAVE: 1.2.3 (ready)" in report
    assert "Voice: native" in report
    assert "cached authenticated" in report
    assert "private" not in report


@pytest.mark.asyncio
async def test_ffmpeg_failure_does_not_hide_other_diagnostics_or_leak_exception(doctor):
    bot, backend, factory, guild = components(conflict=True, voice=object())
    factory.check.side_effect = RuntimeError("https://secret.example/token?value=hidden")
    report = await doctor.collect_diagnostics(
        bot, backend, factory, tidal_authenticated=None, guild=guild,
    )
    assert "FFmpeg: unavailable" in report
    assert "yt-dlp: 1.2.3" in report
    assert "unload Audio" in report
    assert "Voice: foreign" in report
    assert "not checked" in report
    assert "secret" not in report
    assert "RuntimeError" not in report


@pytest.mark.asyncio
async def test_installed_voice_packages_need_restart_when_import_flags_false(doctor, monkeypatch):
    monkeypatch.setattr(doctor, "_voice_runtime", lambda: (False, False))
    bot, backend, factory, guild = components()
    report = await doctor.collect_diagnostics(
        bot, backend, factory, tidal_authenticated=False, guild=guild,
    )
    assert "PyNaCl: 1.2.3 (restart Red required)" in report
    assert "DAVE: 1.2.3 (restart Red required)" in report
    assert "tidalsetup login" in report
    assert "Voice: none" in report


@pytest.mark.asyncio
async def test_missing_optional_packages_report_remedy_in_dms(doctor, monkeypatch):
    monkeypatch.setattr(doctor, "_version", lambda name: None)
    monkeypatch.setattr(doctor, "_voice_runtime", lambda: (False, False))
    monkeypatch.setattr(doctor, "_youtube_readiness", lambda: (False, False))
    bot, backend, factory, _ = components()
    report = await doctor.collect_diagnostics(
        bot, backend, factory, tidal_authenticated=None,
    )
    assert "PyNaCl: missing" in report
    assert "DAVE: missing" in report
    assert "Deno: missing (executable unavailable)" in report
    assert "install/update cog requirements" in report
    assert "Voice: run in a server" in report
    backend.get.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancelled_doctor_does_not_swallow_cancellation(doctor):
    bot, backend, factory, guild = components()
    factory.check.side_effect = asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        await doctor.collect_diagnostics(
            bot, backend, factory, tidal_authenticated=None, guild=guild,
        )
