"""Concrete Config repository that satisfies the TokenRepository ConfigLike Protocol.

The cog constructs a ConfigRepository around its registered global Config group
and injects it into TokenRepository.  Nothing outside this file touches Red
Config directly for OAuth fields.
"""

from __future__ import annotations

from typing import Any


class ConfigRepository:
    """Wrap a Red Config global group so it satisfies the ConfigLike Protocol.

    Red Config field accessors are stored as attributes on the group object.
    ``all()`` is the standard Config method that returns all registered values
    as a plain dict, which is exactly what TokenRepository.load() needs.
    """

    def __init__(self, config_group: Any) -> None:
        # config_group is the object returned by Config.get_conf(); attribute
        # access on it yields individual field accessors (token_type, etc.).
        self._group = config_group

    # Delegate attribute access so TokenRepository can call
    # self._config.token_type.set(value) transparently.
    def __getattr__(self, name: str) -> Any:
        return getattr(self._group, name)

    async def all(self) -> dict[str, Any]:
        return await self._group.all()
