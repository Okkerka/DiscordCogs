"""
Phase-0 characterization tests: command registration and basic error paths.

Verifies that:
- all required command names are present on the cog class;
- check_ready returns False (and sends an error embed) when prerequisites
  are not met, without raising;
- _format_duration produces the correct string for known inputs.

No live Discord connection or credentials required.
"""
from __future__ import annotations

import importlib
import inspect
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

MODULE_NAME = "TidalPlayerExp.tidalplayer"

REQUIRED_COMMAND_NAMES = {
    "tplay",
    "tsearch",
    "tnowplaying",
    "tqueue",
    "stop_command",
    "tfilter",
    "tinteractive",
    "tpl",
    "tidalsetup",
}


@pytest.fixture(scope="module")
def mod():
    sys.modules.pop(MODULE_NAME, None)
    return importlib.import_module(MODULE_NAME)


# ---------------------------------------------------------------------------
# Command name registration
# ---------------------------------------------------------------------------

class TestCommandRegistration:
    """All required command names must be present as methods on TidalPlayerExp."""

    def test_all_command_methods_exist(self, mod):
        cls = mod.TidalPlayerExp
        # Collect method names that look like they are commands
        method_names = {name for name, _ in inspect.getmembers(cls, predicate=inspect.isfunction)}
        for cmd_name in REQUIRED_COMMAND_NAMES:
            assert cmd_name in method_names, (
                f"Command '{cmd_name}' not found on TidalPlayerExp class — "
                "moving it to a sub-module must preserve this name"
            )

    def test_tplay_is_coroutine(self, mod):
        assert inspect.iscoroutinefunction(mod.TidalPlayerExp.tplay)

    def test_tsearch_is_coroutine(self, mod):
        assert inspect.iscoroutinefunction(mod.TidalPlayerExp.tsearch)

    def test_tnowplaying_is_coroutine(self, mod):
        assert inspect.iscoroutinefunction(mod.TidalPlayerExp.tnowplaying)

    def test_tqueue_is_coroutine(self, mod):
        assert inspect.iscoroutinefunction(mod.TidalPlayerExp.tqueue)

    def test_stop_command_is_coroutine_and_tstop_is_removed(self, mod):
        assert inspect.iscoroutinefunction(mod.TidalPlayerExp.stop_command)
        assert not hasattr(mod.TidalPlayerExp, "tstop")

    def test_tfilter_is_coroutine(self, mod):
        assert inspect.iscoroutinefunction(mod.TidalPlayerExp.tfilter)

    def test_tinteractive_is_coroutine(self, mod):
        assert inspect.iscoroutinefunction(mod.TidalPlayerExp.tinteractive)


# ---------------------------------------------------------------------------
# _format_duration
# ---------------------------------------------------------------------------

class TestFormatDuration:
    @pytest.mark.parametrize("seconds,expected", [
        (0, "00:00"),
        (59, "00:59"),
        (60, "01:00"),
        (3599, "59:59"),
        (3600, "1:00:00"),
        (3661, "1:01:01"),
        (7322, "2:02:02"),
    ])
    def test_format(self, cog, seconds: int, expected: str):
        assert cog._format_duration(seconds) == expected


# ---------------------------------------------------------------------------
# check_ready guard behaviour
# ---------------------------------------------------------------------------

