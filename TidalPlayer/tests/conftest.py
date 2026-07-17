"""
Shared fixtures that stub every optional dependency so tidalplayer.py can
be imported and instantiated in a plain pytest run without a live Discord
connection, Redis, Lavalink node, or third-party API credentials.

Design principle: minimal stubs — only the attributes/methods that the
current monolith actually accesses at import-time or inside __init__ /
cog_load are faked here.  Test files add further patching as needed.
"""
from __future__ import annotations

import asyncio
import sys
import types
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest


# ---------------------------------------------------------------------------
# Lightweight in-memory Config stand-in
# ---------------------------------------------------------------------------

class _ConfigValue:
    """Mimics a single Config accessor returned by attribute access."""

    def __init__(self, default: Any = None) -> None:
        self._value = default

    async def __call__(self) -> Any:  # read:  await config.access_token()
        return self._value

    async def set(self, value: Any) -> None:
        self._value = value

    def __await__(self):
        return self.__call__().__await__()


class FakeGuildConfig:
    def __init__(self) -> None:
        self._filter_remixes = _ConfigValue(True)
        self._interactive_search = _ConfigValue(False)
        self._autoplay_enabled = _ConfigValue(False)

    @property
    def filter_remixes(self) -> _ConfigValue:
        return self._filter_remixes

    @property
    def interactive_search(self) -> _ConfigValue:
        return self._interactive_search

    @property
    def autoplay_enabled(self) -> _ConfigValue:
        return self._autoplay_enabled


class FakeConfig:
    """Minimal Config stand-in."""

    _IDENTIFIER = 160819386

    def __init__(self) -> None:
        self.token_type = _ConfigValue(None)
        self.access_token = _ConfigValue(None)
        self.refresh_token = _ConfigValue(None)
        self.expiry_time = _ConfigValue(None)
        self._schema_version = _ConfigValue(3)
        self._guild_configs: dict[int, FakeGuildConfig] = {}

    # Config.get_conf factory
    @classmethod
    def get_conf(cls, cog: Any, identifier: int, force_registration: bool = False) -> "FakeConfig":
        return cls()

    def register_global(self, **defaults: Any) -> None:
        for key, value in defaults.items():
            if not hasattr(self, key):
                setattr(self, key, _ConfigValue(value))
            else:
                existing: _ConfigValue = getattr(self, key)
                if existing._value is None:
                    existing._value = value

    def register_guild(self, **defaults: Any) -> None:
        # Store defaults for later FakeGuildConfig construction
        self._guild_defaults = defaults

    def guild(self, guild: Any) -> FakeGuildConfig:
        gid = getattr(guild, "id", guild)
        if gid not in self._guild_configs:
            self._guild_configs[gid] = FakeGuildConfig()
        return self._guild_configs[gid]

    def guild_from_id(self, guild_id: int) -> FakeGuildConfig:
        return self.guild(guild_id)

    async def all(self) -> dict[str, Any]:
        return {
            "token_type": await self.token_type(),
            "access_token": await self.access_token(),
            "refresh_token": await self.refresh_token(),
            "expiry_time": await self.expiry_time(),
            "_schema_version": await self._schema_version(),
        }

    async def clear_raw(self, *_args: Any) -> None:
        pass


# ---------------------------------------------------------------------------
# Fake discord / redbot stubs
# ---------------------------------------------------------------------------

