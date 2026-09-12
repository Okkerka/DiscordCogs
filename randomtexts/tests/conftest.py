import asyncio
import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from randomtexts.randomchats import RandomText


class Value:
    def __init__(self, data, key):
        self.data, self.key = data, key

    def __call__(self):
        return self

    def __await__(self):
        async def read():
            await asyncio.sleep(0)
            return copy.deepcopy(self.data[self.key])

        return read().__await__()

    async def set(self, value):
        await asyncio.sleep(0)
        self.data[self.key] = copy.deepcopy(value)


class Group:
    def __init__(self, data):
        self.data = data

    async def all(self):
        await asyncio.sleep(0)
        return copy.deepcopy(self.data)

    async def set(self, value):
        self.data.clear()
        self.data.update(copy.deepcopy(value))

    def __getattr__(self, key):
        return Value(self.data, key)


class MemoryConfig:
    def __init__(self):
        self.defaults, self.guilds = {}, {}

    def register_guild(self, **defaults):
        self.defaults.update(defaults)

    def guild_from_id(self, guild_id):
        return Group(self.guilds.setdefault(guild_id, copy.deepcopy(self.defaults)))

    def guild(self, guild):
        return self.guild_from_id(guild.id)

    async def all_guilds(self):
        return copy.deepcopy(self.guilds)


@pytest_asyncio.fixture
async def random_cog(monkeypatch):
    config = MemoryConfig()
    monkeypatch.setattr(
        "randomtexts.randomchats.Config.get_conf", lambda *a, **kw: config
    )
    bot = SimpleNamespace(
        get_context=AsyncMock(return_value=SimpleNamespace(valid=False)),
        is_owner=AsyncMock(return_value=False),
        is_admin=AsyncMock(return_value=False),
        cog_disabled_in_guild=AsyncMock(return_value=False),
        allowed_by_whitelist_blacklist=AsyncMock(return_value=True),
    )
    cog = RandomText(bot)
    yield cog
    await cog.cog_unload()


@pytest.fixture
def message():
    guild = SimpleNamespace(id=1, me=SimpleNamespace(id=9))
    author = SimpleNamespace(id=2, bot=False)
    channel = SimpleNamespace(
        id=10,
        guild=guild,
        send=AsyncMock(),
        permissions_for=lambda _: SimpleNamespace(send_messages=True, embed_links=True),
    )
    return SimpleNamespace(guild=guild, author=author, channel=channel, content="hello")
