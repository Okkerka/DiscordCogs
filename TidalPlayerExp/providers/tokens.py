"""Validated OAuth snapshots and persistence boundary.

The repository deliberately accepts only complete replacements.  That avoids
persisting a half-refreshed OAuth credential set when a provider refresh fails.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class TokenSnapshot:
    token_type: str
    access_token: str
    refresh_token: str
    expiry_time: int

    @classmethod
    def from_mapping(cls, data: dict[str, Any]) -> "TokenSnapshot | None":
        try:
            snapshot = cls(
                token_type=data["token_type"],
                access_token=data["access_token"],
                refresh_token=data["refresh_token"],
                expiry_time=int(data["expiry_time"]),
            )
        except (KeyError, TypeError, ValueError):
            return None
        return snapshot if snapshot.is_complete else None

    @property
    def is_complete(self) -> bool:
        return (
            isinstance(self.token_type, str)
            and isinstance(self.access_token, str)
            and isinstance(self.refresh_token, str)
            and bool(self.token_type.strip())
            and bool(self.access_token.strip())
            and bool(self.refresh_token.strip())
            and self.expiry_time > 0
        )

    def as_mapping(self) -> dict[str, Any]:
        return {
            "token_type": self.token_type,
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "expiry_time": self.expiry_time,
        }


class ConfigGroupContext(
    AbstractAsyncContextManager[dict[str, Any]], Awaitable[dict[str, Any]], Protocol
):
    """Red's global values support both reads and locked read-modify-write."""


class ConfigLike(Protocol):
    def all(self) -> ConfigGroupContext: ...


class TokenRepository:
    """Owns the stable persisted OAuth field shape in Red Config."""

    _FIELDS = ("token_type", "access_token", "refresh_token", "expiry_time")

    def __init__(self, config: ConfigLike) -> None:
        self._config = config
        self._lock = asyncio.Lock()

    async def load(self) -> TokenSnapshot | None:
        async with self._lock:
            return TokenSnapshot.from_mapping(await self._config.all())

    async def replace(self, snapshot: TokenSnapshot) -> None:
        if not snapshot.is_complete:
            raise ValueError("OAuth snapshot must contain all fields")
        async with self._lock:
            # Persist one credential generation in a single group write while
            # retaining schema metadata and other global settings.
            async with self._config.all() as data:
                data.update(snapshot.as_mapping())

    async def clear(self) -> None:
        async with self._lock:
            async with self._config.all() as data:
                data.update(dict.fromkeys(self._FIELDS))


class TokenService:
    """Tracks authentication generation independently from persisted credentials."""

    def __init__(self, repository: TokenRepository) -> None:
        self.repository = repository
        self.generation = 0

    async def restore(self) -> TokenSnapshot | None:
        return await self.repository.load()

    async def replace(self, snapshot: TokenSnapshot) -> None:
        await self.repository.replace(snapshot)
        self.generation += 1

    async def logout(self) -> None:
        await self.repository.clear()
        self.generation += 1
