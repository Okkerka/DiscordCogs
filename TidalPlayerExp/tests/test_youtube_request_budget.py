"""Optional matching and Google API transport ownership must remain bounded."""
import asyncio
import importlib
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from TidalPlayerExp.playback.models import SourceKind
from TidalPlayerExp.providers.youtube_resolver import YouTubeVideoMetadata


@pytest.mark.asyncio
async def test_optional_matching_deadline_includes_login(cog, monkeypatch):
    module = importlib.import_module(cog.__class__.__module__)
    monkeypatch.setattr(module, "YOUTUBE_MATCH_TIMEOUT", 0.01, raising=False)
    entered = asyncio.Event()
    async def login(*_):
        entered.set()
        await asyncio.Event().wait()
    monkeypatch.setattr(type(cog.tidal), "is_logged_in", login)
    monkeypatch.setattr(type(cog.tidal), "search", AsyncMock())
    video = YouTubeVideoMetadata("abcdefghijk", "Song", "Artist", 120, None)
    entry = await asyncio.wait_for(cog._youtube_entry(video, 42), 0.3)
    assert entered.is_set()
    assert entry.primary.kind is SourceKind.YOUTUBE
    assert entry.primary.identifier == "abcdefghijk"
    assert entry.meta["duration"] == 120
    cog.tidal.search.assert_not_awaited()


def test_google_requests_own_and_close_their_http_transport():
    code = '''
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import patch
from googleapiclient.http import HttpRequest
from TidalPlayerExp.providers.google_transport import IsolatedHttpRequest
barrier = Barrier(2)
created = []
class Transport:
    def __init__(self, **kwargs):
        self.closed = False
        created.append(self)
    def close(self):
        self.closed = True
def execute(self, http=None, num_retries=0):
    assert http is not self.http
    barrier.wait(timeout=2)
    if self.uri.endswith('fail'):
        raise ValueError('provider failed')
    return 'success'
def run(uri):
    request = IsolatedHttpRequest(object(), lambda *args: None, uri)
    try:
        return request.execute()
    except ValueError:
        return 'failed'
with patch('TidalPlayerExp.providers.google_transport.httplib2.Http', Transport), patch.object(HttpRequest, 'execute', execute):
    with ThreadPoolExecutor(2) as executor:
        assert list(executor.map(run, ['https://example.com/ok', 'https://example.com/fail'])) == ['success', 'failed']
assert len(created) == 2 and created[0] is not created[1]
assert all(item.closed for item in created)
'''
    result = subprocess.run([sys.executable, "-c", code],
                            cwd=Path(__file__).resolve().parents[2],
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
