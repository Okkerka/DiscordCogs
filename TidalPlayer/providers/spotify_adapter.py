"""Spotify adapter: converts Spotify track/album/playlist data to NormalizedCandidates.

All spotipy objects are consumed here and never returned to callers.  The Red
shared API token store is used for credentials; no hardcoded secrets exist in
this file.  The adapter is injected with a bot reference so it can fetch tokens
lazily without coupling to the cog lifecycle.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from ..domain.candidates import NormalizedCandidate
from .errors import (
    AuthenticationRequired,
    MalformedResponse,
    NotFound,
    ProviderFailure,
    classify_provider_exception,
)

if TYPE_CHECKING:
    pass

_MAX_PLAYLIST_FETCH = 200  # hard cap: never request unbounded Spotify pages
_PAGE_SIZE = 50


def _item_to_candidate(item: Any, source: str) -> NormalizedCandidate | None:
    """Convert a single Spotipy track dict to a NormalizedCandidate.

    Returns None for items that are missing required fields rather than raising,
    so callers can skip malformed entries without aborting a full page.
    """
    if item is None:
        return None
    # Spotify nested track objects (playlist items wrap a 'track' key)
    if "track" in item and isinstance(item["track"], dict):
        item = item["track"]
    try:
        title: str = item["name"]
        artists: tuple[str, ...] = tuple(
            a["name"] for a in item.get("artists", []) if isinstance(a, dict) and a.get("name")
        )
        isrc: str | None = (
            item.get("external_ids", {}).get("isrc") or None
        )
        duration_ms: int | None = item.get("duration_ms")
        duration: int | None = round(duration_ms / 1000) if isinstance(duration_ms, int) else None
    except (KeyError, TypeError):
        return None
    if not title or not artists:
        return None
    return NormalizedCandidate(
        title=title,
        artists=artists,
        isrc=isrc,
        duration=duration,
        source=source,
    )


class SpotifyAdapter:
    """Fetch normalized candidates from Spotify tracks, albums, and playlists.

    The bot reference is used to call ``get_shared_api_tokens`` so that
    credentials are never hardcoded and are revocable by the bot owner.
    """

    SERVICE_NAME = "spotify"

    def __init__(self, bot: Any) -> None:
        self._bot = bot

    async def _build_client(self) -> Any:
        """Construct an authenticated spotipy.Spotify client from Red shared tokens.

        Raises AuthenticationRequired if the owner has not configured credentials.
        """
        import spotipy  # noqa: PLC0415
        import spotipy.oauth2 as _oauth  # noqa: PLC0415

        tokens = await self._bot.get_shared_api_tokens(self.SERVICE_NAME)
        client_id = tokens.get("client_id") or ""
        client_secret = tokens.get("client_secret") or ""
        if not client_id or not client_secret:
            raise AuthenticationRequired(
                "Spotify credentials not configured. Use [p]set api spotify client_id,<id> "
                "client_secret,<secret>"
            )
        try:
            credentials = _oauth.SpotifyClientCredentials(
                client_id=client_id, client_secret=client_secret
            )
            return spotipy.Spotify(auth_manager=credentials)
        except Exception as exc:
            raise AuthenticationRequired("Failed to authenticate with Spotify") from exc

    async def fetch_track_candidates(self, spotify_id: str) -> list[NormalizedCandidate]:
        """Return a single-element list for a Spotify track ID."""
        client = await self._build_client()
        try:
            item = await asyncio.get_event_loop().run_in_executor(
                None, lambda: client.track(spotify_id)
            )
        except ProviderFailure:
            raise
        except Exception as exc:
            raise classify_provider_exception(exc) from exc
        if not isinstance(item, dict):
            raise MalformedResponse("Spotify returned a malformed track response")
        candidate = _item_to_candidate(item, source="spotify")
        if candidate is None:
            raise NotFound(f"Spotify track {spotify_id} missing required fields")
        return [candidate]

    async def fetch_album_candidates(self, spotify_id: str) -> list[NormalizedCandidate]:
        """Return candidates for all tracks in a Spotify album."""
        client = await self._build_client()
        loop = asyncio.get_event_loop()
        try:
            album = await loop.run_in_executor(None, lambda: client.album(spotify_id))
        except ProviderFailure:
            raise
        except Exception as exc:
            raise classify_provider_exception(exc) from exc
        if not isinstance(album, dict):
            raise MalformedResponse("Spotify returned a malformed album response")
        raw_tracks = album.get("tracks", {}).get("items", [])
        candidates: list[NormalizedCandidate] = []
        for item in raw_tracks:
            candidate = _item_to_candidate(item, source="spotify")
            if candidate is not None:
                candidates.append(candidate)
        return candidates

    async def fetch_playlist_candidates(self, spotify_id: str) -> list[NormalizedCandidate]:
        """Return candidates for up to _MAX_PLAYLIST_FETCH tracks in a Spotify playlist."""
        client = await self._build_client()
        loop = asyncio.get_event_loop()
        candidates: list[NormalizedCandidate] = []
        offset = 0
        while offset < _MAX_PLAYLIST_FETCH:
            batch_limit = min(_PAGE_SIZE, _MAX_PLAYLIST_FETCH - offset)
            try:
                page = await loop.run_in_executor(
                    None,
                    lambda: client.playlist_items(
                        spotify_id,
                        limit=batch_limit,
                        offset=offset,
                        additional_types=("track",),
                    ),
                )
            except ProviderFailure:
                raise
            except Exception as exc:
                raise classify_provider_exception(exc) from exc
            if not isinstance(page, dict):
                raise MalformedResponse("Spotify returned a malformed playlist page")
            items = page.get("items") or []
            for item in items:
                candidate = _item_to_candidate(item, source="spotify")
                if candidate is not None:
                    candidates.append(candidate)
            if not items or page.get("next") is None:
                break
            offset += len(items)
        return candidates
