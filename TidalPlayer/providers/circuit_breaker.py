"""Per-provider circuit breaker with closed / open / half-open states.

Each provider gets its own CircuitBreaker instance so a Tidal outage cannot
block Spotify lookups and vice-versa.  All state mutations are guarded by a
single asyncio.Lock; no threading primitives are used.
"""

from __future__ import annotations

import asyncio
import time
from enum import Enum
from typing import Awaitable, Callable, TypeVar

from .errors import TemporaryUnavailable

_T = TypeVar("_T")


class CircuitState(Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    """Async circuit breaker wrapping any awaitable call.

    Parameters
    ----------
    name:
        Human-readable provider name used in error messages.
    failure_threshold:
        Consecutive failures required to trip the circuit.
    recovery_timeout:
        Seconds to wait in OPEN state before a single probe is allowed.
    probe_successes:
        Consecutive successes in HALF_OPEN required to close the circuit.
    """

    def __init__(
        self,
        name: str,
        *,
        failure_threshold: int = 5,
        recovery_timeout: float = 30.0,
        probe_successes: int = 1,
    ) -> None:
        self._name = name
        self._failure_threshold = failure_threshold
        self._recovery_timeout = recovery_timeout
        self._probe_successes = probe_successes

        self._state = CircuitState.CLOSED
        self._consecutive_failures = 0
        self._consecutive_probe_successes = 0
        self._opened_at: float | None = None
        self._lock = asyncio.Lock()

    @property
    def state(self) -> CircuitState:
        return self._state

    async def call(self, fn: Callable[[], Awaitable[_T]]) -> _T:
        """Execute fn under circuit-breaker protection.

        Raises TemporaryUnavailable immediately when the circuit is OPEN and
        the recovery window has not elapsed.
        """
        async with self._lock:
            await self._maybe_transition_to_half_open()
            if self._state is CircuitState.OPEN:
                raise TemporaryUnavailable(
                    f"Circuit open for provider '{self._name}'; retrying later"
                )

        try:
            result = await fn()
        except Exception as exc:
            async with self._lock:
                self._record_failure()
            raise
        else:
            async with self._lock:
                self._record_success()
            return result

    # ------------------------------------------------------------------
    # Internal state machine (must be called under self._lock)
    # ------------------------------------------------------------------

    async def _maybe_transition_to_half_open(self) -> None:
        if (
            self._state is CircuitState.OPEN
            and self._opened_at is not None
            and (time.monotonic() - self._opened_at) >= self._recovery_timeout
        ):
            self._state = CircuitState.HALF_OPEN
            self._consecutive_probe_successes = 0

    def _record_failure(self) -> None:
        if self._state is CircuitState.HALF_OPEN:
            # Any failure during probing reopens immediately.
            self._trip()
            return
        self._consecutive_failures += 1
        if self._consecutive_failures >= self._failure_threshold:
            self._trip()

    def _record_success(self) -> None:
        if self._state is CircuitState.HALF_OPEN:
            self._consecutive_probe_successes += 1
            if self._consecutive_probe_successes >= self._probe_successes:
                self._close()
            return
        # CLOSED: reset failure counter on any success.
        self._consecutive_failures = 0

    def _trip(self) -> None:
        self._state = CircuitState.OPEN
        self._opened_at = time.monotonic()
        self._consecutive_failures = 0
        self._consecutive_probe_successes = 0

    def _close(self) -> None:
        self._state = CircuitState.CLOSED
        self._consecutive_failures = 0
        self._consecutive_probe_successes = 0
        self._opened_at = None