class TestCheckReady:
    """check_ready must send a precise error embed and return False, not raise."""

    def _make_ctx(self) -> MagicMock:
        ctx = MagicMock()
        ctx.guild = MagicMock()
        ctx.guild.id = 1
        ctx.send = AsyncMock()
        return ctx

    @pytest.mark.asyncio
    async def test_returns_false_when_not_initialized(self, cog):
        cog._initialized = False
        ctx = self._make_ctx()
        result = await cog.check_ready(ctx)
        assert result is False
        ctx.send.assert_called_once()
        embed = ctx.send.call_args.kwargs.get("embed") or ctx.send.call_args[1].get("embed")
        assert embed is not None
        # Must mention still initializing
        assert "initializing" in embed.description.lower() or "still" in embed.description.lower()

    @pytest.mark.asyncio
    async def test_returns_false_when_tidalapi_unavailable(self, cog):
        cog._initialized = True
        ctx = self._make_ctx()
        with patch.object(
            sys.modules[MODULE_NAME], "TIDALAPI_AVAILABLE", False
        ):
            result = await cog.check_ready(ctx)
        assert result is False
        ctx.send.assert_called_once()

    @pytest.mark.asyncio
    async def test_returns_false_when_not_logged_in(self, cog):
        cog._initialized = True
        ctx = self._make_ctx()
        mod = sys.modules[MODULE_NAME]
        with (
            patch.object(mod, "TIDALAPI_AVAILABLE", True),
            patch.object(type(cog.tidal), "is_logged_in", AsyncMock(return_value=False)),
        ):
            result = await cog.check_ready(ctx)
        assert result is False
        ctx.send.assert_called_once()

    @pytest.mark.asyncio
    async def test_ready_does_not_require_a_voice_connection(self, cog):
        cog._initialized = True
        ctx = self._make_ctx()
        mod = sys.modules[MODULE_NAME]
        with (
            patch.object(mod, "TIDALAPI_AVAILABLE", True),
            patch.object(type(cog.tidal), "is_logged_in", AsyncMock(return_value=True)),
        ):
            result = await cog.check_ready(ctx)
        assert result is True
        ctx.send.assert_not_called()


@pytest.mark.asyncio
async def test_tplay_defers_before_readiness_checks(cog) -> None:
    cog._initialized = True
    ctx = SimpleNamespace(defer=AsyncMock(), interaction=SimpleNamespace())

    async def check_ready(_self, _ctx):
        ctx.defer.assert_awaited_once_with()
        return False

    with patch.object(type(cog), "check_ready", new=check_ready):
        await cog.tplay(ctx, query="track")


@pytest.mark.asyncio
async def test_tidalsetup_spotify_opens_red_secure_token_view(cog) -> None:
    ctx = SimpleNamespace(send=AsyncMock())
    view = SimpleNamespace()
    view_factory = MagicMock(return_value=view)
    module = importlib.import_module(cog.__class__.__module__)

    with patch.object(module, "SetApiView", view_factory, create=True):
        await cog.tidalsetup_spotify(ctx)

    view_factory.assert_called_once_with(
        default_service="spotify",
        default_keys={"client_id": "", "client_secret": ""},
    )
    assert ctx.send.await_args.kwargs["view"] is view


@pytest.mark.asyncio
async def test_tidalsetup_youtube_opens_red_secure_token_view(cog) -> None:
    ctx = SimpleNamespace(send=AsyncMock())
    view = SimpleNamespace()
    view_factory = MagicMock(return_value=view)
    module = importlib.import_module(cog.__class__.__module__)

    with patch.object(module, "SetApiView", view_factory, create=True):
        await cog.tidalsetup_youtube(ctx)

    view_factory.assert_called_once_with(
        default_service="youtube",
        default_keys={"api_key": ""},
    )
    assert ctx.send.await_args.kwargs["view"] is view


@pytest.mark.asyncio
async def test_queue_uses_full_snapshot_in_message_bound_queue_view(cog, native_session) -> None:
    from TidalPlayerExp.tests.conftest import make_entry
    current_mod = importlib.import_module(cog.__class__.__module__)
    queue = [
        make_entry(index)
        for index in range(1, current_mod.MAX_ITEMS + 8)
    ]
    native_session.entries = queue
    message = SimpleNamespace()
    ctx = SimpleNamespace(guild=SimpleNamespace(id=1), defer=AsyncMock(), send=AsyncMock(return_value=message))
    created_views = []

    class RecordingQueueView:
        def __init__(self, view_cog, guild_id, snapshot):
            self.cog = view_cog
            self.guild_id = guild_id
            self.snapshot = snapshot
            self.message = None
            created_views.append(self)

        def stop(self):
            pass

    with (
        patch.object(current_mod, "QueueView", RecordingQueueView),
        patch.object(current_mod, "SimpleMenu") as simple_menu,
    ):
        await cog.tqueue(ctx)

    assert len(created_views) == 1
    view = created_views[0]
    assert view.cog is cog
    assert view.guild_id == 1
    assert view.snapshot.queued == tuple(queue)
    assert len(view.snapshot.queued) > current_mod.MAX_ITEMS
    assert view.message is message
    simple_menu.assert_not_called()
