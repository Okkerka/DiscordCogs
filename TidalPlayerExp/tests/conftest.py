"""
Shared fixtures that stub every optional dependency so tidalplayer.py can
be imported and instantiated in a plain pytest run without a live Discord
connection or third-party API credentials.

Design principle: minimal stubs — only the attributes/methods that the
current monolith actually accesses at import-time or inside __init__ /
cog_load are faked here.  Test files add further patching as needed.
"""
from __future__ import annotations

import asyncio
import sys
import types
from copy import deepcopy
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import discord as real_discord
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
        self.volume = _ConfigValue(100)

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

    _IDENTIFIER = 260904001

    def __init__(self) -> None:
        self.token_type = _ConfigValue(None)
        self.access_token = _ConfigValue(None)
        self.refresh_token = _ConfigValue(None)
        self.expiry_time = _ConfigValue(None)
        self._schema_version = _ConfigValue(3)
        self._guild_configs: dict[int, FakeGuildConfig] = {}
        self._global_lock = asyncio.Lock()

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

    def get_lock(self) -> asyncio.Lock:
        return self._global_lock

    async def all(self) -> dict[str, Any]:
        return {
            key: deepcopy(value._value)
            for key, value in vars(self).items()
            if isinstance(value, _ConfigValue)
        }

    async def set(self, data: dict[str, Any]) -> None:
        for key, value in data.items():
            existing = getattr(self, key, None)
            if isinstance(existing, _ConfigValue):
                existing._value = deepcopy(value)
            else:
                setattr(self, key, _ConfigValue(deepcopy(value)))

    async def clear_raw(self, *_args: Any) -> None:
        pass


# ---------------------------------------------------------------------------
# Fake discord / redbot stubs
# ---------------------------------------------------------------------------

def _make_discord_stub() -> types.ModuleType:
    discord = types.ModuleType("discord")
    discord.AudioSource = real_discord.AudioSource
    discord.Attachment = real_discord.Attachment
    discord.AllowedMentions = real_discord.AllowedMentions
    discord.oggparse = real_discord.oggparse
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
        def __init__(self, *, timeout: float | None = 180.0) -> None:
            self.timeout = timeout
            self.children: list = []
            self.stopped = False

        def add_item(self, item: Any) -> None:
            self.children.append(item)

        def stop(self) -> None:
            self.stopped = True

    discord.ui = types.ModuleType("discord.ui")
    discord.ui.View = _View
    discord.ui.LayoutView = _View
    discord.ui.Button = MagicMock()

    class _Modal:
        def __init_subclass__(cls, **_kwargs: Any) -> None:
            return super().__init_subclass__()

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self.children: list[Any] = []

        def add_item(self, item: Any) -> None:
            self.children.append(item)

    class _TextInput:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self.value = ""

    discord.ui.Modal = _Modal
    discord.ui.TextInput = _TextInput
    discord.TextStyle = MagicMock()
    discord.TextStyle.paragraph = 2

    class _Interaction:
        def __init__(self, user_id: int = 123) -> None:
            self.user = MagicMock()
            self.user.id = user_id
            self.response = AsyncMock()

    discord.Interaction = _Interaction
    discord.ButtonStyle = MagicMock()
    discord.ButtonStyle.primary = 1
    discord.ButtonStyle.danger = 4
    discord.HTTPException = type("HTTPException", (Exception,), {})
    discord.Forbidden = type("Forbidden", (discord.HTTPException,), {})
    discord.NotFound = type("NotFound", (discord.HTTPException,), {})
    discord.Guild = MagicMock
    discord.Member = type("Member", (), {})
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

        hybrid_group = group

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

        UserFeedbackCheckFailure = type("UserFeedbackCheckFailure", (Exception,), {})

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

        class CommandInvokeError(Exception):
            def __init__(self, original: Exception | None = None) -> None:
                self.original = original or RuntimeError("error")

        class BucketType:
            default = 0
            user = 1
            guild = 2
            channel = 3
            member = 4
            category = 5
            role = 6

        @staticmethod
        def dynamic_cooldown(*args: Any, **kwargs: Any):
            return lambda f: f

        @staticmethod
        def cooldown(*args: Any, **kwargs: Any):
            return lambda f: f

        class Cooldown:
            def __init__(self, rate: float, per: float) -> None:
                self.rate = rate
                self.per = per

        CommandOnCooldown = type("CommandOnCooldown", (Exception,), {"retry_after": 5.0})
        MissingPermissions = type("MissingPermissions", (Exception,), {})
        BotMissingPermissions = type("BotMissingPermissions", (Exception,), {})
        BadArgument = type("BadArgument", (Exception,), {})
        CheckFailure = type("CheckFailure", (Exception,), {})

    redbot.core.commands = _FakeCommands

    redbot.core.app_commands = types.ModuleType("redbot.core.app_commands")
    redbot.core.app_commands.AppCommandError = Exception
    redbot.core.app_commands.CommandInvokeError = Exception
    redbot.core.app_commands.UserFeedbackCheckFailure = Exception
    redbot.core.app_commands.CommandOnCooldown = type("AppCommandOnCooldown", (Exception,), {"retry_after": 5.0})
    redbot.core.app_commands.MissingPermissions = type("AppMissingPermissions", (Exception,), {})
    redbot.core.app_commands.BotMissingPermissions = type("AppBotMissingPermissions", (Exception,), {})
    redbot.core.app_commands.CheckFailure = type("AppCheckFailure", (Exception,), {})

    class _FakeRed:
        async def get_shared_api_tokens(self, service: str) -> dict:
            return {}

        async def set_shared_api_tokens(self, service: str, **tokens: str) -> None:
            pass

        async def remove_shared_api_tokens(self, service: str, *token_names: str) -> None:
            pass

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
    redbot.core.utils.views = types.ModuleType("redbot.core.utils.views")
    redbot.core.utils.views.SetApiView = MagicMock()

    return redbot


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
    spotipy.oauth2.SpotifyOAuth = MagicMock()
    spotipy.cache_handler = types.ModuleType("spotipy.cache_handler")
    spotipy.cache_handler.MemoryCacheHandler = MagicMock()
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
        "redbot.core.utils.views": redbot_stub.core.utils.views,
        "tidalapi": _make_tidalapi_stub(),
        "tidalapi.media": _make_tidalapi_stub().media,
        "spotipy": _make_spotipy_stub(),
        "spotipy.oauth2": _make_spotipy_stub().oauth2,
        "spotipy.cache_handler": _make_spotipy_stub().cache_handler,
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
    bot.set_shared_api_tokens = AsyncMock()
    bot.remove_shared_api_tokens = AsyncMock()
    bot.add_view = MagicMock()
    bot.add_cog = AsyncMock()
    bot.cog_disabled_in_guild = AsyncMock(return_value=False)
    bot.is_owner = AsyncMock(return_value=False)
    bot.get_cog = MagicMock(return_value=None)
    bot.get_guild = MagicMock(return_value=None)
    bot.guilds = []
    bot.user = types.SimpleNamespace(id=999)
    return bot


