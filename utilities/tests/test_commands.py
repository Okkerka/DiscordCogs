from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from utilities.utilities import Utilities


@pytest.mark.asyncio
async def test_status_handles_nan_latency_before_first_heartbeat():
    ctx = SimpleNamespace(bot=SimpleNamespace(latency=float("nan")), send=AsyncMock())

    await Utilities.status.callback(object.__new__(Utilities), ctx)

    ctx.send.assert_awaited_once_with("Bot latency: `unknown`")


@pytest.mark.asyncio
async def test_dice_rejects_non_decimal_digit_expression():
    ctx = SimpleNamespace(send=AsyncMock())

    await Utilities.dice.callback(object.__new__(Utilities), ctx, expression="²")

    ctx.send.assert_awaited_once()
    assert "Invalid dice format" in ctx.send.await_args.args[0]


def test_slash_surface_and_thanos_exclusion():
    commands = {c.name: c for c in Utilities.__cog_commands__}
    for name in (
        "avatar",
        "choose",
        "remindme",
        "reminders",
        "quote",
        "timestamp",
        "membercount",
    ):
        assert name in commands
        assert commands[name].app_command is not None
    assert getattr(commands["thanos"], "app_command", None) is None
