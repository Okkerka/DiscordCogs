"""Contract tests for CircuitBreaker state transitions."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from TidalPlayer.providers.circuit_breaker import CircuitBreaker, CircuitState
from TidalPlayer.providers.errors import TemporaryUnavailable


def _run(coro):
    return asyncio.run(coro)


async def _ok() -> str:
    return "ok"


async def _fail() -> None:
    raise ValueError("provider error")


class TestCircuitBreakerClosed:
    def test_initial_state_is_closed(self) -> None:
        cb = CircuitBreaker("test", failure_threshold=3)
        assert cb.state is CircuitState.CLOSED

    def test_successful_call_returns_value(self) -> None:
        cb = CircuitBreaker("test")
        result = _run(cb.call(_ok))
        assert result == "ok"

    def test_single_failure_stays_closed(self) -> None:
        cb = CircuitBreaker("test", failure_threshold=3)
        with pytest.raises(ValueError):
            _run(cb.call(_fail))
        assert cb.state is CircuitState.CLOSED

    def test_failures_below_threshold_stay_closed(self) -> None:
        cb = CircuitBreaker("test", failure_threshold=3)
        for _ in range(2):
            with pytest.raises(ValueError):
                _run(cb.call(_fail))
        assert cb.state is CircuitState.CLOSED

    def test_success_resets_failure_counter(self) -> None:
        cb = CircuitBreaker("test", failure_threshold=3)
        for _ in range(2):
            with pytest.raises(ValueError):
                _run(cb.call(_fail))
        _run(cb.call(_ok))  # resets counter
        with pytest.raises(ValueError):
            _run(cb.call(_fail))  # back to 1 failure
        assert cb.state is CircuitState.CLOSED


class TestCircuitBreakerOpening:
    def test_trips_to_open_at_threshold(self) -> None:
        cb = CircuitBreaker("test", failure_threshold=3)
        for _ in range(3):
            with pytest.raises(ValueError):
                _run(cb.call(_fail))
        assert cb.state is CircuitState.OPEN

    def test_open_circuit_raises_temporary_unavailable(self) -> None:
        cb = CircuitBreaker("test", failure_threshold=1)
        with pytest.raises(ValueError):
            _run(cb.call(_fail))
        assert cb.state is CircuitState.OPEN
        with pytest.raises(TemporaryUnavailable):
            _run(cb.call(_ok))  # call blocked; _ok never runs

    def test_open_circuit_does_not_call_fn(self) -> None:
        cb = CircuitBreaker("test", failure_threshold=1)
        with pytest.raises(ValueError):
            _run(cb.call(_fail))
        probe = AsyncMock(return_value="should-not-run")
        with pytest.raises(TemporaryUnavailable):
            _run(cb.call(probe))
        probe.assert_not_awaited()


class TestCircuitBreakerHalfOpen:
    def test_transitions_to_half_open_after_timeout(self) -> None:
        cb = CircuitBreaker("test", failure_threshold=1, recovery_timeout=0.0)
        with pytest.raises(ValueError):
            _run(cb.call(_fail))
        assert cb.state is CircuitState.OPEN
        # With recovery_timeout=0, the next call should see half-open
        with patch.object(cb, "_maybe_transition_to_half_open", wraps=cb._maybe_transition_to_half_open):
            # Force time to have elapsed by using timeout=0
            _run(cb.call(_ok))
        assert cb.state is CircuitState.CLOSED

    def test_failure_in_half_open_reopens(self) -> None:
        cb = CircuitBreaker("test", failure_threshold=1, recovery_timeout=0.0)
        with pytest.raises(ValueError):
            _run(cb.call(_fail))
        # First probe attempt (recovery_timeout=0 means already elapsed)
        with pytest.raises(ValueError):
            _run(cb.call(_fail))
        assert cb.state is CircuitState.OPEN

    def test_required_probe_successes_close_circuit(self) -> None:
        cb = CircuitBreaker("test", failure_threshold=1, recovery_timeout=0.0, probe_successes=2)
        with pytest.raises(ValueError):
            _run(cb.call(_fail))
        _run(cb.call(_ok))  # first probe: still half-open
        assert cb.state is CircuitState.HALF_OPEN
        _run(cb.call(_ok))  # second probe: closes
        assert cb.state is CircuitState.CLOSED

    def test_half_open_allows_only_one_concurrent_probe(self) -> None:
        async def scenario() -> None:
            cb = CircuitBreaker("test", failure_threshold=1, recovery_timeout=0.0)
            with pytest.raises(ValueError):
                await cb.call(_fail)

            probe_started = asyncio.Event()
            release_probe = asyncio.Event()

            async def probe() -> str:
                probe_started.set()
                await release_probe.wait()
                return "ok"

            first_probe = asyncio.create_task(cb.call(probe))
            await probe_started.wait()

            blocked_probe = AsyncMock(return_value="must-not-run")
            with pytest.raises(TemporaryUnavailable):
                await cb.call(blocked_probe)
            blocked_probe.assert_not_awaited()

            release_probe.set()
            assert await first_probe == "ok"
            assert cb.state is CircuitState.CLOSED

        _run(scenario())