@pytest.fixture()
def cog(fake_bot, monkeypatch, tmp_path):
    """Return a freshly constructed TidalPlayerExp cog (no cog_load called)."""
    # Force removal of cached module so each test fixture gets a fresh import
    sys.modules.pop("TidalPlayerExp.tidalplayer", None)
    import importlib
    import os
    import sys as _sys
    # Add cog parent to path so relative import works
    cog_root = os.path.join(os.path.dirname(__file__), "..")
    if cog_root not in _sys.path:
        _sys.path.insert(0, cog_root)
    mod = importlib.import_module("TidalPlayerExp.tidalplayer")
    monkeypatch.setattr(mod, "cog_data_path", lambda cog: tmp_path / "cog-data", raising=False)
    return mod.TidalPlayerExp(fake_bot)


@pytest.fixture()
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


def make_entry(number=1, *, title=None, artist="Artist"):
    from TidalPlayerExp.playback.models import PlaybackEntry, SourceKind, SourceReference
    return PlaybackEntry(str(number), SourceReference(SourceKind.TIDAL, str(number)), None,
        {"title": title or f"Track {number}", "artist": artist, "album": None,
         "duration": 120, "quality": "LOSSLESS", "image": None, "share_url": None,
         "audio_resolution": None, "track_id": number}, 5)


class FakePlaybackSession:
    """Native boundary recorder: no Lavalink-shaped compatibility methods."""

    def __init__(self, current=None, queued=()):
        self.current = current
        self.entries = list(queued)
        self.paused = False
        self.enqueue = AsyncMock(side_effect=self._enqueue)
        self.skip = AsyncMock(return_value=True)
        self.stop = AsyncMock(side_effect=self._stop)
        self.clear_queue = AsyncMock(side_effect=self._clear_queue)
        self.set_paused = AsyncMock(return_value=True)

    def _enqueue(self, entry, **kwargs):
        self.entries.append(entry)
        return True

    async def _stop(self, *, clear_queue=True):
        self.current = None
        self.paused = False
        if clear_queue:
            await self._clear_queue()

    async def _clear_queue(self):
        count = len(self.entries)
        self.entries.clear()
        return count

    def snapshot(self):
        from TidalPlayerExp.playback.models import PlaybackSnapshot
        return PlaybackSnapshot(self.current, tuple(self.entries), self.paused, 22)


@pytest.fixture
def native_session(cog):
    session = FakePlaybackSession()
    cog.backend = types.SimpleNamespace(get=AsyncMock(return_value=session), connect=AsyncMock(return_value=session), close=AsyncMock(), close_guild=AsyncMock())
    return session
