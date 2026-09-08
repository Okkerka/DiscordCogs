"""OAuth refresh contracts, including the installed tidalapi implementation."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.mark.asyncio
@pytest.mark.parametrize("old_result", [True, False, RuntimeError("old credentials")])
async def test_login_check_cannot_restore_or_invalidate_session_after_logout(cog, old_result):
    loop = asyncio.get_running_loop()
    started = asyncio.Event()
    release = threading.Event()

    def old_login_check():
        loop.call_soon_threadsafe(started.set)
        assert release.wait(timeout=2)
        if isinstance(old_result, Exception):
            raise old_result
        return old_result

    cog.tidal.session = SimpleNamespace(check_login=old_login_check)
    cog.tidal.invalidate_login_cache()
    pending = asyncio.create_task(cog.tidal.is_logged_in())
    try:
        await asyncio.wait_for(started.wait(), timeout=1)
        await cog.tidal.logout()
        # Also cover a new successful login while the old SDK call winds down.
        expected_login = old_result is not True
        cog.tidal._login_cache = expected_login
        cog.tidal._login_cache_time = loop.time()
        release.set()
        assert await pending is False
        assert await cog.tidal.is_logged_in() is expected_login
    finally:
        release.set()
        await asyncio.gather(pending, return_exceptions=True)
        await cog.tidal.unload()


def _credentials(expiry: datetime) -> dict:
    return {
        "token_type": "Bearer",
        "access_token": "stored-access",
        "refresh_token": "stored-refresh",
        "expiry_time": int(expiry.timestamp()),
    }


def _make_handler(bot):
    from TidalPlayerExp.providers.tokens import TokenRepository, TokenService
    from TidalPlayerExp.tests.conftest import FakeConfig
    from TidalPlayerExp.tidalplayer import TidalHandler

    return TidalHandler(bot, TokenService(TokenRepository(FakeConfig())))


async def _real_tidal_case(mode: str) -> None:
    """Keep tidalapi real; replace only its credential-free HTTP transport."""
    import requests

    from TidalPlayerExp.providers.tokens import TokenSnapshot

    class Transport:
        def post(self, url, data):
            assert data["grant_type"] == "refresh_token"
            assert data["refresh_token"] == "stored-refresh"
            return self.response(url, 200, {
                "access_token": "refreshed-access",
                "token_type": "Bearer",
                "expires_in": 86400,
            })

        def request(self, method, url, *, params, data, headers):
            assert method == "GET"
            if headers["authorization"] == "Bearer stored-access":
                message = (
                    "The token has expired."
                    if mode == "restore_expired"
                    else "Authentication required"
                )
                return self.response(url, 401, {"userMessage": message})
            assert headers["authorization"] == "Bearer refreshed-access"
            if url.endswith("/sessions"):
                payload = {"sessionId": "session-1", "countryCode": "HU", "userId": 1}
            elif url.endswith("/users/1"):
                payload = {
                    "id": 1, "username": "test-user", "email": "user@example.invalid",
                    "firstName": "Test", "lastName": "User", "picture": None,
                }
            elif url.endswith("/users/1/subscription"):
                payload = {}
            else:
                raise AssertionError(f"Unexpected HTTP endpoint: {url}")
            return self.response(url, 200, payload)

        @staticmethod
        def response(url, status, payload):
            response = requests.Response()
            response.status_code = status
            response._content = json.dumps(payload).encode()
            response.url = url
            response.request = requests.Request("GET", url).prepare()
            return response

    handler = _make_handler(SimpleNamespace())
    session = handler.session
    # Fail clearly if a test runner accidentally imports the suite's tidalapi stub.
    assert type(session).__module__ == "tidalapi.session"
    session.request_session.close()
    session.request_session = Transport()
    expiry = datetime.now(timezone.utc) + timedelta(
        hours=6 if mode == "forced_401" else -1
    )
    credentials = _credentials(expiry)
    await handler.tokens.replace(TokenSnapshot(**credentials))
    generation = handler.tokens.generation
    try:
        if mode == "restore_expired":
            await handler.initialize(credentials)
        else:
            session.token_type = "Bearer"
            session.access_token = "stored-access"
            session.refresh_token = "stored-refresh"
            session.expiry_time = expiry
            if mode == "forced_401":
                response = await handler._run_with_backoff(
                    lambda: session.request.request("GET", "sessions")
                )
                assert response.json()["sessionId"] == "session-1"
            else:
                assert await handler.refresh_tokens(), "Installed tidalapi refresh failed"

        snapshot = await handler.tokens.restore()
        assert snapshot.access_token == "refreshed-access"
        assert snapshot.refresh_token == "stored-refresh"
        assert snapshot.token_type == "Bearer"
        assert snapshot.expiry_time > int(datetime.now(timezone.utc).timestamp()) + 82800
        assert handler.tokens.generation == generation + 1
        assert await handler.is_logged_in()
    finally:
        await handler.unload()


@pytest.mark.parametrize("mode", ["scheduled_refresh", "restore_expired", "forced_401"])
def test_real_tidalapi_refresh_persists_credentials(mode):
    # The suite stubs tidalapi globally, so exercise the installed package in isolation.
    script = (
        "import asyncio, sys; sys.path.insert(0, sys.argv[1]); "
        "from TidalPlayerExp.tests.test_tidal_refresh import _real_tidal_case; "
        "asyncio.run(_real_tidal_case(sys.argv[2]))"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-c", script, str(Path(__file__).resolve().parents[2]), mode],
        capture_output=True, text=True, timeout=30, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["false", "incomplete", "exception", "unsupported"])
async def test_failed_refresh_preserves_credentials_and_invalidates_login(fake_bot, caplog, failure):
    from TidalPlayerExp.providers.tokens import TokenSnapshot

    handler = _make_handler(fake_bot)
    expiry = datetime.now(timezone.utc) - timedelta(hours=1)
    original = TokenSnapshot(**_credentials(expiry))
    await handler.tokens.replace(original)
    handler._login_cache = True
    handler._login_cache_time = asyncio.get_running_loop().time()
    session = SimpleNamespace(
        token_type="Bearer", access_token="stored-access", refresh_token="stored-refresh",
        expiry_time=expiry, check_login=lambda: False,
    )

    def refresh(refresh_token):
        assert refresh_token == "stored-refresh"
        session.access_token = "partial-access"
        if failure == "exception":
            raise RuntimeError("credential-in-provider-exception")
        if failure == "incomplete":
            session.token_type = ""
            return True
        return False

    if failure != "unsupported":
        session.token_refresh = refresh
    handler.session = session
    try:
        assert not await handler.refresh_tokens()
        assert await handler.tokens.restore() == original
        assert handler.tokens.generation == 1
        assert not await handler.is_logged_in()
        assert "credential-in-provider-exception" not in caplog.text
    finally:
        await handler.unload()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["false", "incomplete", "exception"])
async def test_failed_restore_preserves_credentials_and_invalidates_login(fake_bot, caplog, failure):
    from TidalPlayerExp.providers.tokens import TokenSnapshot

    handler = _make_handler(fake_bot)
    expiry = datetime.now(timezone.utc) + timedelta(hours=6)
    credentials = _credentials(expiry)
    original = TokenSnapshot(**credentials)
    await handler.tokens.replace(original)
    handler._login_cache = True
    handler._login_cache_time = asyncio.get_running_loop().time()
    session = SimpleNamespace(
        token_type="Bearer", access_token="partial-access", refresh_token="stored-refresh",
        expiry_time=expiry, check_login=lambda: False,
    )

    def load(token_type, access_token, refresh_token, expiry_time):
        if failure == "exception":
            raise RuntimeError("credential-in-provider-exception")
        if failure == "incomplete":
            session.access_token = ""
            return True
        return False

    session.load_oauth_session = load
    handler.session = session
    try:
        await handler.initialize(credentials)
        assert not await handler.is_logged_in()
        assert await handler.tokens.restore() == original
        assert handler.tokens.generation == 1
        assert "credential-in-provider-exception" not in caplog.text
    finally:
        await handler.unload()