def _make_discord_stub() -> types.ModuleType:
    discord = types.ModuleType("discord")
    discord.Color = MagicMock()
    discord.Color.blue = MagicMock(return_value="blue")
    discord.Color.green = MagicMock(return_value="green")
    discord.Color.red = MagicMock(return_value="red")
    discord.Color.blurple = MagicMock(return_value="blurple")
    discord.Color.teal = MagicMock(return_value="teal")
    discord.Color.purple = MagicMock(return_value="purple")

    class _Embed:
        def __init__(self, *, title: str = "", description: str = "", color: Any = None) -> None:
            self.title = title
            self.description = description
            self.color = color
            self.fields: list[dict] = []
            self._thumbnail: str | None = None
            self._footer: str | None = None
            self._image: str | None = None

        def add_field(self, *, name: str, value: str, inline: bool = True) -> None:
            self.fields.append({"name": name, "value": value, "inline": inline})

        def set_thumbnail(self, *, url: str) -> None:
            self._thumbnail = url

        def set_footer(self, *, text: str) -> None:
            self._footer = text

        def set_image(self, *, url: str) -> None:
            self._image = url

    discord.Embed = _Embed

    class _View:
        def __init__(self, *, timeout: float = 180.0) -> None:
            self.timeout = timeout
            self.children: list = []

        def add_item(self, item: Any) -> None:
            self.children.append(item)

        def stop(self) -> None:
            pass

    discord.ui = types.ModuleType("discord.ui")
    discord.ui.View = _View
    discord.ui.LayoutView = _View
    discord.ui.Button = MagicMock()

    class _Interaction:
        def __init__(self, user_id: int = 123) -> None:
            self.user = MagicMock()
            self.user.id = user_id
            self.response = AsyncMock()

    discord.Interaction = _Interaction
    discord.ButtonStyle = MagicMock()
    discord.ButtonStyle.primary = 1
    discord.ButtonStyle.danger = 4
    discord.Guild = MagicMock
    discord.User = MagicMock
    return discord


def _make_redbot_stub(fake_config: FakeConfig) -> types.ModuleType:
    redbot = types.ModuleType("redbot")
    redbot.core = types.ModuleType("redbot.core")

    # Patch Config factory to return our fake
    redbot.core.Config = FakeConfig

    class _FakeCommands:
        @staticmethod
        def hybrid_command(*args: Any, **kwargs: Any):
            def decorator(f: Any) -> Any:
                f.name = kwargs.get("name", f.__name__)
                f.qualified_name = f.name
                return f
            return decorator

        @staticmethod
        def group(*args: Any, **kwargs: Any):
            def decorator(f: Any) -> Any:
                f.name = kwargs.get("name", f.__name__)
                f.qualified_name = f.name

                def command(*_args: Any, **_kwargs: Any):
                    return lambda child: child

                f.command = command
                return f
            return decorator

        @staticmethod
        def is_owner():
            return lambda f: f

        @staticmethod
        def guild_only():
            return lambda f: f

        @staticmethod
        def admin_or_permissions(**_permissions: Any):
            return lambda f: f

        @staticmethod
        def check(predicate: Any):
            return lambda f: f

        class Cog:
            @staticmethod
            def listener(*args: Any, **kwargs: Any):
                return lambda f: f

            async def cog_load(self) -> None:
                pass

            def cog_unload(self) -> None:
                pass

        class Context:
            def __init__(self, guild_id: int = 1) -> None:
                self.guild = MagicMock()
                self.guild.id = guild_id
                self.author = MagicMock()
                self.author.id = 42
                self.author.voice = None
                self.send = AsyncMock()
                self.command = MagicMock()

        @staticmethod
        def CommandInvokeError(e: Exception) -> Exception:
            return e

    redbot.core.commands = _FakeCommands

    redbot.core.app_commands = types.ModuleType("redbot.core.app_commands")
    redbot.core.app_commands.AppCommandError = Exception
    redbot.core.app_commands.CommandInvokeError = Exception
    redbot.core.app_commands.UserFeedbackCheckFailure = Exception

    class _FakeRed:
        async def get_shared_api_tokens(self, service: str) -> dict:
            return {}

        async def add_cog(self, cog: Any) -> None:
            pass

        async def cog_disabled_in_guild(self, cog: Any, guild: Any) -> bool:
            return False

        async def is_owner(self, user: Any) -> bool:
            return False

    redbot.core.bot = types.ModuleType("redbot.core.bot")
    redbot.core.bot.Red = _FakeRed

    redbot.core.utils = types.ModuleType("redbot.core.utils")
    redbot.core.utils.menus = types.ModuleType("redbot.core.utils.menus")
    redbot.core.utils.menus.SimpleMenu = MagicMock()

    return redbot


