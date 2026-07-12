"""TidalClient: sole boundary for all tidalapi interaction.

No tidalapi object escapes this module.  Every public method returns either
a domain model from TidalPlayer.domain or raises a typed ProviderFailure.
The session is injected at construction so tests can substitute a fake.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from ..domain.candidates import NormalizedCandidate
from ..domain.models import TrackMeta
from .errors import (
    AuthenticationRequired,
    MalformedResponse,
    NotFound,
    PlaybackUnavailable,
    ProviderFailure,
    classify_provider_exception,
)

if TYPE_CHECKING:
    pass


def _track_to_meta(track: Any) -> TrackMeta:
    """Convert a tidalapi Track object to an immutable TrackMeta dict.

    Raises MalformedResponse if required fields are absent or wrong type.
    """
    try:
        title: str = track.name
        artist: str = track.artist.name
        duration: int = int(track.duration)
        track_id: int | None = getattr(track, "id", None)
    except (AttributeError, TypeError, ValueError) as exc:
        raise MalformedResponse("Provider returned a malformed track object") from exc

    album_obj = getattr(track, "album", None)
    album: str | None = getattr(album_obj, "name", None) if album_obj else None
    image: str | None = None
    if album_obj is not None:
        try:
            image = album_obj.image(320)
        except Exception:  # noqa: BLE001 — provider image call is best-effort
            image = None
    share_url: str | None = getattr(track, "share_url", None)

    quality_raw = getattr(track, "audio_quality", None)
    quality: str = str(quality_raw) if quality_raw is not None else "UNKNOWN"
    audio_resolution: str | None = None
    if quality_raw == "HI_RES":
        audio_resolution = "24bit / 96kHz"

    return TrackMeta(
        title=title,
        artist=artist,
        album=album,
        duration=duration,
        quality=quality,
        image=image,
        share_url=share_url,
        audio_resolution=audio_resolution,
        track_id=track_id,
    )


def _track_to_candidate(track: Any) -> NormalizedCandidate:
    """Convert a tidalapi Track to a NormalizedCandidate (for matching layer)."""
    try:
        title: str = track.name
        artist_name: str = track.artist.name
        isrc: str | None = getattr(track, "isrc", None)
        duration: int | None = int(track.duration) if track.duration is not None else None
    except (AttributeError, TypeError, ValueError) as exc:
        raise MalformedResponse("Provider returned a malformed track object") from exc
    return NormalizedCandidate(
        title=title,
        artists=(artist_name,),
        isrc=isrc or None,
        duration=duration,
        source="tidal",
    )


class TidalClient:
    """Async wrapper around a tidalapi.Session.

    The caller is responsible for ensuring the session is authenticated before
    invoking search or track methods.  All tidalapi exceptions are caught and
    re-raised as typed ProviderFailure subclasses.

    The session is intentionally not imported at module level; tidalapi is an
    optional runtime dependency and tests inject a fake.
    """

    def __init__(self, session: Any, loop: asyncio.AbstractEventLoop | None = None) -> None:
        self._session = session
        self._loop = loop or asyncio.get_event_loop()

    def is_authenticated(self) -> bool:
        """Return True if the underlying session reports a valid login state."""
        try:
            return bool(self._session.check_login())
        except Exception:
            return False

    def load_oauth_snapshot(self, token_type: str, access_token: str, refresh_token: str, expiry_time: int) -> None:
        """Restore a persisted OAuth snapshot into the session.

        Raises AuthenticationRequired if the session rejects the credentials.
        """
        try:
            self._session.load_oauth_session(
                token_type, access_token, refresh_token, expiry_time
            )
        except Exception as exc:
            raise AuthenticationRequired("Failed to restore OAuth session") from exc

    async def search_tracks(self, query: str, limit: int = 10) -> list[NormalizedCandidate]:
        """Search Tidal for tracks matching query and return normalized candidates."""
        if not query.strip():
            return []
        try:
            raw = await self._run_in_executor(
                lambda: self._session.search(query, models=[self._track_model()], limit=limit)
            )
        except ProviderFailure:
            raise
        except Exception as exc:
            raise classify_provider_exception(exc) from exc

        tracks = self._extract_tracks(raw)
        candidates: list[NormalizedCandidate] = []
        for track in tracks:
            try:
                candidates.append(_track_to_candidate(track))
            except MalformedResponse:
                continue
        return candidates

    async def get_track(self, track_id: int) -> TrackMeta:
        """Fetch a single track by Tidal ID and return its domain metadata."""
        try:
            track = await self._run_in_executor(
                lambda: self._session.track(track_id)
            )
        except ProviderFailure:
            raise
        except Exception as exc:
            mapped = classify_provider_exception(exc)
            if isinstance(mapped, type) and issubclass(mapped, NotFound):
                raise NotFound(f"Tidal track {track_id} not found") from exc
            raise mapped from exc
        if track is None:
            raise NotFound(f"Tidal track {track_id} not found")
        return _track_to_meta(track)

    async def get_stream_url(self, track_id: int) -> str:
        """Resolve a playback stream URL for a Tidal track ID.

        The returned URL must be consumed immediately; it is never stored.
        Raises PlaybackUnavailable if the track cannot be streamed.
        """
        try:
            url: str = await self._run_in_executor(
                lambda: self._session.track_url(track_id)
            )
        except ProviderFailure:
            raise
        except Exception as exc:
            raise PlaybackUnavailable(f"Stream unavailable for track {track_id}") from exc
        if not isinstance(url, str) or not url.startswith("https://"):
            raise PlaybackUnavailable(f"Invalid stream URL received for track {track_id}")
        return url

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _run_in_executor(self, fn: Any) -> Any:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, fn)

    def _track_model(self) -> Any:
        """Return the tidalapi Track class without importing tidalapi at module level."""
        import tidalapi.media as _media  # noqa: PLC0415 – intentional late import
        return _media.Track

    @staticmethod
    def _extract_tracks(raw: Any) -> list[Any]:
        tracks = raw.get("tracks", []) if isinstance(raw, dict) else getattr(raw, "tracks", [])
        if isinstance(tracks, list):
            return tracks
        items = getattr(tracks, "items", None)
        return items if isinstance(items, list) else []
