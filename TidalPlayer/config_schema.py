"""Stable Red Config schema for TidalPlayer.

Keep these values separate from runtime code: persisted Config data outlives
refactors, so changing them requires an explicit migration rather than a move.
"""

from typing import Final

COG_IDENTIFIER: Final = 160819386
SCHEMA_VERSION: Final = 3

GLOBAL_DEFAULTS: Final = {
    "token_type": None,
    "access_token": None,
    "refresh_token": None,
    "expiry_time": None,
    "_schema_version": SCHEMA_VERSION,
}

GUILD_DEFAULTS: Final = {
    "filter_remixes": True,
    "interactive_search": False,
    "autoplay_enabled": True,
}
