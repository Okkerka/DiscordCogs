"""Contract tests for TokenSnapshot, TokenRepository, and TokenService."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock

import pytest

from TidalPlayer.providers.tokens import TokenRepository, TokenService, TokenSnapshot


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_COMPLETE = {
    "token_type": "Bearer",
    "access_token": "acc",
    "refresh_token": "ref",
    "expiry_time": 9_999_999_999,
}


class _FakeField:
    def __init__(self, value: Any = None) -> None:
        self._value = value

    async def __call__(self) -> Any:
        return self._value

    async def set(self, value: Any) -> None:
        self._value = value


class _FakeConfigGroup:
    def __init__(self, data: dict[str, Any] | None = None) -> None:
        defaults = data or {}
        self.token_type = _FakeField(defaults.get("token_type"))
        self.access_token = _FakeField(defaults.get("access_token"))
        self.refresh_token = _FakeField(defaults.get("refresh_token"))
        self.expiry_time = _FakeField(defaults.get("expiry_time"))

    async def all(self) -> dict[str, Any]:
        return {
            "token_type": await self.token_type(),
            "access_token": await self.access_token(),
            "refresh_token": await self.refresh_token(),
            "expiry_time": await self.expiry_time(),
        }


# ---------------------------------------------------------------------------
# TokenSnapshot
# ---------------------------------------------------------------------------

class TestTokenSnapshot:
    def test_from_complete_mapping_succeeds(self) -> None:
        snap = TokenSnapshot.from_mapping(_COMPLETE)
        assert snap is not None
        assert snap.token_type == "Bearer"
        assert snap.access_token == "acc"
        assert snap.refresh_token == "ref"
        assert snap.expiry_time == 9_999_999_999

    @pytest.mark.parametrize("missing_key", ["token_type", "access_token", "refresh_token", "expiry_time"])
    def test_from_mapping_missing_field_returns_none(self, missing_key: str) -> None:
        data = {k: v for k, v in _COMPLETE.items() if k != missing_key}
        assert TokenSnapshot.from_mapping(data) is None

    def test_from_mapping_empty_string_access_token_returns_none(self) -> None:
        data = {**_COMPLETE, "access_token": "   "}
        assert TokenSnapshot.from_mapping(data) is None

    def test_from_mapping_zero_expiry_returns_none(self) -> None:
        data = {**_COMPLETE, "expiry_time": 0}
        assert TokenSnapshot.from_mapping(data) is None

    def test_from_mapping_negative_expiry_returns_none(self) -> None:
        data = {**_COMPLETE, "expiry_time": -1}
        assert TokenSnapshot.from_mapping(data) is None

    def test_from_mapping_non_int_expiry_coerced(self) -> None:
        data = {**_COMPLETE, "expiry_time": "9999999999"}
        snap = TokenSnapshot.from_mapping(data)
        assert snap is not None
        assert snap.expiry_time == 9_999_999_999

    def test_from_mapping_bad_expiry_type_returns_none(self) -> None:
        data = {**_COMPLETE, "expiry_time": "not-a-number"}
        assert TokenSnapshot.from_mapping(data) is None

    def test_as_mapping_roundtrip(self) -> None:
        snap = TokenSnapshot.from_mapping(_COMPLETE)
        assert snap is not None
        assert snap.as_mapping() == _COMPLETE

    def test_snapshot_is_immutable(self) -> None:
        snap = TokenSnapshot.from_mapping(_COMPLETE)
        assert snap is not None
        with pytest.raises((AttributeError, TypeError)):
            snap.access_token = "mutated"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# TokenRepository
# ---------------------------------------------------------------------------

class TestTokenRepository:
    @pytest.fixture()
    def repo(self) -> TokenRepository:
        return TokenRepository(_FakeConfigGroup())

    @pytest.fixture()
    def repo_with_data(self) -> TokenRepository:
        return TokenRepository(_FakeConfigGroup(_COMPLETE))

    def test_load_returns_none_when_empty(self, repo: TokenRepository) -> None:
        snap = asyncio.run(repo.load())
        assert snap is None

    def test_load_returns_snapshot_when_complete(self, repo_with_data: TokenRepository) -> None:
        snap = asyncio.run(repo_with_data.load())
        assert snap is not None
        assert snap.access_token == "acc"

    def test_replace_persists_all_fields(self, repo: TokenRepository) -> None:
        snap = TokenSnapshot.from_mapping(_COMPLETE)
        assert snap is not None
        asyncio.run(repo.replace(snap))
        loaded = asyncio.run(repo.load())
        assert loaded is not None
        assert loaded.as_mapping() == _COMPLETE

    def test_replace_rejects_incomplete_snapshot(self, repo: TokenRepository) -> None:
        # Construct a snapshot with a whitespace access_token to bypass frozen check
        import dataclasses
        incomplete = dataclasses.replace(
            TokenSnapshot(**_COMPLETE), access_token="  "
        )
        with pytest.raises(ValueError):
            asyncio.run(repo.replace(incomplete))

    def test_clear_sets_all_fields_to_none(self, repo_with_data: TokenRepository) -> None:
        asyncio.run(repo_with_data.clear())
        loaded = asyncio.run(repo_with_data.load())
        assert loaded is None


# ---------------------------------------------------------------------------
# TokenService
# ---------------------------------------------------------------------------

class TestTokenService:
    @pytest.fixture()
    def service(self) -> TokenService:
        return TokenService(TokenRepository(_FakeConfigGroup()))

    @pytest.fixture()
    def service_with_data(self) -> TokenService:
        return TokenService(TokenRepository(_FakeConfigGroup(_COMPLETE)))

    def test_initial_generation_is_zero(self, service: TokenService) -> None:
        assert service.generation == 0

    def test_replace_increments_generation(self, service: TokenService) -> None:
        snap = TokenSnapshot.from_mapping(_COMPLETE)
        assert snap is not None
        asyncio.run(service.replace(snap))
        assert service.generation == 1

    def test_logout_increments_generation(self, service_with_data: TokenService) -> None:
        gen_before = service_with_data.generation
        asyncio.run(service_with_data.logout())
        assert service_with_data.generation == gen_before + 1

    def test_logout_clears_persisted_data(self, service_with_data: TokenService) -> None:
        asyncio.run(service_with_data.logout())
        snap = asyncio.run(service_with_data.restore())
        assert snap is None

    def test_restore_returns_snapshot_when_present(self, service_with_data: TokenService) -> None:
        snap = asyncio.run(service_with_data.restore())
        assert snap is not None
        assert snap.access_token == "acc"

    def test_multiple_replaces_accumulate_generation(self, service: TokenService) -> None:
        snap = TokenSnapshot.from_mapping(_COMPLETE)
        assert snap is not None
        asyncio.run(service.replace(snap))
        asyncio.run(service.replace(snap))
        loop.run_until_complete(service.replace(snap))
        assert service.generation == 3
