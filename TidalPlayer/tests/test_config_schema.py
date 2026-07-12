"""
Phase-0 characterization tests: Config schema.

Captures the exact identifier, global defaults, guild defaults, and
migration behaviour as they exist today.  Any future refactor that silently
changes these values will fail here.
"""
from __future__ import annotations

import importlib
import sys

import pytest

MODULE_NAME = "TidalPlayer.tidalplayer"


@pytest.fixture()
def mod():
    sys.modules.pop(MODULE_NAME, None)
    return importlib.import_module(MODULE_NAME)


# --- Constants ---------------------------------------------------------------

def test_cog_identifier(mod):
    """Config identifier must stay 160819386 for Config continuity."""
    assert mod.COG_IDENTIFIER == 160819386


# --- Global schema defaults --------------------------------------------------

def test_global_defaults_token_fields(cog):
    """Global OAuth fields must default to None."""
    # register_global was called in __init__; verify via the FakeConfig values
    config = cog.config
    assert config.token_type._value is None
    assert config.access_token._value is None
    assert config.refresh_token._value is None
    assert config.expiry_time._value is None


def test_global_defaults_schema_version(cog):
    """_schema_version default must be 3."""
    assert cog.config._schema_version._value == 3


# --- Guild schema defaults ---------------------------------------------------

def test_guild_default_filter_remixes(cog):
    """filter_remixes must default to True."""
    from unittest.mock import MagicMock
    guild = MagicMock()
    guild.id = 999
    guild_cfg = cog.config.guild(guild)
    assert guild_cfg.filter_remixes._value is True


def test_guild_default_interactive_search(cog):
    """interactive_search must default to False."""
    from unittest.mock import MagicMock
    guild = MagicMock()
    guild.id = 998
    guild_cfg = cog.config.guild(guild)
    assert guild_cfg.interactive_search._value is False


# --- Schema version field presence ------------------------------------------

def test_oauth_field_names_present(mod):
    """OAuth field names referenced in the cog must match the stored names."""
    # These are the string literals used in tidalsetup_login and refresh_tokens.
    # They must never be renamed without a migration.
    expected_fields = {"token_type", "access_token", "refresh_token", "expiry_time"}
    cog_source = open(mod.__file__).read()
    for field in expected_fields:
        assert field in cog_source, (
            f"OAuth field '{field}' missing from tidalplayer.py source — "
            "rename without migration would silently lose credentials"
        )


# --- Migration ---------------------------------------------------------------

@pytest.mark.asyncio
async def test_migration_sets_schema_v3(cog):
    """_migrate_config must set _schema_version to 3 when called."""
    cog.config._schema_version._value = 1  # simulate older installation
    await cog._migrate_config()
    assert cog.config._schema_version._value == 3


@pytest.mark.asyncio
async def test_migration_is_idempotent(cog):
    """Running migration twice must not raise and must leave version at 3."""
    await cog._migrate_config()
    await cog._migrate_config()
    assert cog.config._schema_version._value == 3
