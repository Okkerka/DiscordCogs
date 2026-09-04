"""Isolation and identity checks for the experimental cog package."""
from __future__ import annotations

import ast
import importlib
import inspect
import json
from pathlib import Path

import pytest

PACKAGE_ROOT = Path(__file__).parents[1]
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


def test_manifest_uses_experimental_identity() -> None:
    manifest = json.loads((PACKAGE_ROOT / "info.json").read_text(encoding="utf-8"))
    assert manifest["name"] == "TidalPlayerExp"


@pytest.mark.asyncio
async def test_runtime_class_and_setup_use_experimental_identity(fake_bot) -> None:
    module = importlib.import_module("TidalPlayerExp.tidalplayer")
    assert module.TidalPlayerExp.__name__ == "TidalPlayerExp"

    await module.setup(fake_bot)

    added = fake_bot.add_cog.await_args.args[0]
    assert isinstance(added, module.TidalPlayerExp)


def test_config_identifier_is_isolated() -> None:
    schema = importlib.import_module("TidalPlayerExp.config_schema")
    assert schema.COG_IDENTIFIER == 260904001
    assert schema.COG_IDENTIFIER != 160819386


def test_copied_tests_do_not_import_legacy_package() -> None:
    legacy_package = "Tidal" + "Player"
    legacy_module = f"{legacy_package}.tidalplayer"
    violations: list[str] = []

    for path in (PACKAGE_ROOT / "tests").glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                imported = (
                    [alias.name for alias in node.names]
                    if isinstance(node, ast.Import)
                    else [node.module or ""]
                )
                if any(name == legacy_package or name.startswith(f"{legacy_package}.") for name in imported):
                    violations.append(f"{path}: import")
            elif isinstance(node, ast.Constant) and node.value == legacy_module:
                violations.append(f"{path}: module target")

    assert not violations, "legacy imports remain: " + ", ".join(violations)


def test_public_command_callbacks_are_preserved() -> None:
    module = importlib.import_module("TidalPlayerExp.tidalplayer")
    methods = {
        name for name, _ in inspect.getmembers(module.TidalPlayerExp, predicate=inspect.isfunction)
    }
    assert REQUIRED_COMMAND_NAMES <= methods
