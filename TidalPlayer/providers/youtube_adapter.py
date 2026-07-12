"""YouTube adapter: converts YouTube playlist items to NormalizedCandidates.

Only playlist imports are supported; YouTube does not expose ISRC or reliable
duration metadata, so candidates carry title/artist extracted from video titles
only.  The Red shared API key store is used; no hardcoded credentials exist here.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

from ..domain.candidates import NormalizedCandidate
from .errors import (
    AuthenticationRequired,
    MalformedResponse,
    ProviderFailure,
    classify_provider_exception,
)

_MAX_PLAYLIST_FETCH = 200
_PAGE_SIZE = 50

# Best-effort split on common separators: " - ", " – ", " — "
_TITLE_SPLIT_RE = re.compile(r"\s[\-\u2013\u2014]\s")


def _video_title_to_candidate(video_title: str, channel_title: str) -> NormalizedCandidate | None:
    """Parse a YouTube video title into a NormalizedCandidate.

    YouTube video titles are free-form.  We attempt a best-effort parse of
    "Artist - Title" formats; if the separator is absent, the channel name is
    used as artist and the full title as track name.  ISRCs and exact durations
    are unavailable from the Data API v3 playlist endpoint.
    """
    video_title = video_title.strip()
    if not video_title:
        return None
    parts = _TITLE_SPLIT_RE.split(video_title, maxsplit=1)
    if len(parts) == 2:
        artist_raw, track_raw = parts[0].strip(), parts[1].strip()
    else:
        artist_raw = channel_title.strip() or "Unknown"
        track_raw = video_title
    if not track_raw:
        return None
    return NormalizedCandidate(
        title=track_raw,
        artists=(artist_raw,) if artist_raw else ("Unknown",),
        isrc=None,
        duration=None,
        source="youtube",
    )


class YouTubeAdapter:
    """Fetch normalized candidates from a YouTube playlist via Data API v3.

    The bot reference is used to obtain the API key from Red's shared token
    store so credentials are never hardcoded.
    """

    SERVICE_NAME = "youtube"

    def __init__(self, bot: Any) -> None:
        self._bot = bot

    async def _build_client(self) -> Any:
        """Build an authenticated YouTube Data API v3 resource object.

        Raises AuthenticationRequired if the owner has not configured an API key.
        """
        tokens = await self._bot.get_shared_api_tokens(self.SERVICE_NAME)
        api_key = tokens.get("api_key") or ""
        if not api_key:
            raise AuthenticationRequired(
                "YouTube API key not configured. Use [p]set api youtube api_key,<key>"
            )
        try:
            from googleapiclient.discovery import build as _build  # noqa: PLC0415
            return _build("youtube", "v3", developerKey=api_key)
        except Exception as exc:
            raise AuthenticationRequired("Failed to build YouTube API client") from exc

    async def fetch_playlist_candidates(self, playlist_id: str) -> list[NormalizedCandidate]:
        """Return up to _MAX_PLAYLIST_FETCH candidates from a YouTube playlist."""
        client = await self._build_client()
        loop = asyncio.get_event_loop()
        candidates: list[NormalizedCandidate] = []
        page_token: str | None = None
        fetched = 0
        while fetched < _MAX_PLAYLIST_FETCH:
            batch_limit = min(_PAGE_SIZE, _MAX_PLAYLIST_FETCH - fetched)
            request_kwargs: dict[str, Any] = {
                "part": "snippet",
                "playlistId": playlist_id,
                "maxResults": batch_limit,
            }
            if page_token is not None:
                request_kwargs["pageToken"] = page_token
            try:
                response = await loop.run_in_executor(
                    None,
                    lambda: client.playlistItems().list(**request_kwargs).execute(),
                )
            except ProviderFailure:
                raise
            except Exception as exc:
                raise classify_provider_exception(exc) from exc
            if not isinstance(response, dict):
                raise MalformedResponse("YouTube returned a malformed playlist response")
            items = response.get("items") or []
            for item in items:
                snippet = item.get("snippet") if isinstance(item, dict) else None
                if not isinstance(snippet, dict):
                    continue
                video_title = snippet.get("title", "") or ""
                channel_title = snippet.get("videoOwnerChannelTitle") or snippet.get("channelTitle", "")
                # Skip deleted/private videos which YouTube marks with these titles
                if video_title.lower() in {"deleted video", "private video"}:
                    continue
                candidate = _video_title_to_candidate(video_title, channel_title)
                if candidate is not None:
                    candidates.append(candidate)
            fetched += len(items)
            page_token = response.get("nextPageToken")
            if not page_token or not items:
                break
        return candidates
