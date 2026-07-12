"""Contract tests for classify_provider_exception and typed ProviderFailure hierarchy."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from TidalPlayer.providers.errors import (
    AuthenticationRequired,
    DeadlineExceeded,
    MalformedResponse,
    NotFound,
    PermissionDenied,
    PlaybackUnavailable,
    ProviderFailure,
    RateLimited,
    TemporaryUnavailable,
    UnexpectedProviderFailure,
    classify_provider_exception,
)


# ---------------------------------------------------------------------------
# Hierarchy
# ---------------------------------------------------------------------------

class TestProviderFailureHierarchy:
    @pytest.mark.parametrize("cls", [
        AuthenticationRequired,
        PermissionDenied,
        NotFound,
        RateLimited,
        TemporaryUnavailable,
        MalformedResponse,
        DeadlineExceeded,
        PlaybackUnavailable,
        UnexpectedProviderFailure,
    ])
    def test_is_provider_failure_subclass(self, cls: type) -> None:
        assert issubclass(cls, ProviderFailure)

    def test_rate_limited_is_also_runtime_error(self) -> None:
        assert issubclass(RateLimited, RuntimeError)

    def test_rate_limited_carries_retry_after(self) -> None:
        exc = RateLimited(retry_after=42.0)
        assert exc.retry_after == 42.0

    def test_rate_limited_default_retry_after_is_none(self) -> None:
        exc = RateLimited()
        assert exc.retry_after is None


# ---------------------------------------------------------------------------
# classify_provider_exception
# ---------------------------------------------------------------------------

def _make_exc(status: int | None, retry_after: str | None = None) -> Exception:
    exc = Exception("provider error")
    exc.status = status  # type: ignore[attr-defined]
    if retry_after is not None:
        mock_response = MagicMock()
        mock_response.headers = {"Retry-After": retry_after}
        exc.response = mock_response  # type: ignore[attr-defined]
    return exc


class TestClassifyProviderException:
    def test_401_maps_to_authentication_required(self) -> None:
        result = classify_provider_exception(_make_exc(401))
        assert isinstance(result, AuthenticationRequired)

    def test_403_maps_to_permission_denied(self) -> None:
        result = classify_provider_exception(_make_exc(403))
        assert isinstance(result, PermissionDenied)

    def test_404_maps_to_not_found(self) -> None:
        result = classify_provider_exception(_make_exc(404))
        assert isinstance(result, NotFound)

    def test_429_maps_to_rate_limited(self) -> None:
        result = classify_provider_exception(_make_exc(429))
        assert isinstance(result, RateLimited)

    def test_429_with_retry_after_header_is_parsed(self) -> None:
        result = classify_provider_exception(_make_exc(429, retry_after="30"))
        assert isinstance(result, RateLimited)
        assert result.retry_after == 30.0

    def test_429_with_fractional_retry_after(self) -> None:
        result = classify_provider_exception(_make_exc(429, retry_after="1.5"))
        assert isinstance(result, RateLimited)
        assert result.retry_after == 1.5

    def test_429_with_invalid_retry_after_is_none(self) -> None:
        result = classify_provider_exception(_make_exc(429, retry_after="bad"))
        assert isinstance(result, RateLimited)
        assert result.retry_after is None

    def test_429_with_negative_retry_after_clamped_to_zero(self) -> None:
        result = classify_provider_exception(_make_exc(429, retry_after="-5"))
        assert isinstance(result, RateLimited)
        assert result.retry_after == 0.0

    def test_timeout_error_maps_to_deadline_exceeded(self) -> None:
        result = classify_provider_exception(TimeoutError("timed out"))
        assert isinstance(result, DeadlineExceeded)

    def test_unknown_status_maps_to_unexpected_failure(self) -> None:
        result = classify_provider_exception(_make_exc(500))
        assert isinstance(result, UnexpectedProviderFailure)

    def test_no_status_maps_to_unexpected_failure(self) -> None:
        result = classify_provider_exception(Exception("unknown"))
        assert isinstance(result, UnexpectedProviderFailure)

    def test_status_from_response_attribute(self) -> None:
        exc = Exception("err")
        mock_response = MagicMock()
        mock_response.status_code = 401
        mock_response.headers = {}
        exc.response = mock_response  # type: ignore[attr-defined]
        result = classify_provider_exception(exc)
        assert isinstance(result, AuthenticationRequired)
