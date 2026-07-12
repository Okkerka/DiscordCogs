"""
Phase-0 smoke tests.

Verify that:
1. tidalplayer.py compiles without SyntaxError.
2. The module can be imported via importlib (all top-level code runs).
3. async def setup(bot) exists and is a coroutine function.
4. TidalPlayer class is exported.

No Discord connection, no credentials, no network I/O.
"""
from __future__ import annotations

import asyncio
import importlib
import inspect
import os
import py_compile
import sys

import pytest

COG_DIR = os.path.join(os.path.dirname(__file__), "..")
MODULE_PATH = os.path.join(COG_DIR, "tidalplayer.py")
MODULE_NAME = "TidalPlayer.tidalplayer"


def test_py_compile_tidalplayer():
    """tidalplayer.py must compile without SyntaxError."""
    py_compile.compile(MODULE_PATH, doraise=True)


def test_import_tidalplayer():
    """tidalplayer.py must import cleanly under the stub environment."""
    sys.modules.pop(MODULE_NAME, None)
    if COG_DIR not in sys.path:
        sys.path.insert(0, os.path.dirname(COG_DIR))
    mod = importlib.import_module(MODULE_NAME)
    assert mod is not None


def test_setup_is_coroutine():
    """async def setup(bot) must be a coroutine function."""
    sys.modules.pop(MODULE_NAME, None)
    mod = importlib.import_module(MODULE_NAME)
    assert hasattr(mod, "setup"), "Module must export 'setup'"
    assert inspect.iscoroutinefunction(mod.setup), "setup must be async"


def test_tidalplayer_class_exported():
    """TidalPlayer class must be importable from the module."""
    sys.modules.pop(MODULE_NAME, None)
    mod = importlib.import_module(MODULE_NAME)
    assert hasattr(mod, "TidalPlayer")
    assert inspect.isclass(mod.TidalPlayer)


@pytest.mark.asyncio
async def test_setup_calls_add_cog(fake_bot):
    """setup(bot) must call bot.add_cog exactly once."""
    sys.modules.pop(MODULE_NAME, None)
    mod = importlib.import_module(MODULE_NAME)
    fake_bot.add_cog.reset_mock()
    await mod.setup(fake_bot)
    fake_bot.add_cog.assert_called_once()
    added = fake_bot.add_cog.call_args[0][0]
    assert isinstance(added, mod.TidalPlayer)
