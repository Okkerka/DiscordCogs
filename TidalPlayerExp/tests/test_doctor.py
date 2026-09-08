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
    monkeypatch.setattr(module, "_youtube_readiness", lambda: ("2026.8.19", True))
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
    assert "yt-dlp: 2026.8.19 (ready)" in report
    assert "unload Audio" in report
    assert "Voice: foreign" in report
    assert "not checked" in report
    assert "secret" not in report
    assert "RuntimeError" not in report


@pytest.mark.asyncio
async def test_unavailable_voice_packages_do_not_send_owner_into_restart_loop(doctor, monkeypatch):
    monkeypatch.setattr(doctor, "_voice_runtime", lambda: (False, False))
    bot, backend, factory, guild = components()
    report = await doctor.collect_diagnostics(
        bot, backend, factory, tidal_authenticated=False, guild=guild,
    )
    assert "PyNaCl: 1.2.3 (installed but unavailable" in report
    assert "DAVE: 1.2.3 (installed but unavailable" in report
    assert "reload TidalPlayerExp" in report
    assert "restart Red" not in report
    assert "tidalsetup login" in report
    assert "Voice: none" in report


@pytest.mark.asyncio
async def test_missing_optional_packages_report_remedy_in_dms(doctor, monkeypatch):
    monkeypatch.setattr(doctor, "_version", lambda name: None)
    monkeypatch.setattr(doctor, "_voice_runtime", lambda: (False, False))
    monkeypatch.setattr(doctor, "_youtube_readiness", lambda: (None, False))
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


@pytest.mark.asyncio
async def test_doctor_reports_last_ffmpeg_failure_separately_from_capabilities(doctor):
    bot, backend, factory, guild = components()
    factory.last_failure = "http_403 (exit=1)"
    report = await doctor.collect_diagnostics(
        bot, backend, factory, tidal_authenticated=None, guild=guild,
    )
    assert "FFmpeg: 7.1" in report
    assert "FFmpeg last failure: http_403 (exit=1)" in report


@pytest.mark.asyncio
async def test_crashed_ffmpeg_reports_owner_repair_instead_of_dependency_reinstall(doctor):
    bot, backend, factory, guild = components()
    factory.last_failure = "process_signal_11 (exit=-11)"
    report = await doctor.collect_diagnostics(
        bot, backend, factory, tidal_authenticated=None, guild=guild,
    )
    assert "tidalsetup repair" in report


def test_managed_deno_is_ready_even_after_downloader_binary_disappears(monkeypatch, tmp_path):
    module = importlib.import_module("TidalPlayerExp.playback.diagnostics")
    youtube = importlib.import_module("TidalPlayerExp.providers.youtube_resolver")
    binary = tmp_path / "deno"
    binary.write_bytes(b"managed executable")
    binary.chmod(0o755)
    monkeypatch.setattr(youtube, "_yt_dlp_installation", lambda: ("/lib", "2026.8.19"))

    def missing():
        raise FileNotFoundError("Downloader removed bin")

    monkeypatch.setattr(youtube, "_deno_path", missing)
    assert module._youtube_readiness(deno_locator=lambda: str(binary)) == ("2026.8.19", True)
    assert module._youtube_readiness(deno_locator=lambda: str(tmp_path / "missing")) == ("2026.8.19", False)


@pytest.mark.asyncio
async def test_doctor_reports_managed_deno_version_instead_of_stale_pip_metadata(doctor):
    bot, backend, factory, guild = components()
    report = await doctor.collect_diagnostics(
        bot, backend, factory, tidal_authenticated=None, guild=guild,
        managed_deno_version="2.9.6",
    )
    assert "Deno: 2.9.6 (managed; executable ready)" in report
