"""Per-execution HTTP ownership for the optional Google YouTube API."""
from __future__ import annotations

from typing import Any

import httplib2
from googleapiclient.http import HttpRequest


class IsolatedHttpRequest(HttpRequest):
    """Never share httplib2 connections between executor workers.

    The cog uses API-key requests, not an authorized HTTP transport. Construct
    and close the connection inside the worker so async cancellation cannot
    close a connection that a timed-out worker is still using.
    """

    def execute(self, http: Any = None, num_retries: int = 0) -> Any:
        transport = httplib2.Http(timeout=15)
        try:
            return super().execute(http=transport, num_retries=num_retries)
        finally:
            transport.close()
