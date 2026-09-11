"""Test suite for the new playback additions: clear alias, cooldowns, error handling, and position display."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from TidalPlayerExp.domain.models import TrackMeta
from TidalPlayerExp.playback.models import SourceKind, SourceReference
from TidalPlayerExp.ui.controller import PlayerControllerView, _track_info


def _meta(title: str = "Test Track", artist: str = "Test Artist", duration: int = 180) -> TrackMeta:
    return {
        "title": title,
        "artist": artist,
        "album": "Test Album",
        "duration": duration,
        "source": "TIDAL",
        "track_id": "123",
        "audio_resolution": "LOSSLESS",
    }


def test_playback_cooldown_returns_longer_duration_for_playlists() -> None:
    # Install the session's Red command stubs before importing command decorators.
    from TidalPlayerExp.commands import playback_cooldown

    ctx_normal = SimpleNamespace(kwargs={"query": "coldplay yellow"}, args=(), message=None, interaction=None)
    cd_normal = playback_cooldown(ctx_normal)
    assert cd_normal.per == 3.5

    ctx_playlist = SimpleNamespace(kwargs={"query": "https://tidal.com/playlist/abc"}, args=(), message=None, interaction=None)
    cd_playlist = playback_cooldown(ctx_playlist)
    assert cd_playlist.per == 10.0

    ctx_album = SimpleNamespace(kwargs={"query": "https://spotify.com/album/xyz"}, args=(), message=None, interaction=None)
    cd_album = playback_cooldown(ctx_album)
    assert cd_album.per == 10.0


def test_track_info_displays_position_and_remaining() -> None:
    meta = _meta(duration=200)
    info = _track_info(meta, autoplay_enabled=False, position=50.0)
    assert "**Duration:** 03:20" in info
    assert "**Position (snapshot):** 00:50 / 03:20 · **Remaining:** 02:30" in info


@pytest.mark.asyncio
async def test_controller_view_renders_position_and_remaining(cog) -> None:
    meta = _meta(duration=180)
    view = PlayerControllerView(cog, meta=meta, position=60.0)
    texts = [item.content for item in view.walk_children() if hasattr(item, "content")]
    combined = "\n".join(texts)
    assert "**Position (snapshot):** 01:00 / 03:00 · **Remaining:** 02:00" in combined


@pytest.mark.asyncio
async def test_clear_command_clears_queue_without_stopping_playback(cog, native_session) -> None:
    ctx = SimpleNamespace(
        guild=SimpleNamespace(id=1),
        author=SimpleNamespace(id=42, voice=SimpleNamespace(channel=SimpleNamespace(id=22))),
        interaction=None,
        defer=AsyncMock(),
        send=AsyncMock(),
    )
    cog._closing = False
    cog.backend.get = AsyncMock(return_value=native_session)
    cog._cancel_imports = Mock()
    cog._refresh_controller = AsyncMock()
    native_session.entries = [
        SimpleNamespace(primary=SourceReference(SourceKind.TIDAL, "1")),
        SimpleNamespace(primary=SourceReference(SourceKind.TIDAL, "2")),
    ]

    await cog.clear_command(ctx)
    native_session.clear_queue.assert_awaited_once()
    assert "Cleared 2 waiting track(s)" in ctx.send.call_args[0][0]
    cog._refresh_controller.assert_awaited_once_with(1, force=True)


@pytest.mark.asyncio
async def test_cog_command_error_handles_cooldown_and_permissions(cog) -> None:
    ctx = SimpleNamespace(command="play", send=AsyncMock())

    import redbot.core.commands as rc
    cd_err = rc.CommandOnCooldown(5.0)
    cd_err.retry_after = 4.2
    await cog.cog_command_error(ctx, cd_err)
    ctx.send.assert_awaited_once()
    assert "4.2s" in ctx.send.call_args.kwargs["embed"].description

    ctx.send.reset_mock()
    perm_err = rc.MissingPermissions(["manage_guild"])
    await cog.cog_command_error(ctx, perm_err)
    ctx.send.assert_awaited_once()
    assert "permission" in ctx.send.call_args.kwargs["embed"].description

    ctx.send.reset_mock()
    bad_arg = rc.BadArgument("Cannot parse duration")
    await cog.cog_command_error(ctx, bad_arg)
    ctx.send.assert_awaited_once()
    assert "Invalid argument" in ctx.send.call_args.kwargs["embed"].description


@pytest.mark.asyncio
async def test_cog_command_error_handles_value_error_safely(cog) -> None:
    ctx = SimpleNamespace(command="playfile", send=AsyncMock())
    import redbot.core.commands as rc
    err = rc.CommandInvokeError(ValueError("Audio file exceeds 50 MiB"))
    await cog.cog_command_error(ctx, err)
    ctx.send.assert_awaited_once()
    assert "unexpected error" in ctx.send.call_args.kwargs["embed"].description.lower()
    assert "Audio file exceeds 50 MiB" not in ctx.send.call_args.kwargs["embed"].description
