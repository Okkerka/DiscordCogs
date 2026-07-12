"""
Phase-0 characterization tests: command registration and basic error paths.

Verifies that:
- all required command names are present on the cog class;
- _check_ready returns False (and sends an error embed) when prerequisites
  are not met, without raising;
- _format_duration produces the correct string for known inputs.

No live Discord connection or credentials required.
"""
from __future__ import annotations

import asyncio
import importlib
import inspect
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

MODULE_NAME = "TidalPlayer.tidalplayer"

REQUIRED_COMMAND_NAMES = {
    "tplay",
    "tsearch",
    "tnowplaying",
    "tqueue",
    "tstop",
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
    """All required command names must be present as methods on TidalPlayer."""

    def test_all_command_methods_exist(self, mod):
        cls = mod.TidalPlayer
        # Collect method names that look like they are commands
        method_names = {name for name, _ in inspect.getmembers(cls, predicate=inspect.isfunction)}
        for cmd_name in REQUIRED_COMMAND_NAMES:
            assert cmd_name in method_names, (
                f"Command '{cmd_name}' not found on TidalPlayer class — "
                "moving it to a sub-module must preserve this name"
            )

    def test_tplay_is_coroutine(self, mod):
        assert inspect.iscoroutinefunction(mod.TidalPlayer.tplay)

    def test_tsearch_is_coroutine(self, mod):
        assert inspect.iscoroutinefunction(mod.TidalPlayer.tsearch)

    def test_tnowplaying_is_coroutine(self, mod):
        assert inspect.iscoroutinefunction(mod.TidalPlayer.tnowplaying)

    def test_tqueue_is_coroutine(self, mod):
        assert inspect.iscoroutinefunction(mod.TidalPlayer.tqueue)

    def test_tstop_is_coroutine(self, mod):
        assert inspect.iscoroutinefunction(mod.TidalPlayer.tstop)

    def test_tfilter_is_coroutine(self, mod):
        assert inspect.iscoroutinefunction(mod.TidalPlayer.tfilter)

    def test_tinteractive_is_coroutine(self, mod):
        assert inspect.iscoroutinefunction(mod.TidalPlayer.tinteractive)


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
# _check_ready guard behaviour
# ---------------------------------------------------------------------------

class TestCheckReady:
    """_check_ready must send a precise error embed and return False, not raise."""

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
        result = await cog._check_ready(ctx)
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
            result = await cog._check_ready(ctx)
        assert result is False
        ctx.send.assert_called_once()

    @pytest.mark.asyncio
    async def test_returns_false_when_not_logged_in(self, cog):
        cog._initialized = True
        ctx = self._make_ctx()
        mod = sys.modules[MODULE_NAME]
        with (
            patch.object(mod, "TIDALAPI_AVAILABLE", True),
            patch.object(cog.tidal, "is_logged_in", AsyncMock(return_value=False)),
        ):
            result = await cog._check_ready(ctx)
        assert result is False
        ctx.send.assert_called_once()

    @pytest.mark.asyncio
    async def test_returns_false_when_lavalink_unavailable(self, cog):
        cog._initialized = True
        ctx = self._make_ctx()
        mod = sys.modules[MODULE_NAME]
        with (
            patch.object(mod, "TIDALAPI_AVAILABLE", True),
            patch.object(cog.tidal, "is_logged_in", AsyncMock(return_value=True)),
            patch.object(mod, "LAVALINK_AVAILABLE", False),
        ):
            result = await cog._check_ready(ctx)
        assert result is False
        ctx.send.assert_called_once()
