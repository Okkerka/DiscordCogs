"""Contract tests for TokenSnapshot, TokenRepository, and TokenService."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from TidalPlayerExp.providers.tokens import TokenRepository, TokenService, TokenSnapshot
from TidalPlayerExp.tests.conftest import FakeConfig, _ConfigValue


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_COMPLETE = {
    "token_type": "Bearer",
    "access_token": "acc",
    "refresh_token": "ref",
    "expiry_time": 9_999_999_999,
}


class _BlockingField(_ConfigValue):
    def __init__(self, value: Any, started: asyncio.Event, release: asyncio.Event) -> None:
        super().__init__(value)
        self.started = started
        self.release = release

    async def set(self, value: Any) -> None:
        self.started.set()
        await self.release.wait()
        self._value = value


class _FakeConfigGroup(FakeConfig):
    def __init__(self, data: dict[str, Any] | None = None) -> None:
        super().__init__()
        for key, value in (data or {}).items():
            setattr(self, key, _ConfigValue(value))


class _FailingField(_ConfigValue):
    async def set(self, value: Any) -> None:
        raise OSError("persistence unavailable")


class _FailingConfigGroup(_FakeConfigGroup):
    def __init__(self, data: dict[str, Any]) -> None:
        super().__init__(data)
        self.access_token = _FailingField(data["access_token"])

    async def set(self, data: dict[str, Any]) -> None:
        raise OSError("persistence unavailable")


class _BlockingConfigGroup(_FakeConfigGroup):
    def __init__(self, data: dict[str, Any] | None = None) -> None:
        super().__init__(data)
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.access_token = _BlockingField(
            self.access_token._value, self.started, self.release
        )

    async def set(self, data: dict[str, Any]) -> None:
        self.started.set()
        await self.release.wait()
        await super().set(data)


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

    @pytest.mark.asyncio
    async def test_load_waits_for_complete_token_replacement(self) -> None:
        config = _BlockingConfigGroup()
        repository = TokenRepository(config)
        snapshot = TokenSnapshot(**_COMPLETE)

        replace_task = asyncio.create_task(repository.replace(snapshot))
        await asyncio.wait_for(config.started.wait(), timeout=1)
        load_task = asyncio.create_task(repository.load())
        await asyncio.sleep(0)

        assert not load_task.done()
        config.release.set()
        await replace_task
        assert await load_task == snapshot

    @pytest.mark.asyncio
    @pytest.mark.parametrize("operation", ["replace", "clear"])
    async def test_failed_write_keeps_previous_credentials(self, operation: str) -> None:
        previous = {**_COMPLETE, "_schema_version": 3, "other": {"enabled": True}}
        config = _FailingConfigGroup(previous)
        repository = TokenRepository(config)
        replacement = TokenSnapshot("Other", "new-acc", "new-ref", 12345)

        with pytest.raises(OSError, match="persistence unavailable"):
            if operation == "replace":
                await repository.replace(replacement)
            else:
                await repository.clear()

        assert await config.all() == previous

    @pytest.mark.asyncio
    @pytest.mark.parametrize("operation", ["replace", "clear"])
    async def test_cancelled_write_keeps_previous_credentials(self, operation: str) -> None:
        previous = {**_COMPLETE, "_schema_version": 3, "other": {"enabled": True}}
        config = _BlockingConfigGroup(previous)
        repository = TokenRepository(config)
        replacement = TokenSnapshot("Other", "new-acc", "new-ref", 12345)
        operation_task = asyncio.create_task(
            repository.replace(replacement) if operation == "replace" else repository.clear()
        )
        await asyncio.wait_for(config.started.wait(), timeout=1)

        operation_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await operation_task

        assert await config.all() == previous
        config.release.set()
        await asyncio.wait_for(repository.clear(), timeout=1)
        assert await repository.load() is None

    @pytest.mark.asyncio
    async def test_replace_and_clear_preserve_other_global_settings(self) -> None:
        config = _FakeConfigGroup({"_schema_version": 3, "other": {"enabled": True}})
        repository = TokenRepository(config)

        await repository.replace(TokenSnapshot(**_COMPLETE))
        assert await config.all() == {
            **_COMPLETE, "_schema_version": 3, "other": {"enabled": True}
        }

        await repository.clear()
        assert await config.all() == {
            "token_type": None, "access_token": None,
            "refresh_token": None, "expiry_time": None,
            "_schema_version": 3, "other": {"enabled": True},
        }


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
        asyncio.run(service.replace(snap))
        assert service.generation == 3
