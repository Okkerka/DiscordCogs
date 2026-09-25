import asyncio
import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

from utilities.utilities import Utilities


class Value:
    def __init__(self, data):
        self.data = data

    def __call__(self):
        return self

    def __await__(self):
        async def read():
            return copy.deepcopy(self.data)

        return read().__await__()

    async def __aenter__(self):
        return self.data

    async def __aexit__(self, *args):
        return False


class Config:
    def __init__(self, users):
        self.users = users

    async def all_users(self):
        return copy.deepcopy(self.users)

    def user_from_id(self, uid):
        return SimpleNamespace(
            reminders=Value(self.users.setdefault(uid, {"reminders": {}})["reminders"])
        )


def setup(item=None):
    item = item or {
        "text": "test reminder",
        "due": 0,
        "attempts": 0,
        "state": "pending",
    }
    config = Config({1: {"reminders": {"abc": item}}})
    user = SimpleNamespace(send=AsyncMock())
    cog = object.__new__(Utilities)
    cog.config, cog._reminder_lock = config, asyncio.Lock()
    cog.bot = SimpleNamespace(get_user=lambda uid: user, fetch_user=AsyncMock())
    return cog, user, config


@pytest.mark.asyncio
async def test_overdue_reminder_delivers_after_restart_once():
    cog, user, config = setup()
    await cog._deliver_due_reminders()
    await cog._deliver_due_reminders()
    assert user.send.await_count == 1
    assert config.users[1]["reminders"] == {}


@pytest.mark.asyncio
async def test_cancelled_reminder_not_delivered():
    cog, user, config = setup()
    assert await cog._cancel_reminder(1, "abc")
    await cog._deliver_due_reminders()
    user.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_cannot_cancel_another_users_reminder():
    cog, user, config = setup()
    assert not await cog._cancel_reminder(2, "abc")
    assert "abc" in config.users[1]["reminders"]


@pytest.mark.asyncio
async def test_closed_dms_record_failure_without_repeated_sends():
    cog, user, config = setup()
    user.send.side_effect = discord.Forbidden(
        SimpleNamespace(status=403, reason="Forbidden"), "DM closed"
    )
    await cog._deliver_due_reminders()
    await cog._deliver_due_reminders()
    assert user.send.await_count == 1
    assert config.users[1]["reminders"]["abc"]["state"].startswith("failed")


@pytest.mark.asyncio
async def test_transient_failure_retries_later():
    cog, user, config = setup()
    user.send.side_effect = discord.HTTPException(
        SimpleNamespace(status=500, reason="Server error"), "error"
    )
    await cog._deliver_due_reminders()
    await cog._deliver_due_reminders()
    assert user.send.await_count == 1
    assert config.users[1]["reminders"]["abc"]["attempts"] == 1
    assert config.users[1]["reminders"]["abc"]["state"] == "pending"


@pytest.mark.asyncio
async def test_delivery_serializes_with_cancellation():
    cog, user, config = setup()
    started, release = asyncio.Event(), asyncio.Event()

    async def send(**kwargs):
        started.set()
        await release.wait()

    user.send.side_effect = send
    task = asyncio.create_task(cog._deliver_due_reminders())
    await started.wait()
    cancellation = asyncio.create_task(cog._cancel_reminder(1, "abc"))
    await asyncio.sleep(0)
    assert not cancellation.done()
    release.set()
    await task
    assert not await cancellation
