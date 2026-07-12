"""Typed, sanitized failures produced at provider and audio boundaries."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final


class ProviderFailure(RuntimeError):
    """Base class whose message is safe to show or log without raw provider details."""


class AuthenticationRequired(ProviderFailure):
    pass


class PermissionDenied(ProviderFailure):
    pass


class NotFound(ProviderFailure):
    pass


@dataclass(frozen=True)
class RateLimited(ProviderFailure):
    retry_after: float | None = None


class TemporaryUnavailable(ProviderFailure):
    pass


class MalformedResponse(ProviderFailure):
    pass


class DeadlineExceeded(ProviderFailure):
    pass


class PlaybackUnavailable(ProviderFailure):
    pass


class UnexpectedProviderFailure(ProviderFailure):
    pass


_STATUS_FAILURES: Final = {
    401: AuthenticationRequired,
    403: PermissionDenied,
    404: NotFound,
    429: RateLimited,
}


def classify_provider_exception(error: Exception) -> ProviderFailure:
    """Map untrusted third-party errors into stable application failures."""
    status = getattr(error, "status", None) or getattr(error, "status_code", None)
    response = getattr(error, "response", None)
    if status is None and response is not None:
        status = getattr(response, "status_code", None)
    if status in _STATUS_FAILURES:
        if status == 429:
            headers = getattr(response, "headers", {}) or {}
            raw_retry = headers.get("Retry-After") if hasattr(headers, "get") else None
            try:
                retry_after = max(0.0, float(raw_retry)) if raw_retry is not None else None
            except (TypeError, ValueError):
                retry_after = None
            return RateLimited(retry_after)
        return _STATUS_FAILURES[status]()
    if isinstance(error, TimeoutError):
        return DeadlineExceeded()
    return UnexpectedProviderFailure()