def _make_lavalink_stub() -> types.ModuleType:
    lavalink = types.ModuleType("lavalink")
    lavalink.get_player = MagicMock(side_effect=Exception("no player"))
    lavalink.connect = AsyncMock()
    return lavalink


def _make_tidalapi_stub() -> types.ModuleType:
    tidalapi = types.ModuleType("tidalapi")

    class _Session:
        token_type: str | None = None
        access_token: str | None = None
        refresh_token: str | None = None
        expiry_time: Any = None

        def check_login(self) -> bool:
            return False

        def load_oauth_session(self, *args: Any) -> None:
            pass

        def search(self, query: str, **kwargs: Any) -> dict:
            return {"tracks": []}

        def login_oauth(self) -> tuple:
            url = MagicMock()
            url.verification_uri_complete = "https://tidal.com/activate"
            url.expires_in = 300
            future = MagicMock()
            future.result = MagicMock(return_value=None)
            return url, future

    tidalapi.Session = _Session

    tidalapi.media = types.ModuleType("tidalapi.media")

    class _Track:
        pass

    tidalapi.media.Track = _Track
    return tidalapi


def _make_spotipy_stub() -> types.ModuleType:
    spotipy = types.ModuleType("spotipy")
    spotipy.Spotify = MagicMock()
    spotipy.oauth2 = types.ModuleType("spotipy.oauth2")
    spotipy.oauth2.SpotifyClientCredentials = MagicMock()
    return spotipy


def _make_googleapi_stub() -> types.ModuleType:
    google = types.ModuleType("googleapiclient")
    google.discovery = types.ModuleType("googleapiclient.discovery")
    google.discovery.build = MagicMock()
    return google


# ---------------------------------------------------------------------------
# Session-scoped fixture: patch sys.modules before the cog is imported
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session", autouse=True)
def _patch_dependencies():
    """Install all stubs into sys.modules for the whole test session."""
    fake_config = FakeConfig()
    discord_stub = _make_discord_stub()
    redbot_stub = _make_redbot_stub(fake_config)

    patches = {
        "discord": discord_stub,
        "discord.ui": discord_stub.ui,
        "redbot": redbot_stub,
        "redbot.core": redbot_stub.core,
        "redbot.core.commands": redbot_stub.core.commands,
        "redbot.core.app_commands": redbot_stub.core.app_commands,
        "redbot.core.bot": redbot_stub.core.bot,
        "redbot.core.utils": redbot_stub.core.utils,
        "redbot.core.utils.menus": redbot_stub.core.utils.menus,
        "lavalink": _make_lavalink_stub(),
        "tidalapi": _make_tidalapi_stub(),
        "tidalapi.media": _make_tidalapi_stub().media,
        "spotipy": _make_spotipy_stub(),
        "spotipy.oauth2": _make_spotipy_stub().oauth2,
        "googleapiclient": _make_googleapi_stub(),
        "googleapiclient.discovery": _make_googleapi_stub().discovery,
    }
    originals = {}
    for name, stub in patches.items():
        originals[name] = sys.modules.get(name)
        sys.modules[name] = stub

    yield

    for name, original in originals.items():
        if original is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = original


@pytest.fixture()
def fake_bot():
    from redbot.core.bot import Red  # noqa: PLC0415 – resolved to stub
    bot = Red()
    bot.get_shared_api_tokens = AsyncMock(return_value={})
    bot.add_cog = AsyncMock()
    bot.cog_disabled_in_guild = AsyncMock(return_value=False)
    bot.is_owner = AsyncMock(return_value=False)
    return bot


@pytest.fixture()
def cog(fake_bot):
    """Return a freshly constructed TidalPlayer cog (no cog_load called)."""
    # Force removal of cached module so each test fixture gets a fresh import
    sys.modules.pop("TidalPlayer.tidalplayer", None)
    import importlib
    import os
    import sys as _sys
    # Add cog parent to path so relative import works
    cog_root = os.path.join(os.path.dirname(__file__), "..")
    if cog_root not in _sys.path:
        _sys.path.insert(0, cog_root)
    mod = importlib.import_module("TidalPlayer.tidalplayer")
    return mod.TidalPlayer(fake_bot)


@pytest.fixture()
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()
