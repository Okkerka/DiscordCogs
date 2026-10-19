"""A refreshed panel must keep its controls dispatchable.

discord.py registers a view's controls by (type, custom id) under the
message they belong to, and removes them by the same key. Two panels on
one message therefore share their keys, and stopping the replaced view
unregisters the controls of the view that replaced it.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import discord as real_discord
import pytest


@pytest.fixture
def real_controller_ui(monkeypatch):
    """Load the controller view against discord.py's real view store."""
    monkeypatch.setitem(sys.modules, "discord", real_discord)
    monkeypatch.setitem(sys.modules, "discord.ui", real_discord.ui)
    path = Path(__file__).parents[1] / "ui" / "controller.py"
    spec = importlib.util.spec_from_file_location("TidalPlayerExp.ui._test_controller", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _store():
    from discord.ui.view import ViewStore

    return ViewStore(SimpleNamespace())


def _registered(store, message_id: int) -> set[str]:
    return {
        custom_id
        for (component_type, custom_id) in store._views.get(message_id, {})
    }


def test_a_refreshed_panel_keeps_its_controls_registered(real_controller_ui):
    store = _store()
    previous = real_controller_ui.PlayerControllerView(SimpleNamespace())
    store.add_view(previous, 42)
    current = real_controller_ui.PlayerControllerView(SimpleNamespace())
    store.add_view(current, 42)

    # The cog's rule: a replaced view that shares the message is not stopped.
    if getattr(previous, "_cache_key", None) != getattr(current, "_cache_key", None):
        previous.stop()

    registered = _registered(store, 42)
    assert "tidalplayer:v2:suggestions" in registered
    assert "tidalplayer:v2:skip" in registered
    assert "tidalplayer:v2:stop" in registered


def test_stopping_the_replaced_view_would_unregister_the_new_controls(real_controller_ui):
    """Documents why the replaced view must not be stopped in place."""
    store = _store()
    previous = real_controller_ui.PlayerControllerView(SimpleNamespace())
    store.add_view(previous, 42)
    current = real_controller_ui.PlayerControllerView(SimpleNamespace())
    store.add_view(current, 42)

    previous.stop()

    assert "tidalplayer:v2:suggestions" not in _registered(store, 42)
    assert "tidalplayer:v2:skip" not in _registered(store, 42)
