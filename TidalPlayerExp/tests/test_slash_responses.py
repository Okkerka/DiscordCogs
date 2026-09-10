"""Hybrid commands must finish their interaction, not only post in the channel."""

from __future__ import annotations

import asyncio
import importlib
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from TidalPlayerExp.playback.models import SourceKind
from TidalPlayerExp.providers.public_audio import PublicAudioMetadata
from TidalPlayerExp.tests.conftest import make_entry


@pytest.mark.asyncio
@pytest.mark.parametrize("slash", [True, False])
async def test_first_track_admission_completes_slash_response_only(cog, native_session, monkeypatch, slash):
    module = importlib.import_module(cog.__class__.__module__)
    monkeypatch.setattr(module, "QUEUED_EMBED_DELETE_DELAY", 0)
    queued = make_entry()
    ctx = SimpleNamespace(
        guild=SimpleNamespace(id=1), channel=SimpleNamespace(send=AsyncMock()),
        interaction=object() if slash else None,
        send=AsyncMock(return_value=SimpleNamespace(delete=AsyncMock())),
    )
    assert await cog._admit_entry(ctx, native_session, queued)
    if slash:
        ctx.send.assert_awaited_once()
        assert ctx.send.await_args.kwargs["embed"].title == "Song added to the queue"
        await asyncio.gather(*cog._tasks)
        ctx.send.return_value.delete.assert_awaited_once()
    else:
        ctx.send.assert_not_awaited()
    ctx.channel.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_soundcloud_tplay_completes_deferred_interaction(cog, native_session, monkeypatch):
    module = importlib.import_module(cog.__class__.__module__)
    monkeypatch.setattr(module, "QUEUED_EMBED_DELETE_DELAY", 0)
    cog._initialized = True
    cog._prepare_playback_session = AsyncMock(return_value=native_session)
    queued = make_entry()
    reference = module.SourceReference(SourceKind.SOUNDCLOUD, "https://soundcloud.com/artist/song")
    cog.public_audio_resolver = SimpleNamespace(
        fetch_metadata=AsyncMock(return_value=PublicAudioMetadata(reference, queued.meta)),
    )
    ctx = SimpleNamespace(
        guild=SimpleNamespace(id=1), channel=SimpleNamespace(send=AsyncMock()),
        author=SimpleNamespace(id=42), interaction=object(),
        defer=AsyncMock(), send=AsyncMock(return_value=SimpleNamespace(delete=AsyncMock())),
    )
    await cog.tplay(ctx, query=reference.identifier)
    ctx.defer.assert_awaited_once()
    native_session.enqueue.assert_awaited_once()
    ctx.send.assert_awaited_once()
    await asyncio.gather(*cog._tasks)


@pytest.mark.asyncio
async def test_slash_nowplaying_sends_panel_through_command_response(cog, native_session):
    current = make_entry()
    native_session.current = current
    panel = SimpleNamespace(stop=Mock())
    cog._controller_view = AsyncMock(return_value=panel)
    ctx = SimpleNamespace(
        guild=SimpleNamespace(id=1), channel=SimpleNamespace(send=AsyncMock()),
        interaction=object(), defer=AsyncMock(), send=AsyncMock(),
    )
    await cog.tnowplaying(ctx)
    ctx.defer.assert_awaited_once()
    from discord import AllowedMentions
    ctx.send.assert_awaited_once()
    assert ctx.send.await_args.kwargs["view"] is panel
    assert ctx.send.await_args.kwargs["allowed_mentions"].to_dict() == AllowedMentions.none().to_dict()
    ctx.channel.send.assert_not_awaited()
    assert cog._controller_messages[1] is ctx.send.return_value


@pytest.mark.asyncio
async def test_slash_nowplaying_reports_track_ending_during_refresh(cog, native_session):
    native_session.current = make_entry()
    cog.backend.get.side_effect = [native_session, None]
    ctx = SimpleNamespace(
        guild=SimpleNamespace(id=1), channel=SimpleNamespace(send=AsyncMock()),
        interaction=object(), defer=AsyncMock(), send=AsyncMock(),
    )
    await cog.tnowplaying(ctx)
    ctx.defer.assert_awaited_once()
    ctx.send.assert_awaited_once()
    assert "Could not refresh" in ctx.send.await_args.kwargs["embed"].description


async def _real_red_response_contract() -> None:
    import discord
    from redbot.core.commands import Context

    ctx = object.__new__(Context)
    ctx.interaction = SimpleNamespace(
        is_expired=lambda: False,
        response=SimpleNamespace(is_done=lambda: True, defer=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()),
    )
    await ctx.defer()
    embed = discord.Embed(title="Song added to the queue")
    result = await ctx.send(embed=embed)
    ctx.interaction.response.defer.assert_awaited_once()
    ctx.interaction.followup.send.assert_awaited_once()
    assert ctx.interaction.followup.send.await_args.kwargs["embed"] is embed
    assert ctx.interaction.followup.send.await_args.kwargs["wait"] is True
    assert result is ctx.interaction.followup.send.return_value


def test_installed_red_completes_deferred_response_using_followup() -> None:
    result = subprocess.run(
        [
            sys.executable, "-c",
            (
                "import asyncio; from TidalPlayerExp.tests.test_slash_responses "
                "import _real_red_response_contract; asyncio.run(_real_red_response_contract())"
            ),
        ],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True, text=True, timeout=20, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
