import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body", [b"not json", b"[]", b'{"text": null}', b'{"text": 123}']
)
async def test_malformed_provider_response_is_unavailable(random_cog, body):
    random_cog._fetch = AsyncMock(return_value=body)
    assert await random_cog.get_fact() is None


@pytest.mark.asyncio
async def test_rss_missing_fields_does_not_crash(random_cog):
    random_cog._fetch = AsyncMock(
        return_value=b'<feed xmlns="http://www.w3.org/2005/Atom"><entry/></feed>'
    )
    assert await random_cog.get_showerthought() is None


@pytest.mark.asyncio
async def test_rss_cache_avoids_refetch(random_cog):
    random_cog._fetch = AsyncMock(return_value=b"<feed/>")
    await random_cog.fetch_rss("https://example.com/feed")
    await random_cog.fetch_rss("https://example.com/feed")
    assert random_cog._fetch.await_count == 1


class Response:
    status = 200

    def __init__(self, chunks):
        self.chunks = chunks
        self.content = self
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        self.closed = True

    async def iter_chunked(self, size):
        for chunk in self.chunks:
            if isinstance(chunk, BaseException):
                raise chunk
            yield chunk


@pytest.mark.asyncio
async def test_oversized_response_is_closed_and_rejected(random_cog):
    response = Response([b"x" * 300000, b"x" * 300000])
    random_cog.session = SimpleNamespace(
        closed=False, get=Mock(return_value=response), close=AsyncMock()
    )
    assert await random_cog._fetch("https://example.com/feed") is None
    assert response.closed


@pytest.mark.asyncio
async def test_cancelled_requests_release_request_slots(random_cog):
    random_cog.session = SimpleNamespace(
        closed=False,
        get=Mock(side_effect=lambda *a, **kw: Response([asyncio.CancelledError()])),
        close=AsyncMock(),
    )
    for _ in range(4):
        with pytest.raises(asyncio.CancelledError):
            await random_cog._fetch("https://example.com/feed")
    random_cog.session.get.side_effect = lambda *a, **kw: Response([b"healthy"])
    assert (
        await asyncio.wait_for(random_cog._fetch("https://example.com/feed"), 1)
        == b"healthy"
    )


@pytest.mark.asyncio
async def test_request_deadline_includes_wait_for_capacity(random_cog, monkeypatch):
    monkeypatch.setattr("randomtexts.randomchats.FETCH_DEADLINE", 0.01, raising=False)
    random_cog.session = SimpleNamespace(closed=False, close=AsyncMock())
    random_cog._network_slots = asyncio.Semaphore(0)
    assert (
        await asyncio.wait_for(random_cog._fetch("https://example.com/feed"), 0.5)
        is None
    )
