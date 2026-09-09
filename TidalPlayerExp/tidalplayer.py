"""
TidalPlayerExp - experimental Tidal music integration for Red Discord Bot
Features: Hi-Res Audio, Album Art, Spotify/YT Importing, MixV2, Video URLs,
          Hybrid Slash Commands, Similar Albums, UserPlaylist Mgmt, Rich UI
"""
from __future__ import annotations

import asyncio
import logging
import os
import secrets
from collections.abc import Awaitable
from collections import OrderedDict, defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from itertools import islice
from typing import Any, Callable, Deque, Dict, List, Optional, Set, Tuple
from urllib.parse import parse_qs, urlsplit

import aiohttp
import discord
from redbot.core import Config, app_commands, commands
from redbot.core.bot import Red
from redbot.core.data_manager import cog_data_path
from redbot.core.utils.menus import SimpleMenu
from redbot.core.utils.views import SetApiView

from .config_schema import COG_IDENTIFIER, GLOBAL_DEFAULTS, GUILD_DEFAULTS, SCHEMA_VERSION
from .domain.models import PageResult as _PageResult
from .domain.models import TrackMeta
from .domain.candidates import NormalizedCandidate
from .domain.matching import select_best_tidal_track, select_confident_youtube_tidal_track
from .domain.identity import normalize_identity_text, recording_signature
from .domain.normalization import (
    FILTER_REGEX, ISRC_PATTERN, SPOTIFY_ALBUM_PATTERN, SPOTIFY_PLAYLIST_PATTERN,
    SPOTIFY_TRACK_PATTERN,
    YOUTUBE_SKIP_TITLES, ensure_aware as _ensure_aware,
    format_duration, make_tidal_url, truncate, utc_now as _utc_now,
)
from .ui.embeds import (
    COLOR_BLUE, COLOR_GREEN, COLOR_PURPLE, COLOR_RED, COLOR_TEAL, Messages,
    error_embed as _error_embed, make_queue_embed, success_embed as _success_embed,
)
from .ui.controller import PlayerControllerView
from .playback.errors import PlaybackUnavailable
from .playback.interfaces import PlaybackSession
from .playback.requests import current_request, playback_request, request_is_cancelled
from .playback.models import PlaybackEntry, SourceKind, SourceReference
from .playback.ffmpeg import FFmpegSourceFactory, _default_locator as _default_ffmpeg_locator
from .playback.runtime_repair import DENO_VERSION, ManagedRuntime, RuntimeRepairError
from .playback.backend import NativePlaybackBackend
from .playback.voice_runtime import initialize_voice_runtime
from .providers.tidal_source import CompositeSourceResolver, TidalSourceResolver
from .providers.public_audio import PublicAudioResolver
from .providers.youtube_resolver import YouTubeResolver, YouTubeVideoMetadata, _deno_path, parse_youtube_api_duration
from .providers.tokens import TokenRepository, TokenService, TokenSnapshot
from .providers.urls import MalformedProviderURL, ProviderKind, ProviderURL, parse_provider_url

logging.getLogger("googleapiclient.discovery_cache").setLevel(logging.ERROR)

_CACHE_MISS = object()



try:
    import tidalapi
    try:
        from tidalapi.media import Track as TidalTrack
        TIDAL_MODELS_AVAILABLE = True
    except ImportError:
        TidalTrack = None
        TIDAL_MODELS_AVAILABLE = False
    TIDALAPI_AVAILABLE = True
except ImportError:
    TidalTrack = None
    TIDALAPI_AVAILABLE = False
    TIDAL_MODELS_AVAILABLE = False

try:
    from googleapiclient.discovery import build
    YOUTUBE_API_AVAILABLE = True
except ImportError:
    YOUTUBE_API_AVAILABLE = False

try:
    import spotipy
    from spotipy.cache_handler import MemoryCacheHandler
    from spotipy.oauth2 import SpotifyClientCredentials, SpotifyOAuth
    SPOTIFY_AVAILABLE = True
except ImportError:
    SPOTIFY_AVAILABLE = False

log = logging.getLogger("red.tidalplayerexp")

__red_end_user_data_statement__ = (
    "This cog stores Tidal and Spotify OAuth credentials globally for the bot owner. "
    "It does not store data associated with individual Discord users."
)


def _log_provider_failure(provider: str, operation: str, error: BaseException) -> None:
    """Log provider failures without formatting credential-bearing exception text."""
    log.warning("%s %s failed (%s)", provider, operation, type(error).__name__)


async def _delete_message_safe(message: discord.Message) -> None:
    """Delete a Discord message, silently ignoring all expected errors."""
    try:
        await message.delete()
    except (discord.HTTPException, discord.Forbidden, discord.NotFound):
        pass

API_SEMAPHORE_LIMIT = 5
TIDAL_EXECUTOR_WORKERS = 4
INTERACTIVE_TIMEOUT = 30
BATCH_UPDATE_INTERVAL = 10
LOGIN_CACHE_TTL = 300.0
PROGRESS_EDIT_RATELIMIT = 1.5
LOGIN_CHECK_TIMEOUT = 10.0
LOGIN_CHECK_RETRIES = 2
PAGINATION_LIMIT = 100
MAX_ITEMS = 1000
RATELIMIT_BACKOFF_BASE = 2.0
RATELIMIT_BACKOFF_MAX = 30.0
RATELIMIT_MAX_RETRIES = 4
QUEUE_PAGE_SIZE = 10
TPL_LIST_PAGE_SIZE = 15
SEARCH_BATCH_SIZE = 8
CONTROLLER_REFRESH_COOLDOWN = 3.0   # seconds between background-only controller edits
PROGRESS_SLEEP_INTERVAL = 0.0       # Provider and executor limits already pace batch work.
QUEUED_EMBED_DELETE_DELAY = 60.0    # Keep queue confirmations visible without cluttering chat.
RECOMMENDATION_SEARCH_CONCURRENCY = 1  # Leave Tidal API capacity for playback requests.
RECOMMENDATION_LOOKUP_CONCURRENCY = 2  # Reserve at least one Tidal API slot for foreground commands.
LASTFM_REQUEST_TIMEOUT = 20.0
RECENT_TRACK_HISTORY = 50
SPOTIFY_REDIRECT_URI = "http://127.0.0.1:2402/callback"
SPOTIFY_OAUTH_SCOPE = "playlist-read-private playlist-read-collaborative"
SPOTIFY_LOGIN_TTL = 600.0
YOUTUBE_MATCH_TIMEOUT = 8.0


_CACHE_CAPS: Dict[str, int] = {
    "search": 200,
    "track": 500,
    "isrc": 500,
    "album": 100,
    "playlist": 100,
    "mix": 50,
    "video": 100,
}


def _is_tidal_track(obj: Any) -> bool:
    if TIDAL_MODELS_AVAILABLE and TidalTrack is not None:
        return isinstance(obj, TidalTrack)
    return (
        hasattr(obj, "id")
        and hasattr(obj, "duration")
        and (hasattr(obj, "get_stream") or hasattr(obj, "get_url"))
    )


def _spotify_item_to_query(item: dict) -> NormalizedCandidate:
    track = item.get("item") or item.get("track") or {}
    return _spotify_album_item_to_query(track)


def _spotify_album_item_to_query(item: dict) -> NormalizedCandidate:
    isrc = (item.get("external_ids") or {}).get("isrc")
    artists = tuple(a["name"] for a in item.get("artists", []) if isinstance(a.get("name"), str) and a["name"])
    duration = item.get("duration_ms")
    return NormalizedCandidate(str(item.get("name") or ""), artists,
        isrc=isrc if isinstance(isrc, str) else None,
        duration=int(duration / 1000) if isinstance(duration, (int, float)) and duration > 0 else None,
        source="spotify")


class SpotifyLoginError(ValueError):
    """A safe, user-facing Spotify OAuth validation error."""


class SpotifyCallbackModal(discord.ui.Modal):
    def __init__(self, cog: "TidalPlayerExp", owner_id: int):
        super().__init__(title="Complete Spotify login", timeout=SPOTIFY_LOGIN_TTL)
        self.cog = cog
        self.owner_id = owner_id
        self.callback_url = discord.ui.TextInput(
            label="Redirected URL",
            placeholder="http://127.0.0.1:2402/callback?code=...&state=...",
            style=discord.TextStyle.paragraph,
            required=True,
            max_length=2000,
        )
        self.add_item(self.callback_url)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if (
            interaction.user.id != self.owner_id
            or not await self.cog.bot.is_owner(interaction.user)
        ):
            await interaction.response.send_message(
                "Only the bot owner who started this login can complete it.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            await self.cog._complete_spotify_login(
                self.owner_id,
                str(self.callback_url.value),
            )
        except SpotifyLoginError as error:
            await interaction.edit_original_response(embed=_error_embed(str(error)))
        except Exception as error:
            _log_provider_failure("Spotify", "OAuth completion", error)
            await interaction.edit_original_response(
                embed=_error_embed("Spotify authentication failed. Start the login again.")
            )
        else:
            await interaction.edit_original_response(
                embed=_success_embed("Spotify authentication successful!")
            )


class SpotifyLoginView(discord.ui.View):
    def __init__(self, cog: "TidalPlayerExp", owner_id: int, state: str):
        super().__init__(timeout=SPOTIFY_LOGIN_TTL)
        self.cog = cog
        self.owner_id = owner_id
        self.state = state
        button = discord.ui.Button(
            label="Finish Spotify login",
            style=discord.ButtonStyle.primary,
            custom_id="tidalplayer:spotify-login:v1",
        )
        button.callback = self._open_modal
        self.add_item(button)

    async def _open_modal(self, interaction: discord.Interaction) -> None:
        if (
            interaction.user.id != self.owner_id
            or not await self.cog.bot.is_owner(interaction.user)
        ):
            await interaction.response.send_message(
                "Only the bot owner who started this login can complete it.",
                ephemeral=True,
            )
            return
        pending = self.cog._spotify_login_states.get(self.owner_id)
        if (
            self.cog._spotify_login_views.get(self.owner_id) is not self
            or pending is None
            or pending[0] != self.state
        ):
            await interaction.response.send_message(
                "This Spotify login has expired. Start a new one.",
                ephemeral=True,
            )
            return
        await interaction.response.send_modal(
            SpotifyCallbackModal(self.cog, self.owner_id)
        )

    async def on_timeout(self) -> None:
        if self.cog._spotify_login_views.get(self.owner_id) is self:
            self.cog._spotify_login_views.pop(self.owner_id, None)
            pending = self.cog._spotify_login_states.get(self.owner_id)
            if pending is not None and pending[0] == self.state:
                self.cog._spotify_login_states.pop(self.owner_id, None)
        self.stop()


class TrackSelectView(discord.ui.View):
    def __init__(self, tracks: List[Any], author: discord.User, timeout: float = 30.0):
        super().__init__(timeout=timeout)
        self.tracks = tracks[:5]
        self.author = author
        self.selected: Optional[Any] = None
        self._event = asyncio.Event()
        self._timed_out = False

        for i, track in enumerate(self.tracks):
            name = getattr(track, "full_name", None) or getattr(track, "name", f"Track {i+1}")
            artist = getattr(getattr(track, "artist", None), "name", "")
            raw_label = f"{artist} \u2014 {name}" if artist else name
            btn = discord.ui.Button(
                label=truncate(raw_label, 80),
                style=discord.ButtonStyle.primary,
                custom_id=f"track_{i}",
                row=0,
            )
            btn.callback = self._make_track_callback(i)
            self.add_item(btn)

        cancel_btn = discord.ui.Button(
            label="Cancel", style=discord.ButtonStyle.danger, custom_id="cancel", row=1
        )
        cancel_btn.callback = self._cancel_callback
        self.add_item(cancel_btn)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author.id:
            await interaction.response.send_message("Not your selection.", ephemeral=True)
            return False
        return True

    def _disable_all(self) -> None:
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True

    def _make_track_callback(self, index: int):
        async def callback(interaction: discord.Interaction) -> None:
            self.selected = self.tracks[index]
            self._disable_all()
            self._event.set()
            self.stop()
            await interaction.response.defer()
        return callback

    async def _cancel_callback(self, interaction: discord.Interaction) -> None:
        self.selected = None
        self._disable_all()
        self._event.set()
        self.stop()
        await interaction.response.defer()

    async def wait_for_selection(self) -> Optional[Any]:
        try:
            await asyncio.wait_for(self._event.wait(), timeout=self.timeout)
        except asyncio.TimeoutError:
            self._timed_out = True
        return self.selected

    async def on_timeout(self) -> None:
        self._timed_out = True
        self._event.set()


class TidalHandler:
    __slots__ = (
        "bot", "tokens", "session", "_refresh_task", "api_semaphore",
        "_login_cache", "_login_cache_time", "_cache", "_inflight", "_refresh_lock", "_executor",
        "_executor_slots",
    )

    def __init__(self, bot: Red, tokens: TokenService):
        self.bot = bot
        self.tokens = tokens
        self.session: Optional[Any] = tidalapi.Session() if TIDALAPI_AVAILABLE else None
        self._refresh_task: Optional[asyncio.Task] = None
        self.api_semaphore = asyncio.Semaphore(API_SEMAPHORE_LIMIT)
        self._login_cache: Optional[bool] = None
        self._login_cache_time: float = 0.0
        self._cache: Dict[str, OrderedDict] = {}
        self._inflight: Dict[Tuple[str, str], asyncio.Task[Any]] = {}
        self._refresh_lock = asyncio.Lock()
        self._executor = ThreadPoolExecutor(
            max_workers=TIDAL_EXECUTOR_WORKERS, thread_name_prefix="tidal_io"
        )
        # A timed-out run_in_executor call keeps its worker running. Retain the
        # slot until it really finishes so retries cannot build an unbounded
        # executor backlog during a provider outage.
        self._executor_slots = asyncio.BoundedSemaphore(TIDAL_EXECUTOR_WORKERS)

    def _get_cached(self, category: str, key: str) -> Any:
        bucket = self._cache.get(category)
        if bucket is None:
            return _CACHE_MISS
        entry = bucket.get(key)
        if entry is None:
            return _CACHE_MISS
        value, expiry = entry
        now = asyncio.get_running_loop().time()
        if now > expiry:
            del bucket[key]
            return _CACHE_MISS
        bucket.move_to_end(key)
        return value

    def _set_cached(self, category: str, key: str, value: Any, ttl: float) -> None:
        if category not in self._cache:
            self._cache[category] = OrderedDict()
        bucket = self._cache[category]
        cap = _CACHE_CAPS.get(category, 200)
        if key in bucket:
            bucket.move_to_end(key)
        else:
            if len(bucket) >= cap:
                bucket.popitem(last=False)
        now = asyncio.get_running_loop().time()
        bucket[key] = (value, now + ttl)

    async def _coalesce(
        self,
        category: str,
        key: str,
        operation: Callable[[], Awaitable[Any]],
    ) -> Any:
        """Share one in-flight provider request between equivalent callers."""
        inflight_key = (category, key)
        task = self._inflight.get(inflight_key)
        if task is None:
            task = asyncio.create_task(operation(), name=f"tidalplayer-{category}")
            self._inflight[inflight_key] = task

            def _cleanup(completed: asyncio.Task[Any]) -> None:
                if self._inflight.get(inflight_key) is completed:
                    self._inflight.pop(inflight_key, None)
                # All waiters may have left; never leak an unobserved provider
                # exception (which can contain credentials) to asyncio's logger.
                if not completed.cancelled():
                    completed.exception()

            task.add_done_callback(_cleanup)
        # Do not let cancellation of one Discord command cancel the shared request.
        return await asyncio.shield(task)

    async def _run_blocking(self, func: Callable[[], Any], timeout: float = 10.0) -> Any:
        if timeout <= 0:
            raise asyncio.TimeoutError
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        await asyncio.wait_for(self._executor_slots.acquire(), timeout=timeout)
        remaining = deadline - loop.time()
        if remaining <= 0:
            self._executor_slots.release()
            raise asyncio.TimeoutError
        try:
            future = loop.run_in_executor(self._executor, func)
        except BaseException:
            self._executor_slots.release()
            raise

        def _completed(completed: asyncio.Future[Any]) -> None:
            self._executor_slots.release()
            if not completed.cancelled():
                completed.exception()

        future.add_done_callback(_completed)
        # Shielding is deliberate: cancelling the waiter must not falsely free
        # a worker while its synchronous provider request is still running.
        return await asyncio.wait_for(asyncio.shield(future), timeout=remaining)

    async def _run_with_backoff(self, func: Callable[[], Any], timeout: float = 10.0) -> Any:
        delay = RATELIMIT_BACKOFF_BASE
        last_exc: Optional[Exception] = None
        for attempt in range(RATELIMIT_MAX_RETRIES):
            try:
                return await self._run_blocking(func, timeout=timeout)
            except Exception as e:
                last_exc = e
                status = getattr(e, "status", None) or getattr(e, "status_code", None)
                if status is None and hasattr(e, "response") and e.response is not None:
                    status = getattr(e.response, "status_code", None)
                is_unauthorized = status == 401 or "401" in str(e).lower() or "unauthorized" in str(e).lower()
                if is_unauthorized:
                    log.warning("Encountered 401 Unauthorized from Tidal API. Attempting token refresh...")
                    refreshed = await self.refresh_tokens(force=True)
                    if refreshed:
                        log.info("Token refresh succeeded after 401, retrying...")
                        continue
                    else:
                        log.error("Token refresh failed after 401. Session is invalid.")
                        raise
                exc_type = type(e).__name__.lower()
                err_str = str(e).lower()
                is_ratelimit = (
                    status == 429
                    or "429" in err_str or "too many requests" in err_str
                    or "rate limit" in err_str or "ratelimit" in err_str
                    or "toomanyrequests" in exc_type or "ratelimit" in exc_type
                )
                if is_ratelimit and attempt < RATELIMIT_MAX_RETRIES - 1:
                    wait = min(delay, RATELIMIT_BACKOFF_MAX)
                    log.warning(f"Rate limited by Tidal, retrying in {wait:.1f}s (attempt {attempt + 1})")
                    await asyncio.sleep(wait)
                    delay *= 2
                else:
                    raise
        if last_exc is not None:
            raise last_exc
        raise RuntimeError("_run_with_backoff exhausted retries with no exception captured")

    def _session_token_snapshot(self) -> TokenSnapshot:
        """Validate the current session credentials before persisting them."""
        expiry = self.session.expiry_time
        snapshot = TokenSnapshot(
            token_type=self.session.token_type,
            access_token=self.session.access_token,
            refresh_token=self.session.refresh_token,
            expiry_time=int(_ensure_aware(expiry).timestamp()) if expiry else 0,
        )
        if not snapshot.is_complete:
            raise RuntimeError("Tidal returned an incomplete credential set")
        return snapshot

    async def initialize(self, creds: Dict[str, Any]) -> None:
        self._login_cache = False
        self._login_cache_time = asyncio.get_running_loop().time()
        if not self.session or not creds.get("access_token"):
            return
        try:
            expiry = (
                datetime.fromtimestamp(creds["expiry_time"], tz=timezone.utc)
                if creds.get("expiry_time")
                else None
            )
            def _load() -> TokenSnapshot:
                loaded = self.session.load_oauth_session(
                    creds["token_type"], creds["access_token"], creds["refresh_token"], expiry
                )
                if not loaded:
                    raise RuntimeError("Tidal rejected the stored session")
                return self._session_token_snapshot()

            snapshot = await self._run_blocking(_load, timeout=15.0)
            # tidalapi can refresh expired credentials while loading the session.
            if snapshot != TokenSnapshot.from_mapping(creds):
                await self.tokens.replace(snapshot)
            self._login_cache = True
            self._login_cache_time = asyncio.get_running_loop().time()
            log.info("Tidal session loaded successfully")
        except asyncio.TimeoutError:
            log.warning("Timed out loading Tidal session from stored credentials")
        except Exception as error:
            _log_provider_failure("Tidal", "session restore", error)

    async def refresh_tokens(self, *, force: bool = False) -> bool:
        """Refresh expiring credentials, or force renewal after an API rejection."""
        if not self.session:
            return False
        async with self._refresh_lock:
            if not force:
                try:
                    expiry_time = await self._run_blocking(lambda: self.session.expiry_time, timeout=5.0)
                    if expiry_time:
                        expiry_aware = _ensure_aware(expiry_time)
                        if _utc_now() + timedelta(hours=2) <= expiry_aware:
                            return True
                except Exception:
                    pass
            self._login_cache = False
            self._login_cache_time = asyncio.get_running_loop().time()
            log.info("Refreshing Tidal tokens...")
            try:
                refresh_method = getattr(self.session, "token_refresh", None)
                if not callable(refresh_method):
                    log.error("Installed tidalapi session does not expose token refresh support")
                    return False

                def _refresh() -> TokenSnapshot:
                    refresh_token = self.session.refresh_token
                    if not isinstance(refresh_token, str) or not refresh_token.strip():
                        raise RuntimeError("Tidal session has no refresh token")
                    if not refresh_method(refresh_token):
                        raise RuntimeError("Tidal rejected the token refresh")
                    return self._session_token_snapshot()

                snapshot = await self._run_blocking(_refresh, timeout=15.0)
                await self.tokens.replace(snapshot)
                self._login_cache = True
                self._login_cache_time = asyncio.get_running_loop().time()
                log.info("Tidal tokens refreshed successfully")
                return True
            except Exception as error:
                _log_provider_failure("Tidal", "token refresh", error)
                self._login_cache = False
                self._login_cache_time = asyncio.get_running_loop().time()
                return False

    def start_refresh_loop(self) -> None:
        if self._refresh_task:
            self._refresh_task.cancel()
        self._refresh_task = asyncio.create_task(self._auto_refresh_tokens())

    async def unload(self) -> None:
        tasks = set(self._inflight.values())
        if self._refresh_task is not None:
            tasks.add(self._refresh_task)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._inflight.clear()
        self._refresh_task = None
        self._executor.shutdown(wait=False)

    async def logout(self) -> None:
        """Atomically clear persisted and in-memory OAuth state."""
        async with self._refresh_lock:
            await self.tokens.logout()
            self._login_cache = False
            self._login_cache_time = asyncio.get_running_loop().time()
            self._cache.clear()
            for task in self._inflight.values():
                task.cancel()
            self._inflight.clear()
            self.session = tidalapi.Session() if TIDALAPI_AVAILABLE else None

    def invalidate_login_cache(self) -> None:
        self._login_cache = None
        self._login_cache_time = 0.0

    async def is_logged_in(self) -> bool:
        session = self.session
        if not session:
            return False
        now = asyncio.get_running_loop().time()
        if self._login_cache is not None and (now - self._login_cache_time) < LOGIN_CACHE_TTL:
            return self._login_cache
        for attempt in range(LOGIN_CHECK_RETRIES):
            if session is not self.session:
                return False
            try:
                result = bool(await self._run_blocking(session.check_login, timeout=LOGIN_CHECK_TIMEOUT))
                # Logout replaces the session, but cannot interrupt its SDK
                # worker. A late result must not authenticate the new session.
                if session is not self.session:
                    return False
                self._login_cache = result
                self._login_cache_time = asyncio.get_running_loop().time()
                return result
            except asyncio.TimeoutError:
                if session is not self.session:
                    return False
                log.warning(f"Timed out checking Tidal login (attempt {attempt + 1}/{LOGIN_CHECK_RETRIES})")
                if attempt < LOGIN_CHECK_RETRIES - 1:
                    await asyncio.sleep(2)
            except Exception:
                if session is not self.session:
                    return False
                self._login_cache = False
                self._login_cache_time = asyncio.get_running_loop().time()
                return False
        return self._login_cache if self._login_cache is not None else False

    async def _auto_refresh_tokens(self) -> None:
        await self.bot.wait_until_ready()
        while True:
            sleep_secs = 3600
            try:
                if await self.is_logged_in():
                    expiry_time = await self._run_blocking(lambda: self.session.expiry_time, timeout=5.0)
                    if expiry_time:
                        expiry_aware = _ensure_aware(expiry_time)
                        until_expiry = (expiry_aware - _utc_now()).total_seconds()
                        sleep_secs = max(60, until_expiry - 7200)
            except Exception:
                pass
            await asyncio.sleep(sleep_secs)
            try:
                if not await self.is_logged_in():
                    continue
                expiry_time = await self._run_blocking(lambda: self.session.expiry_time, timeout=5.0)
                if not expiry_time:
                    continue
                expiry_aware = _ensure_aware(expiry_time)
                if _utc_now() + timedelta(hours=2) <= expiry_aware:
                    continue
                await self.refresh_tokens()
            except Exception as error:
                _log_provider_failure("Tidal", "automatic token refresh", error)

    async def search(self, query: str, filter_remixes: bool = False) -> List[Any]:
        if not self.session:
            return []
        normalized_query = normalize_identity_text(query) or " ".join(query.casefold().split())
        cache_key = f"{normalized_query}:{filter_remixes}"
        cached = self._get_cached("search", cache_key)
        if cached is not _CACHE_MISS:
            return cached

        return await self._coalesce(
            "search",
            cache_key,
            lambda: self._search_uncached(query, filter_remixes, cache_key),
        )

    async def _search_uncached(
        self, query: str, filter_remixes: bool, cache_key: str,
    ) -> List[Any]:
        async with self.api_semaphore:
            try:
                def run_search():
                    if TIDAL_MODELS_AVAILABLE and TidalTrack is not None:
                        return self.session.search(query, models=[TidalTrack])
                    return self.session.search(query)
                result = await self._run_with_backoff(run_search, timeout=10.0)
                tracks = self._extract_tracks(result)
                filtered = self._filter_tracks(tracks) if filter_remixes else tracks
                self._set_cached("search", cache_key, filtered, 600.0)
                return filtered
            except asyncio.TimeoutError:
                log.warning("Tidal catalog search timed out")
                return []
            except Exception as error:
                _log_provider_failure("Tidal", "catalog search", error)
                return []

    async def get_track_by_isrc(self, isrc: str) -> Optional[Any]:
        if not self.session:
            return None
        cached = self._get_cached("isrc", isrc)
        if cached is not _CACHE_MISS:
            return cached

        return await self._coalesce("isrc", isrc, lambda: self._get_track_by_isrc_uncached(isrc))

    async def _get_track_by_isrc_uncached(self, isrc: str) -> Optional[Any]:
        async with self.api_semaphore:
            try:
                def _fetch():
                    if hasattr(self.session, "get_tracks_by_isrc"):
                        results = self.session.get_tracks_by_isrc(isrc)
                        if isinstance(results, (list, tuple)):
                            return results[0] if results else None
                    return _CACHE_MISS
                res = await self._run_with_backoff(_fetch, timeout=10.0)
                if res is _CACHE_MISS:
                    return None
                if res:
                    self._set_cached("isrc", isrc, res, 3600.0)
                elif res is None:
                    self._set_cached("isrc", isrc, None, 30.0)
                return res
            except Exception as error:
                _log_provider_failure("Tidal", "ISRC lookup", error)
                return None

    async def get_track(self, track_id: str) -> Optional[Any]:
        if not self.session:
            return None
        cached = self._get_cached("track", track_id)
        if cached is not _CACHE_MISS:
            return cached

        return await self._coalesce("track", track_id, lambda: self._get_track_uncached(track_id))

    async def _get_track_uncached(self, track_id: str) -> Optional[Any]:
        async with self.api_semaphore:
            try:
                res = await self._run_with_backoff(lambda: self.session.track(track_id), timeout=10.0)
                if res:
                    self._set_cached("track", track_id, res, 3600.0)
                return res
            except asyncio.TimeoutError:
                log.warning(f"Tidal get_track timeout for id {track_id}")
                return None
            except Exception as error:
                _log_provider_failure("Tidal", "track lookup", error)
                return None

    async def get_track_radio(self, track_id: str) -> List[Any]:
        """Get Tidal Track Radio candidates, retaining sparse objects by ID."""
        if not self.session or not track_id:
            return []
        cache_key = f"radio:{track_id}"
        cached = self._get_cached("mix", cache_key)
        if cached is not _CACHE_MISS:
            return cached

        return await self._coalesce(
            "track-radio", cache_key, lambda: self._get_track_radio_uncached(track_id, cache_key)
        )

    async def _get_track_radio_uncached(self, track_id: str, cache_key: str) -> List[Any]:
        async with self.api_semaphore:
            try:
                def fetch() -> Any:
                    if hasattr(self.session, "get_track_radio"):
                        return self.session.get_track_radio(track_id)
                    track = self.session.track(track_id)
                    radio_method = getattr(track, "radio", None)
                    if callable(radio_method):
                        return radio_method()
                    raise RuntimeError("Installed tidalapi exposes no Track Radio method")
                result = await self._run_with_backoff(fetch, timeout=20.0)
            except Exception as error:
                _log_provider_failure("Tidal", f"Track Radio lookup for {track_id}", error)
                return []
        if isinstance(result, (list, tuple)):
            tracks = list(result)
        else:
            tracks = list(
                getattr(result, "tracks", None)
                or getattr(result, "items", None)
                or []
            )
        tracks = [track for track in tracks if getattr(track, "id", None)][:25]
        if tracks:
            self._set_cached("mix", cache_key, tracks, 300.0)
            log.info("Tidal Track Radio returned %s candidate(s) for %s.", len(tracks), track_id)
        else:
            log.warning("Tidal Track Radio returned no usable tracks for %s.", track_id)
        return tracks
    async def get_video(self, video_id: str) -> Optional[Any]:
        return await self._coalesce("video", video_id, lambda: self._get_video_uncached(video_id))

    async def _get_video_uncached(self, video_id: str) -> Optional[Any]:
        if not self.session or not hasattr(self.session, "video"):
            return None
        cached = self._get_cached("video", video_id)
        if cached is not _CACHE_MISS:
            return cached
        async with self.api_semaphore:
            try:
                res = await self._run_with_backoff(lambda: self.session.video(video_id), timeout=10.0)
                if res:
                    self._set_cached("video", video_id, res, 3600.0)
                return res
            except Exception as error:
                _log_provider_failure("Tidal", "video lookup", error)
                return None

    async def get_album(self, album_id: str) -> Optional[Any]:
        return await self._coalesce("album", album_id, lambda: self._get_album_uncached(album_id))

    async def _get_album_uncached(self, album_id: str) -> Optional[Any]:
        if not self.session:
            return None
        cached = self._get_cached("album", album_id)
        if cached is not _CACHE_MISS:
            return cached
        async with self.api_semaphore:
            try:
                res = await self._run_with_backoff(lambda: self.session.album(album_id), timeout=10.0)
                if res:
                    self._set_cached("album", album_id, res, 1800.0)
                return res
            except Exception:
                return None

    async def get_playlist(self, playlist_id: str) -> Optional[Any]:
        return await self._coalesce("playlist", playlist_id, lambda: self._get_playlist_uncached(playlist_id))

    async def _get_playlist_uncached(self, playlist_id: str) -> Optional[Any]:
        if not self.session:
            return None
        cached = self._get_cached("playlist", playlist_id)
        if cached is not _CACHE_MISS:
            return cached
        async with self.api_semaphore:
            try:
                res = await self._run_with_backoff(lambda: self.session.playlist(playlist_id), timeout=10.0)
                if res:
                    self._set_cached("playlist", playlist_id, res, 300.0)
                return res
            except Exception:
                return None

    async def get_mix(self, mix_id: str) -> Optional[Any]:
        return await self._coalesce("mix", mix_id, lambda: self._get_mix_uncached(mix_id))

    async def _get_mix_uncached(self, mix_id: str) -> Optional[Any]:
        if not self.session:
            return None
        cached = self._get_cached("mix", mix_id)
        if cached is not _CACHE_MISS:
            return cached
        res = None
        async with self.api_semaphore:
            if hasattr(self.session, "mix_v2"):
                try:
                    res = await self._run_with_backoff(lambda: self.session.mix_v2(mix_id), timeout=10.0)
                except Exception:
                    pass
            if not res and hasattr(self.session, "mix"):
                try:
                    res = await self._run_with_backoff(lambda: self.session.mix(mix_id), timeout=10.0)
                except Exception:
                    pass
        if res:
            self._set_cached("mix", mix_id, res, 300.0)
        return res

    async def get_similar_albums(self, album: Any) -> List[Any]:
        if not album or not hasattr(album, "similar"):
            return []
        async with self.api_semaphore:
            try:
                result = await self._run_with_backoff(album.similar, timeout=10.0)
                return list(result) if result else []
            except Exception as error:
                _log_provider_failure("Tidal", "similar albums lookup", error)
                return []

    async def get_album_review(self, album: Any) -> Optional[str]:
        if not album or not hasattr(album, "review"):
            return None
        async with self.api_semaphore:
            try:
                result = await self._run_with_backoff(album.review, timeout=10.0)
                if isinstance(result, str):
                    return result
                if hasattr(result, "text"):
                    return result.text
                return str(result) if result else None
            except Exception:
                return None

    async def get_user_playlists(self) -> List[Any]:
        if not self.session or not hasattr(self.session, "user"):
            return []
        async with self.api_semaphore:
            try:
                def _fetch():
                    user = self.session.user
                    if hasattr(user, "playlists"):
                        val = user.playlists
                        return list(val() if callable(val) else val)
                    return []
                return await self._run_with_backoff(_fetch, timeout=15.0)
            except Exception as error:
                _log_provider_failure("Tidal", "user playlists lookup", error)
                return []

    async def get_user_playlist_by_id(self, playlist_id: str) -> Optional[Any]:
        if not self.session:
            return None
        try:
            pl = await self.get_playlist(playlist_id)
            if pl is None:
                return None
            creator = getattr(pl, "creator", None)
            session_user = getattr(self.session, "user", None)
            if creator is None or session_user is None:
                log.warning("Refusing playlist write: Tidal did not expose playlist ownership metadata")
                return None
            creator_id = getattr(creator, "id", None)
            user_id = getattr(session_user, "id", None)
            if creator_id is None or user_id is None:
                log.warning("Refusing playlist write: Tidal returned incomplete ownership metadata")
                return None
            if str(creator_id) != str(user_id):
                return None
            return pl
        except Exception as error:
            _log_provider_failure("Tidal", "playlist ownership lookup", error)
            return None

    async def create_user_playlist(self, name: str, description: str = "") -> Optional[Any]:
        if not self.session or not hasattr(self.session, "user"):
            return None
        async with self.api_semaphore:
            try:
                def _create():
                    user = self.session.user
                    if hasattr(user, "create_playlist"):
                        return user.create_playlist(name, description)
                    return None
                return await self._run_with_backoff(_create, timeout=15.0)
            except Exception as error:
                _log_provider_failure("Tidal", "playlist creation", error)
                return None

    async def add_track_to_playlist(self, playlist: Any, track_id: int) -> bool:
        if not playlist or not hasattr(playlist, "add"):
            return False
        async with self.api_semaphore:
            try:
                await self._run_with_backoff(lambda: playlist.add([track_id]), timeout=10.0)
                return True
            except Exception as error:
                _log_provider_failure("Tidal", "playlist track addition", error)
                return False

    async def remove_track_from_playlist(self, playlist: Any, track_id: int) -> bool:
        if not playlist or not hasattr(playlist, "remove_by_id"):
            return False
        async with self.api_semaphore:
            try:
                await self._run_with_backoff(lambda: playlist.remove_by_id(track_id), timeout=10.0)
                return True
            except Exception as error:
                _log_provider_failure("Tidal", "playlist track removal", error)
                return False

    async def get_items(self, container: Any) -> List[Any]:
        if hasattr(container, "items") and callable(container.items):
            try:
                return await self._paginate_items(container)
            except Exception as error:
                _log_provider_failure("Tidal", "paginated item fetch", error)
        def _fetch():
            if hasattr(container, "tracks"):
                val = container.tracks
                return list(islice(val() if callable(val) else val, MAX_ITEMS))
            if hasattr(container, "items"):
                val = container.items
                return list(islice(val() if callable(val) else val, MAX_ITEMS))
            return []
        async with self.api_semaphore:
            try:
                items = await self._run_with_backoff(_fetch, timeout=30.0)
            except asyncio.TimeoutError:
                log.error("Timed out extracting items from Tidal container")
                return []
            except Exception as error:
                _log_provider_failure("Tidal", "item extraction", error)
                return []
        if len(items) > MAX_ITEMS:
            log.warning(f"Truncating Tidal container from {len(items)} to {MAX_ITEMS} items")
        return items[:MAX_ITEMS]

    async def _paginate_items(self, container: Any) -> List[Any]:
        all_items: List[Any] = []
        offset = 0
        _sparse_supported: Optional[bool] = None
        while len(all_items) < MAX_ITEMS:
            async with self.api_semaphore:
                try:
                    def _fetch(o: int = offset, sparse: Optional[bool] = _sparse_supported) -> _PageResult:
                        limit = min(PAGINATION_LIMIT, MAX_ITEMS - o)
                        if sparse is False:
                            return _PageResult(
                                items=list(islice(container.items(limit=limit, offset=o), limit)),
                                sparse_supported=None,
                            )
                        try:
                            result = list(islice(container.items(limit=limit, offset=o, sparse_album=True), limit))
                            return _PageResult(items=result, sparse_supported=True)
                        except TypeError:
                            return _PageResult(
                                items=list(islice(container.items(limit=limit, offset=o), limit)),
                                sparse_supported=False,
                            )
                    page: _PageResult = await self._run_with_backoff(_fetch, timeout=25.0)
                except asyncio.TimeoutError:
                    log.error(f"Pagination timeout at offset {offset}")
                    break
                except Exception as error:
                    _log_provider_failure("Tidal", f"pagination at offset {offset}", error)
                    break
            if _sparse_supported is None and page.sparse_supported is not None:
                _sparse_supported = page.sparse_supported
            if not page.items:
                break
            all_items.extend(page.items)
            if len(page.items) < PAGINATION_LIMIT:
                break
            offset += PAGINATION_LIMIT
        return all_items[:MAX_ITEMS]

    async def get_audio_resolution(self, album_obj: Any) -> Optional[Tuple[int, int]]:
        if not album_obj or not hasattr(album_obj, "get_audio_resolution"):
            return None
        try:
            res = await self._run_blocking(album_obj.get_audio_resolution, timeout=5.0)
            if res:
                entry = res[0] if isinstance(res, (list, tuple)) and len(res) > 0 else res
                if hasattr(entry, "__iter__") and not isinstance(entry, str):
                    parts = list(entry)
                    if len(parts) >= 2:
                        return int(parts[0]), int(parts[1])
        except Exception:
            pass
        return None

    async def get_stream_url(self, track: Any) -> Optional[str]:
        """Return a current stream URL, sharing only concurrent lookups by track ID."""
        track_id = str(getattr(track, "id", "") or "")
        if not track_id:
            return await self._get_stream_url_uncached(track)
        return await self._coalesce(
            "stream-url", track_id, lambda: self._get_stream_url_uncached(track)
        )

    async def _get_stream_url_uncached(self, track: Any) -> Optional[str]:
        """Resolve a full Tidal track and return a real stream URL, never a web URL."""
        track_id = getattr(track, "id", None)
        resolution_started = asyncio.get_running_loop().time()
        if track_id:
            full_track = await self.get_track(str(track_id))
            if full_track is not None:
                track = full_track
            else:
                log.warning("Could not resolve full Tidal track object for %s.", track_id)
        async with self.api_semaphore:
            try:
                get_url = track.get_url
                url = await self._run_with_backoff(get_url, timeout=15.0)
                if url:
                    log.info(
                        "Resolved Tidal stream URL for track %s via get_url() in %.2fs.",
                        track_id,
                        asyncio.get_running_loop().time() - resolution_started,
                    )
                    return url
            except AttributeError:
                log.debug("Tidal track %s does not expose get_url(); trying get_stream fallback.", track_id)
            except Exception as error:
                _log_provider_failure("Tidal", f"get_url for track {track_id}", error)
        async with self.api_semaphore:
            try:
                def get_urls() -> List[str]:
                    stream = track.get_stream()
                    manifest = stream.get_stream_manifest()
                    if (
                        manifest.is_mpd
                        or not manifest.is_bts
                        or str(manifest.encryption_type).upper() != "NONE"
                        or manifest.encryption_key
                        or manifest.is_encrypted
                    ):
                        return []
                    return manifest.get_urls()
                urls = await self._run_with_backoff(get_urls, timeout=20.0)
                if urls:
                    log.info(
                        "Resolved Tidal stream URL for track %s via get_stream() fallback in %.2fs.",
                        track_id,
                        asyncio.get_running_loop().time() - resolution_started,
                    )
                    return urls[0]
            except asyncio.TimeoutError:
                log.warning("Tidal stream request timed out for track %s.", track_id)
            except AttributeError:
                log.warning("Tidal track %s does not expose a compatible stream URL method.", track_id)
            except Exception as error:
                _log_provider_failure("Tidal", f"get_stream for track {track_id}", error)
        log.error("No playable Tidal stream URL available for track %s.", track_id)
        return None
    def _extract_tracks(self, result: Any) -> List[Any]:
        if (t := getattr(result, "tracks", None)) is not None:
            return t if isinstance(t, list) else getattr(t, "items", [])
        if isinstance(result, dict):
            t = result.get("tracks", [])
            return t if isinstance(t, list) else getattr(t, "items", [])
        return result if isinstance(result, list) else []

    def _filter_tracks(self, tracks: List[Any]) -> List[Any]:
        if not tracks:
            return []
        return [t for t in tracks if not FILTER_REGEX.search(getattr(t, "name", "") or "")]


class TidalPlayerExp(commands.Cog):
    """Play music from Tidal with full metadata support."""

    __slots__ = (
        "bot", "config", "tidal", "sp", "yt", "_tasks", "_guild_locks",
        "_cancel_events", "_last_progress_edit", "_initialized", "_current_meta", "backend", "tokens",
        "_controller_messages", "_playback_channels", "_controller_meta", "_recent_track_ids",
        "_recent_track_signatures", "_autoplay_tasks",
        "_recommendation_cache", "_recommendation_tasks", "_recommendation_task_sources",
        "_controller_recommendation_tasks", "_controller_last_refresh", "_current_entries",
        "_recommendation_lookup_slots", "_lastfm_session", "youtube_resolver", "source_factory", "_closing", "_guild_generations",
        "_persistent_view", "_controller_views", "_stop_generations", "public_audio_resolver",
        "_spotify_auth_manager", "_spotify_refresh_token", "_spotify_login_states",
        "_spotify_login_views", "_spotify_auth_lock", "_spotify_commit_lock",
        "runtime",
    )

    def __init__(self, bot: Red):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=COG_IDENTIFIER, force_registration=True)
        self.config.register_global(**GLOBAL_DEFAULTS)
        self.config.register_guild(**GUILD_DEFAULTS)
        self.tokens = TokenService(TokenRepository(self.config))
        self.tidal = TidalHandler(bot, self.tokens)
        self.runtime = ManagedRuntime(cog_data_path(self) / "native-runtime")
        self.youtube_resolver = YouTubeResolver(deno_locator=self._native_deno_path)
        self.public_audio_resolver = PublicAudioResolver(self.youtube_resolver)
        self.source_factory = FFmpegSourceFactory(locator=self._native_ffmpeg_path)
        resolver = CompositeSourceResolver(
            TidalSourceResolver(self.tidal), self.youtube_resolver,
            public_audio=self.public_audio_resolver,
        )
        self.backend = NativePlaybackBackend(bot, resolver, self.source_factory, self)
        self._closing = False
        self._guild_generations: Dict[int, int] = defaultdict(int)
        self._stop_generations: Dict[int, int] = defaultdict(int)
        self._current_entries: Dict[int, PlaybackEntry] = {}
        self.sp: Optional[Any] = None
        self._spotify_auth_manager: Optional[Any] = None
        self._spotify_refresh_token: Optional[str] = None
        self._spotify_login_states: Dict[int, Tuple[str, float]] = {}
        self._spotify_login_views: Dict[int, SpotifyLoginView] = {}
        self._spotify_auth_lock = asyncio.Lock()
        self._spotify_commit_lock = asyncio.Lock()
        self.yt: Optional[Any] = None
        self._tasks: Set[asyncio.Task] = set()
        self._guild_locks: Dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._cancel_events: Dict[int, asyncio.Event] = {}
        self._last_progress_edit: Dict[int, float] = {}
        self._current_meta: Dict[int, TrackMeta] = {}
        self._controller_messages: Dict[int, discord.Message] = {}
        self._persistent_view: PlayerControllerView | None = None
        self._controller_views: Dict[int, PlayerControllerView] = {}
        self._playback_channels: Dict[int, discord.abc.Messageable] = {}
        self._controller_meta: Dict[int, TrackMeta] = {}
        self._recent_track_ids: Dict[int, Deque[str]] = defaultdict(
            lambda: deque(maxlen=RECENT_TRACK_HISTORY)
        )
        self._recent_track_signatures: Dict[int, Deque[str]] = defaultdict(
            lambda: deque(maxlen=RECENT_TRACK_HISTORY)
        )
        self._autoplay_tasks: Dict[int, asyncio.Task[None]] = {}
        self._recommendation_cache: Dict[int, Tuple[str, List[Any]]] = {}
        self._recommendation_tasks: Dict[int, asyncio.Task[List[Any]]] = {}
        self._recommendation_task_sources: Dict[int, str] = {}
        self._controller_recommendation_tasks: Dict[int, asyncio.Task[None]] = {}
        self._controller_last_refresh: Dict[int, float] = {}


        self._recommendation_lookup_slots = asyncio.Semaphore(RECOMMENDATION_LOOKUP_CONCURRENCY)
        self._lastfm_session: aiohttp.ClientSession | None = None
        self._initialized: bool = False

    def _native_ffmpeg_path(self) -> str:
        """Respect an administrator override, then use an explicitly repaired runtime."""
        if os.environ.get("IMAGEIO_FFMPEG_EXE"):
            return _default_ffmpeg_locator()
        return self.runtime.locate("ffmpeg") or _default_ffmpeg_locator()

    def _native_deno_path(self) -> str:
        """Find Deno without downloading or changing Downloader's libraries."""
        return self.runtime.locate("deno") or _deno_path()

    async def cog_load(self) -> None:
        if self.bot.get_cog("Audio") is not None or self.bot.get_cog("TidalPlayer") is not None:
            await self.backend.close()
            await self.tidal.unload()
            raise commands.UserFeedbackCheckFailure(
                "Unload Audio and the original TidalPlayer before loading TidalPlayerExp."
            )
        await asyncio.to_thread(initialize_voice_runtime)
        try:
            await self.runtime.cleanup(protected_paths=())
        except RuntimeRepairError as error:
            log.warning("Native runtime cleanup deferred (%s)", error.code)
        await self._migrate_config()
        await self._initialize_apis()
        self._persistent_view = PlayerControllerView(self)
        self.bot.add_view(self._persistent_view)

    async def _migrate_config(self) -> None:
        try:
            version = await self.config._schema_version()
            if version is None or version < SCHEMA_VERSION:
                await self.config.clear_raw("spotify_client_id")
                await self.config.clear_raw("spotify_client_secret")
                await self.config.clear_raw("youtube_api_key")
                await self.config._schema_version.set(SCHEMA_VERSION)
                log.info("TidalPlayerExp: config migrated to schema v3 (cleared legacy API keys)")
        except Exception as error:
            log.warning("Config migration check failed (non-fatal; %s)", type(error).__name__)

    async def cog_unload(self) -> None:
        self._closing = True
        for ev in self._cancel_events.values():
            ev.set()
        tasks = {
            *self._tasks,
            *self._autoplay_tasks.values(),
            *self._recommendation_tasks.values(),
            *self._controller_recommendation_tasks.values(),
        }
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await self._close_lastfm_session()
        for resource, cleanup in (
            ("Native runtime repair", self.runtime.close),
            ("Native voice", self.backend.close),
            ("Tidal", self.tidal.unload),
        ):
            try:
                await cleanup()
            except Exception as error:
                _log_provider_failure(resource, "cog unload", error)

        for guild_id in tuple(self._controller_views):
            self._stop_controller_view(guild_id)
        if self._persistent_view is not None:
            self._persistent_view.stop()
            self._persistent_view = None

        self.sp = None
        self._spotify_auth_manager = None
        self._spotify_refresh_token = None
        self._spotify_login_states.clear()
        for view in self._spotify_login_views.values():
            view.stop()
        self._spotify_login_views.clear()
        self.yt = None
        self._tasks.clear()
        self._guild_locks.clear()
        self._cancel_events.clear()
        self._autoplay_tasks.clear()
        self._recommendation_tasks.clear()
        self._recommendation_task_sources.clear()
        self._controller_recommendation_tasks.clear()
        self._recommendation_cache.clear()
        self._controller_messages.clear()
        self._playback_channels.clear()
        self._controller_meta.clear()
        self._recent_track_ids.clear()
        self._recent_track_signatures.clear()
        self._current_meta.clear()
        self._current_entries.clear()
        self._last_progress_edit.clear()
        self._controller_last_refresh.clear()
        log.info("TidalPlayerExp cog unloaded")

    async def _close_lastfm_session(self) -> None:
        """Close the reusable Last.fm HTTP session during cog unload."""
        session = self._lastfm_session
        self._lastfm_session = None
        if session is None or session.closed:
            return
        try:
            await session.close()
        except Exception as error:
            _log_provider_failure("Last.fm", "session close", error)

    def _stop_controller_view(self, guild_id: int) -> None:
        view = self._controller_views.pop(guild_id, None)
        if view is not None:
            view.stop()

    def _claim_batch(self, guild_id: int) -> asyncio.Event | None:
        if guild_id in self._cancel_events:
            return None
        event = asyncio.Event()
        self._cancel_events[guild_id] = event
        return event

    def _release_batch(self, guild_id: int, event: asyncio.Event) -> None:
        if self._cancel_events.get(guild_id) is event:
            self._cancel_events.pop(guild_id, None)

    async def _activate_controller_view(
        self,
        guild_id: int,
        view: PlayerControllerView,
        operation: Callable[[PlayerControllerView], Awaitable[Any]],
        *, still_current: Callable[[], Awaitable[bool]] | None = None,
    ) -> Any:
        if still_current is not None and not await still_current():
            view.stop()
            return None
        self._stop_controller_view(guild_id)
        try:
            result = await operation(view)
        except BaseException:
            view.stop()
            raise
        if still_current is not None and not await still_current():
            view.stop()
            return None
        self._controller_views[guild_id] = view
        return result

    async def cog_command_error(self, ctx: commands.Context, error: Exception) -> None:
        if isinstance(error, commands.CommandInvokeError):
            log.error(
                "Unhandled error in command %s (%s)",
                ctx.command, type(error.original).__name__,
            )
            await ctx.send(embed=_error_embed("An unexpected error occurred. Please try again later."))

    async def red_delete_data_for_user(self, *, requester: str, user_id: int) -> None:
        """This cog does not persist data tied to Discord user IDs."""
        return None

    async def cog_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        if isinstance(error, app_commands.CommandInvokeError):
            log.error(
                "Unhandled error in app command %s (%s)",
                interaction.command, type(error.original).__name__,
            )
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    embed=_error_embed("An unexpected error occurred. Please try again later."),
                    ephemeral=True,
                )
            else:
                await interaction.followup.send(
                    embed=_error_embed("An unexpected error occurred. Please try again later."),
                    ephemeral=True,
                )

    @commands.Cog.listener()
    async def on_guild_remove(self, guild: discord.Guild) -> None:
        self._stop_generations[guild.id] += 1
        cancel_event = self._cancel_events.pop(guild.id, None)
        if cancel_event is not None:
            cancel_event.set()
        self._stop_controller_view(guild.id)
        self._guild_locks.pop(guild.id, None)
        self._cancel_guild_background_tasks(guild.id)
        self._controller_messages.pop(guild.id, None)
        self._playback_channels.pop(guild.id, None)
        self._controller_meta.pop(guild.id, None)
        self._recent_track_ids.pop(guild.id, None)
        self._recent_track_signatures.pop(guild.id, None)
        self._current_meta.pop(guild.id, None)
        self._last_progress_edit.pop(guild.id, None)
        self._controller_last_refresh.pop(guild.id, None)
        self._guild_generations[guild.id] += 1
        self._current_entries.pop(guild.id, None)
        await self.backend.close_guild(guild.id)

    @commands.Cog.listener()
    async def on_cog_add(self, cog: commands.Cog) -> None:
        if getattr(cog, "qualified_name", None) == "Audio":
            for guild in self.bot.guilds:
                try:
                    await self.on_guild_remove(guild)
                except Exception as error:
                    _log_provider_failure("Native voice", "guild cleanup", error)

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: Any, after: Any) -> None:
        if member.id != getattr(self.bot.user, "id", None) or before.channel is None:
            return
        if after.channel is None:
            await self.on_guild_remove(member.guild)
            return
        session = await self.backend.get(member.guild.id)
        if session is not None and after.channel.id != session.snapshot().channel_id:
            await self.on_guild_remove(member.guild)

    async def _entry_is_current(self, guild_id: int, entry_id: str) -> bool:
        if self._closing:
            return False
        session = await self.backend.get(guild_id)
        current = session.snapshot().current if session is not None else None
        return current is not None and current.entry_id == entry_id

    async def track_started(self, guild_id: int, entry: PlaybackEntry) -> None:
        """Publish the effective entry only after playback actually starts."""
        if not await self._entry_is_current(guild_id, entry.entry_id):
            return
        previous = self._current_entries.get(guild_id)
        if previous is not None and previous.entry_id == entry.entry_id:
            return
        self._guild_generations[guild_id] += 1
        self._cancel_guild_background_tasks(guild_id)
        self._current_entries[guild_id] = entry
        self._current_meta[guild_id] = entry.meta
        self._controller_meta[guild_id] = entry.meta
        self._remember_track(guild_id, entry.meta)
        await self._resend_controller_for_track_start(guild_id=guild_id)
        if await self._entry_is_current(guild_id, entry.entry_id):
            self._schedule_controller_recommendations(guild_id)

    async def track_failed(self, guild_id: int, entry: PlaybackEntry, reason: str) -> None:
        """One sanitized error; the session owns advancement."""
        if self._closing:
            return
        channel = self._playback_channels.get(guild_id)
        if channel is not None:
            try:
                await channel.send(embed=_error_embed("Could not start this track. It was skipped."))
            except discord.HTTPException:
                log.debug("Could not send playback failure in guild %s", guild_id)

    async def queue_ended(self, guild_id: int, previous: PlaybackEntry | None) -> None:
        if self._closing:
            return
        session = await self.backend.get(guild_id)
        if session is None or session.snapshot().current or session.snapshot().queued:
            return
        generation = self._guild_generations[guild_id]
        message = self._controller_messages.pop(guild_id, None)
        self._stop_controller_view(guild_id)
        self._current_entries.pop(guild_id, None)
        self._current_meta.pop(guild_id, None)
        self._controller_meta.pop(guild_id, None)
        if message is not None:
            await _delete_message_safe(message)
        if previous is not None and generation == self._guild_generations[guild_id] and not self._closing:
            self._schedule_autoplay(guild_id, previous, generation)

    async def check_ready(self, ctx: commands.Context) -> bool:
        if not self._initialized:
            await ctx.send(embed=_error_embed(Messages.ERROR_STILL_LOADING))
            return False
        if not TIDALAPI_AVAILABLE:
            await ctx.send(embed=_error_embed(Messages.ERROR_NO_TIDALAPI))
            return False
        if not await self.tidal.is_logged_in():
            await ctx.send(embed=_error_embed(Messages.ERROR_NOT_AUTHENTICATED))
            return False
        return True

    async def _initialize_apis(self) -> None:
        t0 = asyncio.get_running_loop().time()
        snapshot = await self.tokens.restore()
        creds = snapshot.as_mapping() if snapshot else {}
        results = await asyncio.gather(
            self.tidal.initialize(creds),
            self._initialize_spotify(),
            self._initialize_youtube(),
            return_exceptions=True,
        )
        for name, r in zip(["Tidal", "Spotify", "YouTube"], results):
            if isinstance(r, Exception):
                _log_provider_failure(name, "initialization", r)
        elapsed = asyncio.get_running_loop().time() - t0
        self._initialized = True
        self.tidal.start_refresh_loop()
        log.info(f"TidalPlayerExp fully initialized in {elapsed:.2f}s")

    async def _initialize_spotify(self) -> None:
        async with self._spotify_commit_lock:
            await self._initialize_spotify_locked()

    async def _initialize_spotify_locked(self) -> None:
        """Initialize while holding the shared credential commit boundary."""
        async with self._spotify_auth_lock:
            self.sp = None
            self._spotify_auth_manager = None
            self._spotify_refresh_token = None
            if not SPOTIFY_AVAILABLE:
                return
            tokens = await self.bot.get_shared_api_tokens("spotify")
            cid = tokens.get("client_id")
            csec = tokens.get("client_secret")
            if not cid or not csec:
                return
            try:
                stored_refresh_token = tokens.get("refresh_token")
                if stored_refresh_token:
                    token_info = {
                        "access_token": "",
                        "refresh_token": stored_refresh_token,
                        "expires_at": 0,
                        "scope": SPOTIFY_OAUTH_SCOPE,
                    }
                    oauth = self._new_spotify_oauth(
                        cid,
                        csec,
                        token_info=token_info,
                    )
                    validated = await self.tidal._run_blocking(
                        lambda: oauth.validate_token(token_info),
                        timeout=15.0,
                    )
                    if isinstance(validated, dict) and validated.get("refresh_token"):
                        self.sp = await self.tidal._run_blocking(
                            lambda: spotipy.Spotify(
                                auth_manager=oauth,
                                requests_timeout=15.0,
                            ),
                            timeout=15.0,
                        )
                        self._spotify_auth_manager = oauth
                        refresh_token = str(validated["refresh_token"])
                        if refresh_token != stored_refresh_token:
                            await self.bot.set_shared_api_tokens(
                                "spotify",
                                refresh_token=refresh_token,
                            )
                        self._spotify_refresh_token = refresh_token
                        return

                self.sp = await self.tidal._run_blocking(
                    lambda: spotipy.Spotify(
                        client_credentials_manager=SpotifyClientCredentials(
                            cid,
                            csec,
                            cache_handler=MemoryCacheHandler(),
                        ),
                        requests_timeout=15.0,
                    ),
                    timeout=15.0,
                )
            except Exception as error:
                _log_provider_failure("Spotify", "client initialization", error)

    @staticmethod
    def _parse_spotify_callback(callback_url: str, expected_state: str) -> str:
        try:
            parsed = urlsplit(callback_url.strip())
            is_expected_redirect = (
                parsed.scheme == "http"
                and parsed.hostname == "127.0.0.1"
                and parsed.port == 2402
                and parsed.path == "/callback"
                and parsed.username is None
                and parsed.password is None
            )
        except ValueError as error:
            raise SpotifyLoginError("Invalid Spotify redirect URL.") from error
        if not is_expected_redirect:
            raise SpotifyLoginError("Invalid Spotify redirect URL.")

        query = parse_qs(parsed.query, keep_blank_values=True)
        if query.get("error"):
            raise SpotifyLoginError("Spotify authorization was denied.")
        states = query.get("state", [])
        if len(states) != 1 or not secrets.compare_digest(states[0], expected_state):
            raise SpotifyLoginError("Invalid Spotify OAuth state.")
        codes = query.get("code", [])
        if len(codes) != 1 or not codes[0]:
            raise SpotifyLoginError(
                "Spotify callback did not include an authorization code."
            )
        return codes[0]

    @staticmethod
    def _new_spotify_oauth(
        client_id: str,
        client_secret: str,
        *,
        state: Optional[str] = None,
        token_info: Optional[dict] = None,
    ) -> Any:
        return SpotifyOAuth(
            client_id=client_id,
            client_secret=client_secret,
            redirect_uri=SPOTIFY_REDIRECT_URI,
            state=state,
            scope=SPOTIFY_OAUTH_SCOPE,
            open_browser=False,
            requests_timeout=15.0,
            cache_handler=MemoryCacheHandler(token_info=token_info),
        )

    async def _begin_spotify_login(self, owner_id: int) -> Tuple[str, str]:
        tokens = await self.bot.get_shared_api_tokens("spotify")
        client_id = tokens.get("client_id")
        client_secret = tokens.get("client_secret")
        if not client_id or not client_secret:
            raise SpotifyLoginError("Spotify client credentials are not configured.")
        state = secrets.token_urlsafe(32)
        expires_at = asyncio.get_running_loop().time() + SPOTIFY_LOGIN_TTL
        oauth = self._new_spotify_oauth(
            client_id,
            client_secret,
            state=state,
        )
        self._spotify_login_states[owner_id] = (state, expires_at)
        return oauth.get_authorize_url(), state

    async def _complete_spotify_login(self, owner_id: int, callback_url: str) -> None:
        pending = self._spotify_login_states.get(owner_id)
        if pending is None:
            raise SpotifyLoginError("No Spotify login is pending.")
        state, expires_at = pending
        if asyncio.get_running_loop().time() >= expires_at:
            self._clear_spotify_login(owner_id)
            raise SpotifyLoginError("Spotify login expired. Start it again.")

        code = self._parse_spotify_callback(callback_url, state)
        tokens = await self.bot.get_shared_api_tokens("spotify")
        client_id = tokens.get("client_id")
        client_secret = tokens.get("client_secret")
        if not client_id or not client_secret:
            raise SpotifyLoginError("Spotify client credentials are not configured.")

        oauth = self._new_spotify_oauth(client_id, client_secret, state=state)
        token_info = await self.tidal._run_blocking(
            lambda: oauth.get_access_token(code, as_dict=True, check_cache=False),
            timeout=20.0,
        )
        if not isinstance(token_info, dict) or not token_info.get("refresh_token"):
            raise SpotifyLoginError("Spotify did not return a refreshable token.")

        # Serialize persistence and activation with logout so an in-flight
        # credential write cannot restore authorization after logout returns.
        async with self._spotify_commit_lock:
            if self._closing or self._spotify_login_states.get(owner_id) != pending:
                raise SpotifyLoginError("Spotify login is no longer pending. Start it again.")
            if asyncio.get_running_loop().time() >= expires_at:
                self._clear_spotify_login(owner_id)
                raise SpotifyLoginError("Spotify login expired. Start it again.")

            self._clear_spotify_login(owner_id)
            await self.bot.set_shared_api_tokens(
                "spotify",
                refresh_token=str(token_info["refresh_token"]),
            )
            await self._initialize_spotify_locked()
            if self._spotify_auth_manager is None or self.sp is None:
                raise RuntimeError("Spotify session could not activate after authorization.")

    def _clear_spotify_login(self, owner_id: int) -> None:
        self._spotify_login_states.pop(owner_id, None)
        view = self._spotify_login_views.pop(owner_id, None)
        if view is not None:
            view.stop()

    async def _run_spotify(
        self,
        operation: Callable[[Any], Any],
        *,
        timeout: float,
    ) -> Any:
        client = self.sp
        oauth = self._spotify_auth_manager
        if client is None:
            raise RuntimeError("Spotify client is unavailable")
        result = await self.tidal._run_blocking(
            lambda: operation(client),
            timeout=timeout,
        )

        if oauth is not None:
            try:
                async with self._spotify_commit_lock:
                    if self._closing or self.sp is not client or self._spotify_auth_manager is not oauth:
                        return result
                    token_info = oauth.cache_handler.get_cached_token()
                    if isinstance(token_info, dict) and token_info.get("refresh_token"):
                        refresh_token = str(token_info["refresh_token"])
                        if refresh_token != self._spotify_refresh_token:
                            await self.bot.set_shared_api_tokens(
                                "spotify",
                                refresh_token=refresh_token,
                            )
                            self._spotify_refresh_token = refresh_token
            except Exception as error:
                _log_provider_failure("Spotify", "token persistence", error)
        return result

    async def _initialize_youtube(self) -> None:
        self.yt = None
        if not YOUTUBE_API_AVAILABLE:
            return
        tokens = await self.bot.get_shared_api_tokens("youtube")
        key = tokens.get("api_key")
        if key:
            try:
                from .providers.google_transport import IsolatedHttpRequest
                self.yt = await self.tidal._run_blocking(
                    lambda: build("youtube", "v3", developerKey=key, cache_discovery=False,
                                  requestBuilder=IsolatedHttpRequest),
                    timeout=15.0,
                )
            except Exception as error:
                _log_provider_failure("YouTube", "client initialization", error)

    @commands.Cog.listener()
    async def on_red_api_tokens_update(self, service_name: str, api_tokens: Dict[str, str]) -> None:
        if service_name == "spotify":
            await self._initialize_spotify()
        elif service_name == "youtube":
            await self._initialize_youtube()

    def _build_meta_sync(self, track: Any) -> TrackMeta:
        full_name = getattr(track, "full_name", None)
        name = full_name or getattr(track, "name", "Unknown") or "Unknown"
        artist_obj = getattr(track, "artist", None)
        artist = getattr(artist_obj, "name", "Unknown") if artist_obj else "Unknown"
        album_obj = getattr(track, "album", None)
        album = getattr(album_obj, "name", None) if album_obj else None
        duration = int(getattr(track, "duration", 0) or 0)
        quality = getattr(track, "audio_quality", "LOSSLESS") or "LOSSLESS"
        track_id = getattr(track, "id", None)
        is_video = getattr(track, "video_quality", None) is not None
        content_type = "video" if is_video else "track"
        share_url = make_tidal_url(content_type, track_id) if track_id else None
        meta: TrackMeta = {
            "title": name, "artist": artist, "album": album, "duration": duration,
            "quality": quality, "image": None, "share_url": share_url,
            "audio_resolution": None, "track_id": track_id,
        }
        try:
            if album_obj and hasattr(album_obj, "image"):
                meta["image"] = album_obj.image(dimensions=640)
            elif album_obj and hasattr(album_obj, "cover") and album_obj.cover:
                uuid = album_obj.cover.replace("-", "/")
                meta["image"] = f"https://resources.tidal.com/images/{uuid}/640x640.jpg"
        except Exception:
            pass
        return meta

    async def _extract_meta(self, track: Any, skip_audio_res: bool = False) -> TrackMeta:
        meta = self._build_meta_sync(track)
        if meta["quality"] == "HI_RES_LOSSLESS" and not skip_audio_res:
            album_obj = getattr(track, "album", None)
            if album_obj:
                res = await self.tidal.get_audio_resolution(album_obj)
                if res:
                    bit_depth, sample_rate = res
                    khz = sample_rate // 1000 if sample_rate >= 1000 else sample_rate
                    meta["audio_resolution"] = f"HI-RES LOSSLESS ({bit_depth}-bit / {khz}kHz)"
        return meta



    @staticmethod
    def _track_signature(title: Any, artist: Any) -> str:
        """Return a stable song identity across Tidal album/version IDs."""
        return recording_signature(title, artist)

    @classmethod
    def _meta_track_signature(cls, meta: TrackMeta | None) -> str:
        values = meta or {}
        return cls._track_signature(values.get("title"), values.get("artist"))

    @classmethod
    def _tidal_track_signature(cls, track: Any) -> str:
        return cls._track_signature(
            getattr(track, "full_name", None) or getattr(track, "name", None),
            getattr(getattr(track, "artist", None), "name", None),
        )

    def _is_recent_autoplay_track(self, guild_id: int, track: Any) -> bool:
        track_id = str(getattr(track, "id", "") or "")
        signature = self._tidal_track_signature(track)
        return bool(
            (track_id and track_id in self._recent_track_ids[guild_id])
            or (signature and signature in self._recent_track_signatures[guild_id])
        )

    def _is_recent_autoplay_meta(self, guild_id: int, meta: TrackMeta) -> bool:
        track_id = str(meta.get("track_id") or "")
        signature = self._meta_track_signature(meta)
        return bool(
            (track_id and track_id in self._recent_track_ids[guild_id])
            or (signature and signature in self._recent_track_signatures[guild_id])
        )

    async def _is_current_or_queued_track(self, guild_id: int, track_id: str, signature: str) -> bool:
        session = await self.backend.get(guild_id)
        if session is None:
            return False
        snapshot = session.snapshot()
        return any(
            entry is not None and (
                (track_id and track_id == str(entry.meta.get("track_id") or ""))
                or (signature and signature == self._meta_track_signature(entry.meta))
            )
            for entry in (*snapshot.queued, snapshot.current)
        )

    def _format_duration(self, seconds: int) -> str:
        return format_duration(seconds)

    async def _get_session_for_guild(self, guild_id: int) -> PlaybackSession | None:
        return await self.backend.get(guild_id)

    async def _prepare_playback_session(self, ctx: commands.Context) -> PlaybackSession | None:
        if self._closing or ctx.guild is None:
            return None
        if request_is_cancelled(self, ctx):
            await ctx.send(embed=_error_embed("Playback request cancelled. Please try again."))
            return None
        stop_generation = self._stop_generations[ctx.guild.id]
        channel = getattr(getattr(ctx.author, "voice", None), "channel", None)
        if channel is None:
            await ctx.send(embed=_error_embed("Join a voice channel first."))
            return None
        if self.bot.get_cog("Audio") is not None:
            await ctx.send(embed=_error_embed("Unload Audio before using native playback."))
            return None
        permissions = channel.permissions_for(ctx.guild.me)
        if not permissions.connect or not permissions.speak:
            await ctx.send(embed=_error_embed("I need Connect and Speak permissions in your voice channel."))
            return None
        session = await self.backend.get(ctx.guild.id)
        if session is not None and session.snapshot().channel_id != channel.id:
            await ctx.send(embed=_error_embed("Join my voice channel to queue music."))
            return None
        if session is None and ctx.guild.voice_client is not None:
            await ctx.send(embed=_error_embed("Another cog owns this server's voice connection."))
            return None
        try:
            session = await self.backend.connect(ctx.guild, channel)
        except PlaybackUnavailable:
            await ctx.send(embed=_error_embed("Could not connect native voice. Check permissions and tidalsetup doctor."))
            return None
        if self._closing or stop_generation != self._stop_generations[ctx.guild.id]:
            await ctx.send(embed=_error_embed("Playback request cancelled. Please try again."))
            return None
        self._playback_channels[ctx.guild.id] = ctx.channel
        return session

    def _cancel_guild_background_tasks(self, guild_id: int) -> None:
        """Cancel provider work that is no longer useful after a guild stop/removal."""
        current_task = asyncio.current_task()
        for tasks in (
            self._autoplay_tasks,
            self._recommendation_tasks,
            self._controller_recommendation_tasks,
        ):
            task = tasks.pop(guild_id, None)
            if task is not None and task is not current_task:
                task.cancel()
        self._recommendation_task_sources.pop(guild_id, None)
        self._recommendation_cache.pop(guild_id, None)

    @staticmethod
    def _tidal_entry(track: Any, meta: TrackMeta, requester_id: int | None, *, kind: SourceKind | None = None) -> PlaybackEntry:
        if kind is None:
            kind = SourceKind.TIDAL_VIDEO if getattr(track, "video_quality", None) is not None else SourceKind.TIDAL
        return PlaybackEntry(secrets.token_hex(12), SourceReference(kind, str(track.id)), None, meta, requester_id)

    async def _queue_resolved_chunk(
        self, ctx: commands.Context, session: PlaybackSession,
        resolved_chunk: List[Optional[Tuple[Any, TrackMeta]]], cancel_event: asyncio.Event,
    ) -> Tuple[int, int]:
        queued = skipped = 0
        for result in resolved_chunk:
            if cancel_event.is_set() or self._closing or await self.backend.get(ctx.guild.id) is not session:
                break
            if result is None:
                skipped += 1
                continue
            track, meta = result
            try:
                entry = self._tidal_entry(track, meta, ctx.author.id)
                accepted = await session.enqueue(entry)
            except (ValueError, PlaybackUnavailable):
                accepted = False
            if accepted:
                self._guild_generations[ctx.guild.id] += 1
                queued += 1
            else:
                skipped += 1
                cancel_event.set()
                break
        return queued, skipped

    async def _admit_entry(self, ctx: commands.Context, session: PlaybackSession, entry: PlaybackEntry, *, show_embed: bool = True, stop_generation: int | None = None) -> bool:
        if (self._closing or await self.backend.get(ctx.guild.id) is not session
                or request_is_cancelled(self, ctx)
                or (stop_generation is not None and stop_generation != self._stop_generations[ctx.guild.id])):
            if show_embed:
                await ctx.send(embed=_error_embed("Playback request cancelled or voice session changed. Please try again."))
            return False
        snapshot = session.snapshot()
        was_waiting = snapshot.current is not None or bool(snapshot.queued)
        if not await session.enqueue(entry):
            if show_embed:
                await ctx.send(embed=_error_embed("The queue is full or the voice session ended."))
            return False
        self._guild_generations[ctx.guild.id] += 1
        self._playback_channels[ctx.guild.id] = ctx.channel
        # A separately posted player panel does not complete a deferred slash
        # response. Acknowledge the first slash request as well as queued tracks.
        if show_embed and (was_waiting or getattr(ctx, "interaction", None) is not None):
            try:
                message = await ctx.send(embed=self._make_queued_embed(entry.meta))
                task = asyncio.create_task(self._delete_after(message, QUEUED_EMBED_DELETE_DELAY))
                self._tasks.add(task)
            except discord.HTTPException:
                log.debug("Could not send queued confirmation in guild %s", ctx.guild.id)
        return True

    async def _load_and_queue_track(
        self, ctx: commands.Context, tidal_track: Any, show_embed: bool = True,
        skip_audio_res: bool = True, *, session: PlaybackSession | None = None,
        kind: SourceKind | None = None,
    ) -> bool:
        stop_generation = self._stop_generations[ctx.guild.id]
        if request_is_cancelled(self, ctx):
            await ctx.send(embed=_error_embed("Playback request cancelled. Please try again."))
            return False
        if session is None:
            session = await self._prepare_playback_session(ctx)
        if session is None:
            return False
        meta = await self._extract_meta(tidal_track, skip_audio_res=skip_audio_res)
        if kind is SourceKind.TIDAL_VIDEO:
            meta["share_url"] = make_tidal_url("video", tidal_track.id)
        try:
            entry = self._tidal_entry(tidal_track, meta, ctx.author.id, kind=kind)
        except (TypeError, ValueError):
            await ctx.send(embed=_error_embed(Messages.ERROR_FETCH_FAILED))
            return False
        return await self._admit_entry(ctx, session, entry, show_embed=show_embed, stop_generation=stop_generation)

    @staticmethod
    def _youtube_watch_url(video_id: str) -> str:
        return f"https://www.youtube.com/watch?v={video_id}"

    @staticmethod
    def _youtube_snippet_metadata(
        video_id: str, snippet: Any, *, duration: int | None = None,
    ) -> YouTubeVideoMetadata:
        if not isinstance(snippet, dict) or not isinstance(snippet.get("title"), str):
            raise ValueError("Invalid video metadata")
        title = snippet["title"].strip()
        channel = snippet.get("videoOwnerChannelTitle") or snippet.get("channelTitle")
        if channel is not None and not isinstance(channel, str):
            raise ValueError("Invalid video metadata")
        if not title or title.casefold() in YOUTUBE_SKIP_TITLES:
            raise ValueError("Unavailable video")
        thumbnail = None
        thumbnails = snippet.get("thumbnails")
        if isinstance(thumbnails, dict):
            for quality in ("maxres", "standard", "high", "medium", "default"):
                value = thumbnails.get(quality)
                if isinstance(value, dict) and isinstance(value.get("url"), str):
                    thumbnail = value["url"]
                    break
        return YouTubeVideoMetadata(video_id, title, channel, duration, thumbnail)

    async def _youtube_video_metadata(self, video_id: str) -> YouTubeVideoMetadata:
        reference = SourceReference(SourceKind.YOUTUBE, video_id)
        if self.yt is not None:
            try:
                response = await self.tidal._run_blocking(
                    self.yt.videos().list(part="snippet,contentDetails", id=video_id, maxResults=1).execute, timeout=15.0,
                )
                item = response["items"][0]
                return self._youtube_snippet_metadata(
                    video_id, item["snippet"],
                    duration=parse_youtube_api_duration(item.get("contentDetails", {}).get("duration")),
                )
            except asyncio.CancelledError:
                raise
            except Exception as error:
                _log_provider_failure("YouTube", "metadata API", error)
        metadata = await self.youtube_resolver.fetch_metadata(reference)
        if metadata.title.casefold() in YOUTUBE_SKIP_TITLES:
            raise ValueError("Unavailable video")
        return metadata

    @staticmethod
    def _youtube_track_meta(video: YouTubeVideoMetadata) -> TrackMeta:
        return {
            "title": video.title, "artist": video.channel or "YouTube",
            "album": None, "duration": video.duration or 0, "quality": "YouTube audio",
            "image": video.thumbnail, "share_url": f"https://www.youtube.com/watch?v={video.video_id}",
            "audio_resolution": None, "track_id": None, "source": "YouTube",
        }

    async def _youtube_entry(self, video: YouTubeVideoMetadata, requester_id: int | None) -> PlaybackEntry:
        reference = SourceReference(SourceKind.YOUTUBE, video.video_id)
        meta = self._youtube_track_meta(video)
        try:
            async with asyncio.timeout(YOUTUBE_MATCH_TIMEOUT):
                if TIDALAPI_AVAILABLE and await self.tidal.is_logged_in():
                    results = await self.tidal.search(f"{video.title} {video.channel or ''}", filter_remixes=False)
                    match = select_confident_youtube_tidal_track(video.title, video.channel or "", results)
                    if match is not None:
                        tidal_meta = await self._extract_meta(match, skip_audio_res=True)
                        return PlaybackEntry(
                            secrets.token_hex(12), SourceReference(SourceKind.TIDAL, str(match.id)),
                            reference, tidal_meta, requester_id, fallback_meta=meta,
                        )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            _log_provider_failure("Tidal", "optional YouTube matching", error)
        return PlaybackEntry(secrets.token_hex(12), reference, None, meta, requester_id)

    @playback_request()
    async def _handle_youtube_video(self, ctx: commands.Context, video_id: str) -> None:
        stop_generation = self._stop_generations[ctx.guild.id]
        session = await self._prepare_playback_session(ctx)
        if session is None:
            return
        try:
            video = await self._youtube_video_metadata(video_id)
            entry = await self._youtube_entry(video, ctx.author.id)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            _log_provider_failure("YouTube", "video metadata", error)
            await ctx.send(embed=_error_embed(Messages.ERROR_YOUTUBE_FAILED))
            return
        await self._admit_entry(ctx, session, entry, stop_generation=stop_generation)

    @playback_request()
    async def _handle_public_audio(self, ctx: commands.Context, parsed: ProviderURL) -> None:
        """Queue public SoundCloud/Bandcamp references without TIDAL authentication."""
        guild_id = ctx.guild.id
        stop_generation = self._stop_generations[guild_id]
        session = await self._prepare_playback_session(ctx)
        if session is None:
            return
        kind = SourceKind(parsed.provider.value)
        label = "SoundCloud" if kind is SourceKind.SOUNDCLOUD else "Bandcamp"
        cancel = None
        if parsed.content_type != "track":
            cancel = self._claim_batch(guild_id)
            if cancel is None:
                await ctx.send(embed=_error_embed(Messages.ERROR_BATCH_IN_PROGRESS))
                return
        try:
            if parsed.content_type == "track":
                item = await self.public_audio_resolver.fetch_metadata(
                    SourceReference(kind, parsed.identifier, secret_token=parsed.secret_token),
                )
                entry = PlaybackEntry(secrets.token_hex(12), item.reference, None, item.meta, ctx.author.id)
                await self._admit_entry(ctx, session, entry, stop_generation=stop_generation)
                return

            message = await ctx.send(embed=discord.Embed(title=f"Importing {label} collection", color=COLOR_BLUE))
            items = await self.public_audio_resolver.fetch_collection(parsed.identifier, 100)
            total = len(items)
            queued = skipped = 0
            interrupted = False
            for item in items:
                if (cancel.is_set() or self._closing or stop_generation != self._stop_generations[guild_id]
                        or await self.backend.get(guild_id) is not session):
                    interrupted = True
                    break
                entry = PlaybackEntry(secrets.token_hex(12), item.reference, None, item.meta, ctx.author.id)
                if not await self._admit_entry(ctx, session, entry, show_embed=False, stop_generation=stop_generation):
                    skipped = total - queued
                    break
                queued += 1
                if queued % SEARCH_BATCH_SIZE == 0:
                    await self._edit_progress_message(message, discord.Embed(
                        title=f"Importing {label} collection",
                        description=f"Queued {queued}/{total}. Skipped {skipped}.", color=COLOR_BLUE,
                    ))
            status = "Cancelled" if interrupted or cancel.is_set() else "Finished"
            await message.edit(embed=discord.Embed(
                title=f"{status} importing {label} collection",
                description=f"Queued {queued}/{total}. Skipped {skipped}.", color=COLOR_BLUE,
            ))
        except asyncio.CancelledError:
            raise
        except Exception as error:
            _log_provider_failure(label, "public audio import", error)
            await ctx.send(embed=_error_embed(
                f"Could not read playable audio from this {label} link. "
                "SoundCloud private tracks need a valid shared link; preview-only, "
                "premium-only, or unavailable releases cannot be played."
            ))
        finally:
            if cancel is not None:
                self._release_batch(guild_id, cancel)

    async def _lastfm_similar_tracks(
        self, artist: str, title: str, limit: int = 25,
    ) -> List[Tuple[str, str]]:
        """Get similar track names from Last.fm's public read-only API."""
        tokens = await self.bot.get_shared_api_tokens("lastfm")
        api_key = tokens.get("api_key")
        if not api_key or not artist or not title:
            return []

        params = {
            "method": "track.getsimilar", "artist": artist, "track": title,
            "limit": limit, "autocorrect": 1, "api_key": api_key, "format": "json",
        }
        try:
            session = self._lastfm_session
            if session is None or session.closed:
                session = aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=LASTFM_REQUEST_TIMEOUT)
                )
                self._lastfm_session = session
            async with session.get("https://ws.audioscrobbler.com/2.0/", params=params) as response:
                response.raise_for_status()
                payload = await response.json(content_type=None)
            entries = payload.get("similartracks", {}).get("track", [])
            if isinstance(entries, dict):
                entries = [entries]
            return [
                (str(item.get("artist", {}).get("name", "")).strip(), str(item.get("name", "")).strip())
                for item in entries
                if item.get("artist", {}).get("name") and item.get("name")
            ]
        except Exception as error:
            _log_provider_failure("Last.fm", "similar-track lookup", error)
            return []

    async def _delete_after(self, message: discord.Message, delay: float) -> None:
        """Delete a temporary message without leaving a task behind on cog unload."""
        try:
            await asyncio.sleep(delay)
            await _delete_message_safe(message)
        except asyncio.CancelledError:
            return
        finally:
            task = asyncio.current_task()
            if task is not None:
                self._tasks.discard(task)

    async def _radio_candidates(self, guild_id: int, meta: TrackMeta) -> List[Any]:
        """Resolve Last.fm similar tracks without starving foreground Tidal work."""
        async with self._recommendation_lookup_slots:
            return await self._radio_candidates_limited(guild_id, meta)

    async def _radio_candidates_limited(self, guild_id: int, meta: TrackMeta) -> List[Any]:
        """Resolve one background recommendation workload to Tidal catalog tracks."""
        current_id = str(meta.get("track_id") or "")
        current_title = str(meta.get("title") or "").casefold().strip()
        current_artist = str(meta.get("artist") or "").casefold().strip()
        seen_ids = set(self._recent_track_ids[guild_id])
        seen_signatures = set(self._recent_track_signatures[guild_id])
        if current_id:
            seen_ids.add(current_id)
        if current_signature := self._meta_track_signature(meta):
            seen_signatures.add(current_signature)
        pairs = await self._lastfm_similar_tracks(
            str(meta.get("artist") or ""), str(meta.get("title") or ""), limit=25,
        )
        candidates: List[Any] = []
        used_ids: Set[str] = set()
        used_signatures: Set[str] = set()
        # Suggestions are background work. Keep spare Tidal API permits for an
        # interactive play/queue request instead of scheduling all 25 at once.
        for offset in range(0, len(pairs), RECOMMENDATION_SEARCH_CONCURRENCY):
            batch = pairs[offset:offset + RECOMMENDATION_SEARCH_CONCURRENCY]
            searches = await asyncio.gather(
                *(self.tidal.search(f"{artist} {title}", filter_remixes=False) for artist, title in batch),
                return_exceptions=True,
            )
            for (artist, title), results in zip(batch, searches):
                if isinstance(results, Exception):
                    _log_provider_failure("Tidal", "suggestion lookup", results)
                    continue
                if not results:
                    continue
                track = select_best_tidal_track(NormalizedCandidate(title, (artist,), source="lastfm"), results)
                if track is None:
                    continue
                track_id = str(getattr(track, "id", "") or "")
                found_title = str(getattr(track, "name", "") or "").casefold().strip()
                found_artist = str(getattr(getattr(track, "artist", None), "name", "") or "").casefold().strip()
                signature = self._tidal_track_signature(track)
                if (
                    not track_id
                    or track_id in seen_ids
                    or track_id in used_ids
                    or (signature and (signature in seen_signatures or signature in used_signatures))
                ):
                    continue
                if found_title == current_title and found_artist == current_artist:
                    continue
                used_ids.add(track_id)
                if signature:
                    used_signatures.add(signature)
                candidates.append(track)
                if len(candidates) >= 25:
                    break
            if len(candidates) >= 25:
                break
        if candidates:
            log.info("Last.fm produced %s Tidal suggestion(s) for guild %s.", len(candidates), guild_id)
            return candidates
        log.info("Last.fm returned no usable Tidal matches; using Tidal search fallback.")
        fallback_query = f"{meta.get('artist', '')} {meta.get('title', '')}".strip()
        fallback = await self.tidal.search(fallback_query, filter_remixes=False)
        fallback_candidates: List[Any] = []
        for track in fallback:
            track_id = str(getattr(track, "id", "") or "")
            signature = self._tidal_track_signature(track)
            if (
                not track_id
                or track_id in seen_ids
                or track_id in used_ids
                or (signature and (signature in seen_signatures or signature in used_signatures))
            ):
                continue
            used_ids.add(track_id)
            if signature:
                used_signatures.add(signature)
            fallback_candidates.append(track)
            if len(fallback_candidates) >= 25:
                break
        return fallback_candidates

    @staticmethod
    def _recommendation_source(meta: TrackMeta | None) -> str:
        """Return the stable ID used to keep suggestions tied to one track."""
        values = meta or {}
        track_id = str(values.get("track_id") or "")
        if track_id:
            return f"id:{track_id}"
        title = str(values.get("title") or "").casefold().strip()
        artist = str(values.get("artist") or "").casefold().strip()
        return f"text:{artist}\x00{title}" if title or artist else ""

    def _cached_recommendations(self, guild_id: int, meta: TrackMeta | None) -> List[Any]:
        source = self._recommendation_source(meta)
        cached = self._recommendation_cache.get(guild_id)
        if not source or cached is None or cached[0] != source:
            return []
        return cached[1]

    def _recommendations_cached(self, guild_id: int, meta: TrackMeta | None) -> bool:
        source = self._recommendation_source(meta)
        cached = self._recommendation_cache.get(guild_id)
        return bool(source and cached is not None and cached[0] == source)

    async def _get_recommendations(self, guild_id: int, meta: TrackMeta) -> List[Any]:
        """Deduplicate recommendation lookups for the same playing track."""
        source = self._recommendation_source(meta)
        if not source:
            return []
        if self._recommendations_cached(guild_id, meta):
            return self._cached_recommendations(guild_id, meta)

        task = self._recommendation_tasks.get(guild_id)
        if task is None or task.done() or self._recommendation_task_sources.get(guild_id) != source:
            if task is not None and not task.done():
                task.cancel()
            task = asyncio.create_task(
                self._radio_candidates(guild_id, meta),
                name=f"tidalplayer-recommendations-{guild_id}-{source}",
            )

            def _consume_result(completed: asyncio.Task[list[Any]]) -> None:
                # A cancelled final waiter may leave this task to finish alone.
                if not completed.cancelled():
                    completed.exception()

            task.add_done_callback(_consume_result)
            self._recommendation_tasks[guild_id] = task
            self._recommendation_task_sources[guild_id] = source

        try:
            recommendations = await asyncio.shield(task)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            log.warning("Could not load recommendations for guild %s (%s)", guild_id, type(error).__name__)
            recommendations = []
        finally:
            if task.done() and self._recommendation_tasks.get(guild_id) is task:
                self._recommendation_tasks.pop(guild_id, None)
                self._recommendation_task_sources.pop(guild_id, None)

        self._recommendation_cache[guild_id] = (source, recommendations)
        return recommendations

    def _schedule_controller_recommendations(self, guild_id: int) -> None:
        """Populate the controller suggestions after its first, immediate send."""
        meta = self._controller_meta.get(guild_id) or self._current_meta.get(guild_id)
        source = self._recommendation_source(meta)
        if not source or self._recommendations_cached(guild_id, meta):
            return

        existing = self._controller_recommendation_tasks.get(guild_id)
        if existing is not None and not existing.done():
            if existing.get_name().endswith(f"-{source}"):
                return
            existing.cancel()
        task = asyncio.create_task(
            self._refresh_controller_recommendations(guild_id, source),
            name=f"tidalplayer-controller-recommendations-{guild_id}-{source}",
        )
        self._controller_recommendation_tasks[guild_id] = task

    async def _refresh_controller_recommendations(self, guild_id: int, source: str) -> None:
        generation = self._stop_generations[guild_id]
        entry = self._current_entries.get(guild_id)
        try:
            meta = self._controller_meta.get(guild_id) or self._current_meta.get(guild_id)
            if self._recommendation_source(meta) != source or meta is None:
                return
            if not await self._get_recommendations(guild_id, meta):
                return
            current = self._controller_meta.get(guild_id) or self._current_meta.get(guild_id)
            if self._recommendation_source(current) != source:
                return
            message = self._controller_messages.get(guild_id)
            if message is None:
                return

            async def still_current() -> bool:
                current_meta = self._controller_meta.get(guild_id) or self._current_meta.get(guild_id)
                return (
                    not self._closing
                    and generation == self._stop_generations[guild_id]
                    and self._current_entries.get(guild_id) is entry
                    and self._controller_messages.get(guild_id) is message
                    and self._recommendation_source(current_meta) == source
                )

            player = await self._get_session_for_guild(guild_id)
            view = await self._controller_view(
                guild_id, paused=player.snapshot().paused if player else False,
            )
            await self._activate_controller_view(
                guild_id, view, lambda active_view: message.edit(view=active_view),
                still_current=still_current,
            )
        except asyncio.CancelledError:
            raise
        except (discord.HTTPException, discord.Forbidden, discord.NotFound):
            log.debug("Could not update controller recommendations for guild %s", guild_id)
        except Exception as error:
            log.warning("Could not update controller recommendations for guild %s (%s)", guild_id, type(error).__name__)
        finally:
            task = asyncio.current_task()
            if task is not None and self._controller_recommendation_tasks.get(guild_id) is task:
                self._controller_recommendation_tasks.pop(guild_id, None)

    async def _controller_view(
        self, guild_id: int, paused: bool = False,
    ) -> PlayerControllerView:
        meta = self._controller_meta.get(guild_id) or self._current_meta.get(guild_id)
        recommendations = self._cached_recommendations(guild_id, meta)
        autoplay_enabled = await self.config.guild_from_id(guild_id).autoplay_enabled()
        return PlayerControllerView(
            self, meta=meta, recommendations=recommendations,
            autoplay_enabled=autoplay_enabled, paused=paused,
        )

    def _make_queued_embed(self, meta: TrackMeta) -> discord.Embed:
        """Return a compact embed confirming a track was added to the queue."""
        from .ui.embeds import make_queue_embed
        return make_queue_embed(meta)

    async def _resend_controller_for_track_start(
        self, *, guild_id: int, ctx: commands.Context | None = None,
    ) -> bool:
        entry = self._current_entries.get(guild_id)
        if entry is None or not await self._entry_is_current(guild_id, entry.entry_id):
            return False
        previous = self._controller_messages.pop(guild_id, None)
        self._stop_controller_view(guild_id)
        channel = self._playback_channels.get(guild_id)
        if previous is not None:
            await _delete_message_safe(previous)
        if channel is None or not await self._entry_is_current(guild_id, entry.entry_id):
            return False
        session = await self.backend.get(guild_id)
        if session is None:
            return False
        view = await self._controller_view(guild_id, session.snapshot().paused)
        if not await self._entry_is_current(guild_id, entry.entry_id):
            view.stop()
            return False
        try:
            message = await (ctx.send(view=view) if ctx is not None else channel.send(view=view))
        except discord.HTTPException:
            view.stop()
            return False
        if not await self._entry_is_current(guild_id, entry.entry_id):
            view.stop()
            await _delete_message_safe(message)
            return False
        self._stop_controller_view(guild_id)
        self._controller_views[guild_id] = view
        self._controller_messages[guild_id] = message
        return True

    def _remember_track(self, guild_id: int, meta: TrackMeta) -> None:
        track_id = str(meta.get("track_id") or "")
        recent_ids = self._recent_track_ids[guild_id]
        if track_id and (not recent_ids or recent_ids[-1] != track_id):
            recent_ids.append(track_id)
        signature = self._meta_track_signature(meta)
        recent_signatures = self._recent_track_signatures[guild_id]
        if signature and (not recent_signatures or recent_signatures[-1] != signature):
            recent_signatures.append(signature)

    async def controller_skip(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            return
        await interaction.response.defer()
        session = await self.backend.get(interaction.guild.id)
        if session is None or not await session.skip():
            await interaction.followup.send("Nothing is playing.", ephemeral=True)

    async def can_control_player(self, interaction: discord.Interaction) -> bool:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            return False
        session = await self.backend.get(interaction.guild.id)
        user_channel = getattr(getattr(interaction.user, "voice", None), "channel", None)
        return session is not None and user_channel is not None and session.snapshot().channel_id == user_channel.id

    async def can_change_guild_settings(self, interaction: discord.Interaction) -> bool:
        """Allow bot owners or members with Manage Guild to change shared settings."""
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            return False
        if await self.bot.is_owner(interaction.user):
            return True
        permissions = getattr(interaction.user, "guild_permissions", None)
        return bool(getattr(permissions, "manage_guild", False))

    async def _refresh_controller(
        self, guild_id: int, interaction: discord.Interaction | None = None,
    ) -> None:
        now = asyncio.get_running_loop().time()
        if interaction is None:
            last = self._controller_last_refresh.get(guild_id, 0.0)
            if now - last < CONTROLLER_REFRESH_COOLDOWN:
                return
            self._controller_last_refresh[guild_id] = now
        generation = self._stop_generations[guild_id]
        player = await self._get_session_for_guild(guild_id)
        entry = player.snapshot().current if player else None
        entry_id = entry.entry_id if entry else None
        message = self._controller_messages.get(guild_id)

        async def still_current() -> bool:
            if self._closing or generation != self._stop_generations[guild_id]:
                return False
            current_player = await self._get_session_for_guild(guild_id)
            if current_player is not player:
                return False
            current = current_player.snapshot().current if current_player else None
            return (current.entry_id if current else None) == entry_id

        paused = player.snapshot().paused if player else False
        view = await self._controller_view(guild_id, paused)
        if not await still_current():
            view.stop()
            return
        if interaction is not None:
            if interaction.response.is_done():
                await self._activate_controller_view(
                    guild_id,
                    view,
                    lambda active_view: interaction.edit_original_response(
                        view=active_view
                    ),
                    still_current=still_current,
                )
            else:
                await self._activate_controller_view(
                    guild_id,
                    view,
                    lambda active_view: interaction.response.edit_message(
                        view=active_view
                    ),
                    still_current=still_current,
                )
            if not await still_current():
                return
            if interaction.message is not None:
                self._controller_messages[guild_id] = interaction.message
            self._controller_last_refresh[guild_id] = now
        elif message is not None:
            try:
                await self._activate_controller_view(
                    guild_id, view, lambda active_view: message.edit(view=active_view),
                    still_current=still_current,
                )
            except (discord.HTTPException, discord.Forbidden, discord.NotFound):
                pass


    async def controller_toggle_autoplay(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            return
        if not await self.can_change_guild_settings(interaction):
            await interaction.response.send_message(
                "You need the Manage Server permission to change autoplay.", ephemeral=True
            )
            return
        await interaction.response.defer()
        setting = self.config.guild(interaction.guild).autoplay_enabled
        enabled = not await setting()
        await setting.set(enabled)
        log.info("Autoplay %s for guild %s", "enabled" if enabled else "disabled", interaction.guild.id)
        await self._refresh_controller(interaction.guild.id, interaction)

    async def controller_toggle_pause(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            return
        await interaction.response.defer()
        session = await self.backend.get(interaction.guild.id)
        if session is None or not await session.set_paused(not session.snapshot().paused):
            await interaction.followup.send("No active player is available.", ephemeral=True)
            return
        await self._refresh_controller(interaction.guild.id, interaction)

    async def controller_stop(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            return
        guild_id = interaction.guild.id
        await interaction.response.defer()
        self._stop_generations[guild_id] += 1
        self._guild_generations[guild_id] += 1
        event = self._cancel_events.get(guild_id)
        if event is not None:
            event.set()
        self._cancel_guild_background_tasks(guild_id)
        session = await self.backend.get(guild_id)
        if session is not None:
            await session.stop(clear_queue=True)
        self._current_entries.pop(guild_id, None)
        self._current_meta.pop(guild_id, None)
        self._controller_meta.pop(guild_id, None)
        old_message = self._controller_messages.pop(guild_id, None)
        self._stop_controller_view(guild_id)
        if old_message is not None:
            await _delete_message_safe(old_message)
        await interaction.followup.send("⏹ Playback stopped. Queue cleared.", ephemeral=True)

    async def queue_recommendation(self, interaction: discord.Interaction, tidal_track: Any) -> bool:
        if interaction.guild is None:
            return False
        if not interaction.response.is_done():
            await interaction.response.defer()
        guild_id = interaction.guild.id
        stop_generation = self._stop_generations[guild_id]
        session = await self.backend.get(guild_id)
        if session is None:
            return False
        selected_id = str(getattr(tidal_track, "id", "") or "")
        signature = self._tidal_track_signature(tidal_track)
        if not selected_id or await self._is_current_or_queued_track(guild_id, selected_id, signature):
            return False
        try:
            meta = await self._extract_meta(tidal_track, skip_audio_res=True)
            entry = self._tidal_entry(tidal_track, meta, interaction.user.id)
            if await self.backend.get(guild_id) is not session or self._closing:
                return False
            if await self._is_current_or_queued_track(guild_id, selected_id, signature):
                return False
            if self._closing or stop_generation != self._stop_generations[guild_id]:
                return False
            if not await session.enqueue(entry):
                return False
            self._guild_generations[guild_id] += 1
            message = await interaction.followup.send(embed=make_queue_embed(meta), wait=True)
            if message is not None:
                task = asyncio.create_task(self._delete_after(message, QUEUED_EMBED_DELETE_DELAY))
                self._tasks.add(task)
            return True
        except asyncio.CancelledError:
            raise
        except (PlaybackUnavailable, ValueError):
            return False
        except discord.HTTPException:
            return True

    async def _autoplay_allowed(self, guild_id: int, session: PlaybackSession, generation: int) -> bool:
        if self._closing or generation != self._guild_generations[guild_id]:
            return False
        if not await self.config.guild_from_id(guild_id).autoplay_enabled():
            return False
        if await self.backend.get(guild_id) is not session:
            return False
        snapshot = session.snapshot()
        return not snapshot.current and not snapshot.queued and generation == self._guild_generations[guild_id]

    async def _run_autoplay(self, guild_id: int, previous: PlaybackEntry, generation: int) -> None:
        try:
            session = await self.backend.get(guild_id)
            if session is None or not await self._autoplay_allowed(guild_id, session, generation):
                return
            if not TIDALAPI_AVAILABLE or not await self.tidal.is_logged_in():
                return
            candidates = await self._get_recommendations(guild_id, previous.meta)
            for track in candidates:
                if not await self._autoplay_allowed(guild_id, session, generation):
                    return
                if self._is_recent_autoplay_track(guild_id, track):
                    continue
                meta = await self._extract_meta(track, skip_audio_res=True)
                if not await self._autoplay_allowed(guild_id, session, generation):
                    return
                entry = self._tidal_entry(track, meta, None)
                if await session.enqueue(entry):
                    return
        except asyncio.CancelledError:
            raise
        except Exception as error:
            _log_provider_failure("Tidal", "autoplay", error)
        finally:
            if self._autoplay_tasks.get(guild_id) is asyncio.current_task():
                self._autoplay_tasks.pop(guild_id, None)

    def _schedule_autoplay(self, guild_id: int, previous: PlaybackEntry, generation: int) -> None:
        task = self._autoplay_tasks.get(guild_id)
        if task is None or task.done():
            self._autoplay_tasks[guild_id] = asyncio.create_task(
                self._run_autoplay(guild_id, previous, generation),
                name=f"tidalplayerexp-autoplay-{guild_id}",
            )

    async def _interactive_select(self, ctx: commands.Context, tracks: List[Any]) -> Optional[Any]:
        if not tracks:
            return None
        top = tracks[:5]
        desc = []
        for i, t in enumerate(top):
            name = getattr(t, "full_name", None) or getattr(t, "name", "Unknown")
            artist_obj = getattr(t, "artist", None)
            artist = getattr(artist_obj, "name", "Unknown") if artist_obj else "Unknown"
            album_obj = getattr(t, "album", None)
            album = getattr(album_obj, "name", None) if album_obj else None
            dur = self._format_duration(int(getattr(t, "duration", 0) or 0))
            line = f"**{i + 1}.** {name} \u2014 {artist}"
            if album:
                line += f" *({album})*"
            line += f" `[{dur}]`"
            desc.append(line)
        embed = discord.Embed(title="Select a Track", description="\n".join(desc), color=COLOR_BLUE)
        view = TrackSelectView(top, ctx.author, timeout=float(INTERACTIVE_TIMEOUT))
        msg = await ctx.send(embed=embed, view=view)
        selected = await view.wait_for_selection()
        try:
            await msg.delete()
        except Exception:
            pass
        if view._timed_out:
            await ctx.send(embed=_error_embed(Messages.ERROR_TIMEOUT))
        return selected

    async def _edit_progress_message(self, msg: discord.Message, embed: discord.Embed) -> None:
        guild_id = msg.guild.id if msg.guild else msg.id
        now = asyncio.get_running_loop().time()
        if now - self._last_progress_edit.get(guild_id, 0.0) < PROGRESS_EDIT_RATELIMIT:
            return
        try:
            await msg.edit(embed=embed)
            self._last_progress_edit[guild_id] = now
        except Exception:
            pass

    async def _fetch_all_spotify_tracks(self, playlist_id: str) -> List[Any]:
        all_items: List[Any] = []
        seen_next: set[str] = set()
        offset = 0
        while offset < MAX_ITEMS:
            resp = await self._run_spotify(
                lambda client, o=offset: client.playlist_items(
                    playlist_id, limit=100, offset=o,
                    fields="items(item(name,artists(name),external_ids)),next",
                ),
                timeout=20.0,
            )
            if not isinstance(resp, dict):
                break
            page = resp.get("items")
            if not isinstance(page, list):
                break
            all_items.extend(
                item
                for item in page
                if isinstance(item, dict) and (item.get("item") or item.get("track"))
            )
            next_url = resp.get("next")
            if (
                not isinstance(next_url, str)
                or not next_url
                or next_url in seen_next
            ):
                break
            seen_next.add(next_url)
            offset += 100
        return all_items[:MAX_ITEMS]

    async def _fetch_all_spotify_album_tracks(self, album_id: str, album_meta: dict | None = None) -> Tuple[List[Any], str]:
        all_items: List[Any] = []
        album_name = album_id
        try:
            alb = album_meta if album_meta is not None else await self._run_spotify(
                lambda client: client.album(album_id),
                timeout=15.0,
            )
            if not isinstance(alb, dict):
                return [], album_name
            album_name = alb.get("name", album_id)
            tracks = alb.get("tracks", {})
            if not isinstance(tracks, dict):
                return [], album_name
            page = tracks.get("items")
            if not isinstance(page, list):
                return [], album_name
            all_items.extend(item for item in page if isinstance(item, dict))
            next_url = tracks.get("next")
            seen_next_urls: set[str] = set()
            max_pages = (MAX_ITEMS + PAGINATION_LIMIT - 1) // PAGINATION_LIMIT
            pages = 1
            while (
                isinstance(next_url, str)
                and next_url
                and next_url not in seen_next_urls
                and len(all_items) < MAX_ITEMS
                and pages < max_pages
            ):
                seen_next_urls.add(next_url)
                resp = await self._run_spotify(
                    lambda client, u=next_url: client._get(u),
                    timeout=20.0,
                )
                pages += 1
                if not isinstance(resp, dict):
                    break
                page = resp.get("items")
                if not isinstance(page, list):
                    break
                all_items.extend(item for item in page if isinstance(item, dict))
                next_url = resp.get("next")
        except Exception as error:
            _log_provider_failure("Spotify", "album fetch", error)
        return all_items[:MAX_ITEMS], album_name

    async def _fetch_all_youtube_tracks(self, playlist_id: str) -> List[Any]:
        all_items: List[Any] = []
        page_token: Optional[str] = None
        seen_page_tokens: set[str] = set()
        raw_count = 0
        while raw_count < MAX_ITEMS:
            if page_token:
                if page_token in seen_page_tokens:
                    log.warning(
                        "YouTube repeated a playlist page token for playlist %s; stopping import.",
                        playlist_id,
                    )
                    break
                seen_page_tokens.add(page_token)
            kwargs: Dict[str, Any] = {"part": "snippet", "playlistId": playlist_id, "maxResults": 50}
            if page_token:
                kwargs["pageToken"] = page_token
            resp = await self.tidal._run_blocking(
                self.yt.playlistItems().list(**kwargs).execute, timeout=20.0
            )
            if not isinstance(resp, dict):
                log.warning("YouTube returned a malformed playlist response for playlist %s.", playlist_id)
                break
            items = resp.get("items", [])
            if not isinstance(items, list):
                log.warning("YouTube returned malformed playlist items for playlist %s.", playlist_id)
                break
            remaining = MAX_ITEMS - raw_count
            raw_count += max(1, len(items))
            for item in items[:remaining]:
                if not isinstance(item, dict):
                    continue
                snippet = item.get("snippet")
                if not isinstance(snippet, dict):
                    continue
                title = str(snippet.get("title") or "").casefold()
                if title not in YOUTUBE_SKIP_TITLES:
                    all_items.append(item)
            next_page_token = resp.get("nextPageToken")
            if not isinstance(next_page_token, str) or not next_page_token or len(all_items) >= MAX_ITEMS:
                break
            page_token = next_page_token
        return all_items[:MAX_ITEMS]

    async def _resolve_and_extract(
        self,
        item: Any,
        item_processor: Callable[[Any], Any],
        filter_remixes: bool,
    ) -> Optional[Tuple[Any, TrackMeta]]:
        try:
            query = item_processor(item)
            if not query:
                return None
            track = None
            if _is_tidal_track(query):
                track = query
            else:
                if isinstance(query, NormalizedCandidate) and query.isrc:
                    track = await self.tidal.get_track_by_isrc(query.isrc)
                if isinstance(query, str) and ISRC_PATTERN.match(query):
                    isrc = ISRC_PATTERN.match(query).group(1).upper()
                    track = await self.tidal.get_track_by_isrc(isrc)
                if not track:
                    results = await self.tidal.search(query.query if isinstance(query, NormalizedCandidate) else query, filter_remixes=filter_remixes)
                    if results:
                        track = select_best_tidal_track(query, results)
            if not track:
                return None
            meta = await self._extract_meta(track, skip_audio_res=True)
            return track, meta
        except Exception as error:
            _log_provider_failure("Tidal", "batch track resolution", error)
            return None

    @playback_request(batch=True)
    async def _process_track_list(
        self,
        ctx: commands.Context,
        items: List[Any],
        name: str,
        item_processor: Callable[[Any], Any],
        color: discord.Color = discord.Color.blue(),
        thumbnail_url: Optional[str] = None,
    ) -> None:
        if not items:
            await ctx.send(embed=_error_embed(Messages.ERROR_NO_TRACKS_FOUND))
            return
        if not await self.check_ready(ctx):
            return
        filter_remixes = await self.config.guild(ctx.guild).filter_remixes()
        player = await self._prepare_playback_session(ctx)
        if not player:
            return
        guild_id = ctx.guild.id
        request = current_request(self, ctx)
        cancel_event = request.batch if request is not None else self._claim_batch(guild_id)
        if cancel_event is None:
            await ctx.send(embed=_error_embed(Messages.ERROR_BATCH_IN_PROGRESS))
            return
        trunc_name = truncate(name, 50)
        total = len(items)
        initial_embed = discord.Embed(
            title=Messages.PROGRESS_QUEUEING.format(name=trunc_name, count=total), color=color
        )
        if thumbnail_url:
            initial_embed.set_thumbnail(url=thumbnail_url)
        try:
            pmsg = await ctx.send(embed=initial_embed)
        except BaseException:
            self._release_batch(guild_id, cancel_event)
            raise
        queued, skipped, last_up = 0, 0, 0
        try:
            for chunk_start in range(0, total, SEARCH_BATCH_SIZE):
                if cancel_event.is_set():
                    break
                if await self.backend.get(guild_id) is not player or self._closing:
                    break
                chunk_items = items[chunk_start:chunk_start + SEARCH_BATCH_SIZE]
                tasks = [
                    self._resolve_and_extract(item, item_processor, filter_remixes)
                    for item in chunk_items
                ]
                resolved_chunk = await asyncio.gather(*tasks)
                chunk_queued, chunk_skipped = await self._queue_resolved_chunk(
                    ctx, player, list(resolved_chunk), cancel_event
                )
                queued += chunk_queued
                skipped += chunk_skipped
                current_count = min(chunk_start + len(chunk_items), total)
                if current_count - last_up >= BATCH_UPDATE_INTERVAL or current_count == total:
                    upd = discord.Embed(
                        title=Messages.PROGRESS_QUEUEING.format(name=trunc_name, count=total),
                        description=Messages.SUCCESS_PARTIAL_QUEUE.format(
                            queued=queued, total=total, skipped=skipped
                        ),
                        color=color,
                    )
                    if thumbnail_url:
                        upd.set_thumbnail(url=thumbnail_url)
                    await self._edit_progress_message(pmsg, upd)
                    last_up = current_count
                if PROGRESS_SLEEP_INTERVAL:
                    await asyncio.sleep(PROGRESS_SLEEP_INTERVAL)
            final = discord.Embed(
                title=Messages.SUCCESS_PARTIAL_QUEUE.format(queued=queued, total=total, skipped=skipped),
                description=f"Source: {truncate(name, 100)}",
                color=color,
            )
            if thumbnail_url:
                final.set_thumbnail(url=thumbnail_url)
            try:
                await pmsg.edit(embed=final)
            except Exception:
                pass
        except Exception as error:
            _log_provider_failure("Tidal", "batch queue processing", error)
            try:
                await pmsg.edit(embed=_error_embed(Messages.ERROR_FETCH_FAILED))
            except Exception:
                pass
        finally:
            self._release_batch(guild_id, cancel_event)

    @playback_request()
    async def _handle_track(self, ctx: commands.Context, tid: str) -> None:
        t = await self.tidal.get_track(tid)
        if t:
            await self._load_and_queue_track(ctx, t)
        else:
            await ctx.send(embed=_error_embed(Messages.ERROR_NO_TRACKS_FOUND))

    @playback_request()
    async def _handle_video(self, ctx: commands.Context, vid: str) -> None:
        v = await self.tidal.get_video(vid)
        if v:
            await self._load_and_queue_track(ctx, v, kind=SourceKind.TIDAL_VIDEO)
        else:
            await ctx.send(embed=_error_embed(Messages.ERROR_NO_TRACKS_FOUND))

    @playback_request(batch=True)
    async def _handle_album(self, ctx: commands.Context, aid: str) -> None:
        alb = await self.tidal.get_album(aid)
        if not alb:
            await ctx.send(embed=_error_embed(Messages.ERROR_CONTENT_UNAVAILABLE))
            return
        async def _get_thumb() -> Optional[str]:
            try:
                if hasattr(alb, "image"):
                    return alb.image(dimensions=320)
            except Exception:
                pass
            return None
        tracks, thumb = await asyncio.gather(self.tidal.get_items(alb), _get_thumb())
        await self._process_track_list(ctx, tracks, getattr(alb, "name", aid), lambda t: t, thumbnail_url=thumb)

    @playback_request(batch=True)
    async def _handle_playlist(self, ctx: commands.Context, pid: str) -> None:
        pl = await self.tidal.get_playlist(pid)
        if not pl:
            await ctx.send(embed=_error_embed(Messages.ERROR_CONTENT_UNAVAILABLE))
            return
        tracks = await self.tidal.get_items(pl)
        await self._process_track_list(ctx, tracks, getattr(pl, "name", pid), lambda t: t)

    @playback_request(batch=True)
    async def _handle_mix(self, ctx: commands.Context, mid: str) -> None:
        mix = await self.tidal.get_mix(mid)
        if not mix:
            await ctx.send(embed=_error_embed(Messages.ERROR_CONTENT_UNAVAILABLE))
            return
        items = await self.tidal.get_items(mix)
        name = getattr(mix, "title", None) or getattr(mix, "name", None) or "Tidal Mix"
        await self._process_track_list(ctx, items, name, lambda t: t, COLOR_PURPLE)

    @commands.hybrid_command(name="tplay")
    @commands.guild_only()
    @playback_request()
    async def tplay(self, ctx: commands.Context, *, query: str):
        """Play Tidal content, provider links, or a Tidal search result."""
        await ctx.defer()
        try:
            provider_url = parse_provider_url(query)
        except MalformedProviderURL:
            await ctx.send(embed=_error_embed(Messages.ERROR_INVALID_URL.format(platform="provider", content_type="link")))
            return
        if self._closing or not self._initialized:
            await ctx.send(embed=_error_embed(Messages.ERROR_STILL_LOADING))
            return
        if provider_url is None or provider_url.provider in {ProviderKind.TIDAL, ProviderKind.SPOTIFY}:
            if not await self.check_ready(ctx):
                return
        if await self._prepare_playback_session(ctx) is None:
            return
        if provider_url is not None:
            if provider_url.provider is ProviderKind.TIDAL:
                handlers = {
                    "track": self._handle_track,
                    "video": self._handle_video,
                    "album": self._handle_album,
                    "playlist": self._handle_playlist,
                    "mix": self._handle_mix,
                }
                await handlers[provider_url.content_type](ctx, provider_url.identifier)
            elif provider_url.provider is ProviderKind.SPOTIFY:
                handlers = {
                    "playlist": self._handle_spotify_playlist,
                    "album": self._handle_spotify_album,
                    "track": self._handle_spotify_track,
                }
                await handlers[provider_url.content_type](ctx, query)
            elif provider_url.provider in {ProviderKind.SOUNDCLOUD, ProviderKind.BANDCAMP}:
                await self._handle_public_audio(ctx, provider_url)
            elif provider_url.content_type == "playlist":
                await self._handle_youtube_playlist(ctx, provider_url.identifier)
            else:
                await self._handle_youtube_video(ctx, provider_url.identifier)
            return
        if ISRC_PATTERN.match(query):
            isrc = ISRC_PATTERN.match(query).group(1).upper()
            track = await self.tidal.get_track_by_isrc(isrc)
            if track:
                await self._load_and_queue_track(ctx, track)
            else:
                await ctx.send(embed=_error_embed(Messages.ERROR_NO_TRACKS_FOUND))
            return
        filter_remixes, interactive = await asyncio.gather(
            self.config.guild(ctx.guild).filter_remixes(),
            self.config.guild(ctx.guild).interactive_search(),
        )
        results = await self.tidal.search(query, filter_remixes=filter_remixes)
        if not results:
            await ctx.send(embed=_error_embed(Messages.ERROR_NO_TRACKS_FOUND))
            return
        if interactive:
            selected = await self._interactive_select(ctx, results)
            if selected:
                await self._load_and_queue_track(ctx, selected)
        else:
            await self._load_and_queue_track(ctx, results[0])

    @playback_request(batch=True)
    async def _handle_spotify_playlist(self, ctx: commands.Context, url: str) -> None:
        if not self.sp:
            await ctx.send(embed=_error_embed(Messages.ERROR_NO_SPOTIFY))
            return
        if self._spotify_auth_manager is None:
            await ctx.send(
                embed=_error_embed(
                    "Spotify playlist imports require user OAuth. "
                    "Use `[p]tidalsetup spotifylogin`."
                )
            )
            return
        match = SPOTIFY_PLAYLIST_PATTERN.search(url)
        if not match:
            await ctx.send(embed=_error_embed(Messages.ERROR_INVALID_URL.format(platform="Spotify", content_type="playlist")))
            return
        playlist_id = match.group(1)
        try:
            meta = await self._run_spotify(
                lambda client: client.playlist(playlist_id, fields="name,images"),
                timeout=15.0,
            )
            items = await self._fetch_all_spotify_tracks(playlist_id)
            thumb = meta.get("images", [{}])[0].get("url") if meta.get("images") else None
            await self._process_track_list(
                ctx, items, meta.get("name", "Spotify Playlist"),
                _spotify_item_to_query, color=COLOR_GREEN, thumbnail_url=thumb,
            )
        except Exception as error:
            _log_provider_failure("Spotify", "playlist import", error)
            await ctx.send(embed=_error_embed(Messages.ERROR_FETCH_FAILED))

    @playback_request()
    async def _handle_spotify_track(self, ctx: commands.Context, url: str) -> None:
        if not self.sp:
            await ctx.send(embed=_error_embed(Messages.ERROR_NO_SPOTIFY))
            return
        match = SPOTIFY_TRACK_PATTERN.search(url)
        if not match:
            await ctx.send(embed=_error_embed(Messages.ERROR_INVALID_URL.format(platform="Spotify", content_type="track")))
            return
        track_id = match.group(1)
        try:
            item = await self._run_spotify(
                lambda client: client.track(track_id),
                timeout=15.0,
            )
            isrc = (item.get("external_ids", {}) or {}).get("isrc")
            if isrc:
                track = await self.tidal.get_track_by_isrc(isrc)
                if track:
                    await self._load_and_queue_track(ctx, track)
                    return
            filter_remixes = await self.config.guild(ctx.guild).filter_remixes()
            candidate = _spotify_album_item_to_query(item)
            results = await self.tidal.search(candidate.query, filter_remixes=filter_remixes)
            matched = select_best_tidal_track(candidate, results)
            if matched:
                await self._load_and_queue_track(ctx, matched)
            else:
                await ctx.send(embed=_error_embed(Messages.ERROR_NO_TRACKS_FOUND))
        except Exception as error:
            _log_provider_failure("Spotify", "track import", error)
            await ctx.send(embed=_error_embed(Messages.ERROR_FETCH_FAILED))

    @playback_request(batch=True)
    async def _handle_spotify_album(self, ctx: commands.Context, url: str) -> None:
        if not self.sp:
            await ctx.send(embed=_error_embed(Messages.ERROR_NO_SPOTIFY))
            return
        match = SPOTIFY_ALBUM_PATTERN.search(url)
        if not match:
            await ctx.send(embed=_error_embed(Messages.ERROR_INVALID_URL.format(platform="Spotify", content_type="album")))
            return
        album_id = match.group(1)
        try:
            album_meta = await self._run_spotify(
                lambda client: client.album(album_id),
                timeout=15.0,
            )
            items, album_name = await self._fetch_all_spotify_album_tracks(album_id, album_meta)
            thumb = album_meta.get("images", [{}])[0].get("url") if album_meta.get("images") else None
            await self._process_track_list(
                ctx, items, album_name,
                _spotify_album_item_to_query, color=COLOR_GREEN, thumbnail_url=thumb,
            )
        except Exception as error:
            _log_provider_failure("Spotify", "album import", error)
            await ctx.send(embed=_error_embed(Messages.ERROR_FETCH_FAILED))

    @playback_request()
    async def _handle_youtube_playlist(self, ctx: commands.Context, playlist_id: str) -> None:
        session = await self._prepare_playback_session(ctx)
        if session is None:
            return
        guild_id = ctx.guild.id
        cancel = self._claim_batch(guild_id)
        if cancel is None:
            await ctx.send(embed=_error_embed(Messages.ERROR_BATCH_IN_PROGRESS))
            return
        queued = skipped = 0
        try:
            message = await ctx.send(embed=discord.Embed(title="Importing YouTube playlist", color=COLOR_RED))
            videos: list[YouTubeVideoMetadata] = []
            if self.yt is not None:
                try:
                    items = await self._fetch_all_youtube_tracks(playlist_id)
                    for item in items:
                        try:
                            snippet = item["snippet"]
                            videos.append(self._youtube_snippet_metadata(snippet["resourceId"]["videoId"], snippet))
                        except (KeyError, TypeError, ValueError):
                            continue
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    _log_provider_failure("YouTube", "playlist API", error)
            if not videos and not cancel.is_set():
                videos = list(await self.youtube_resolver.fetch_playlist(playlist_id, 100))
            deduplicated: dict[str, YouTubeVideoMetadata] = {}
            for video in videos:
                if video.title.casefold() not in YOUTUBE_SKIP_TITLES:
                    deduplicated.setdefault(video.video_id, video)
            videos = list(deduplicated.values())[:MAX_ITEMS]
            total = len(videos)
            for start in range(0, total, SEARCH_BATCH_SIZE):
                if cancel.is_set() or self._closing or await self.backend.get(guild_id) is not session:
                    break
                entries = await asyncio.gather(*(self._youtube_entry(video, ctx.author.id) for video in videos[start:start + SEARCH_BATCH_SIZE]))
                for entry in entries:
                    if cancel.is_set() or self._closing or await self.backend.get(guild_id) is not session:
                        break
                    if not await self._admit_entry(ctx, session, entry, show_embed=False):
                        skipped = total - queued
                        break
                    queued += 1
                if skipped:
                    break
                await self._edit_progress_message(message, discord.Embed(
                    title="Importing YouTube playlist",
                    description=f"Queued {queued}/{total}. Skipped {skipped}.", color=COLOR_RED,
                ))
            status = "Cancelled" if cancel.is_set() else "Finished"
            await message.edit(embed=discord.Embed(
                title=f"{status} importing YouTube playlist",
                description=f"Queued {queued}/{total}. Skipped {skipped}.", color=COLOR_RED,
            ))
        except asyncio.CancelledError:
            raise
        except Exception as error:
            _log_provider_failure("YouTube", "playlist import", error)
            await ctx.send(embed=_error_embed(Messages.ERROR_FETCH_FAILED))
        finally:
            self._release_batch(guild_id, cancel)

    @commands.hybrid_command(name="tsearch")
    @commands.guild_only()
    @playback_request()
    async def tsearch(self, ctx: commands.Context, *, query: str):
        """Search Tidal and choose from top results."""
        await ctx.defer()
        if not await self.check_ready(ctx):
            return
        filter_remixes = await self.config.guild(ctx.guild).filter_remixes()
        results = await self.tidal.search(query, filter_remixes=filter_remixes)
        if not results:
            await ctx.send(embed=_error_embed(Messages.ERROR_NO_TRACKS_FOUND))
            return
        selected = await self._interactive_select(ctx, results)
        if selected:
            await self._load_and_queue_track(ctx, selected)

    @commands.hybrid_command(name="tnowplaying")
    @commands.guild_only()
    async def tnowplaying(self, ctx: commands.Context):
        """Resend the current native playback controller."""
        if ctx.guild is None:
            return
        await ctx.defer()
        session = await self.backend.get(ctx.guild.id)
        current = session.snapshot().current if session is not None else None
        if current is None:
            await ctx.send(embed=_error_embed(Messages.ERROR_NOT_PLAYING))
            return
        self._playback_channels[ctx.guild.id] = ctx.channel
        self._current_entries[ctx.guild.id] = current
        self._current_meta[ctx.guild.id] = current.meta
        self._controller_meta[ctx.guild.id] = current.meta
        if not await self._resend_controller_for_track_start(guild_id=ctx.guild.id, ctx=ctx):
            await ctx.send(embed=_error_embed("Could not refresh the player panel. Playback may have changed."))

    @commands.hybrid_command(name="tqueue")
    @commands.guild_only()
    async def tqueue(self, ctx: commands.Context):
        """Show the native queue independently of TIDAL login."""
        session = await self.backend.get(ctx.guild.id)
        if session is None:
            await ctx.send(embed=_error_embed(Messages.ERROR_NO_PLAYER))
            return
        queue = session.snapshot().queued
        if not queue:
            await ctx.send(embed=_error_embed(Messages.ERROR_NO_QUEUE))
            return
        queue_list = queue[:MAX_ITEMS]
        title = f"Queue ({len(queue)} tracks)" if len(queue) <= MAX_ITEMS else f"Queue (first {len(queue_list)} of {len(queue)} tracks)"
        pages = [
            discord.Embed(
                title=title,
                description="\n".join(
                    f"`{start + i + 1}.` {truncate(entry.meta['title'], 60)} — {truncate(entry.meta['artist'], 40)}"
                    for i, entry in enumerate(queue_list[start:start + QUEUE_PAGE_SIZE])
                ),
                color=COLOR_BLUE,
            )
            for start in range(0, len(queue_list), QUEUE_PAGE_SIZE)
        ]
        if len(pages) == 1:
            await ctx.send(embed=pages[0])
        else:
            await SimpleMenu(pages).start(ctx)

    @commands.hybrid_command(name="tstop")
    @commands.guild_only()
    async def tstop(self, ctx: commands.Context):
        """Stop queueing the current playlist."""
        if ctx.guild:
            event = self._cancel_events.get(ctx.guild.id)
            if event is not None:
                event.set()
            await ctx.send(embed=_success_embed(Messages.STATUS_STOPPING))

    @commands.hybrid_command(name="tfilter")
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    async def tfilter(self, ctx: commands.Context):
        """Toggle the remix/TikTok track filter."""
        current = await self.config.guild(ctx.guild).filter_remixes()
        await self.config.guild(ctx.guild).filter_remixes.set(not current)
        msg = Messages.SUCCESS_FILTER_DISABLED if current else Messages.SUCCESS_FILTER_ENABLED
        await ctx.send(embed=_success_embed(msg))

    @commands.hybrid_command(name="tinteractive")
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    async def tinteractive(self, ctx: commands.Context):
        """Toggle interactive search mode."""
        current = await self.config.guild(ctx.guild).interactive_search()
        await self.config.guild(ctx.guild).interactive_search.set(not current)
        msg = Messages.SUCCESS_INTERACTIVE_DISABLED if current else Messages.SUCCESS_INTERACTIVE_ENABLED
        await ctx.send(embed=_success_embed(msg))

    @commands.group(name="tpl")
    @commands.is_owner()
    async def tpl(self, ctx: commands.Context):
        """Manage your Tidal playlists."""

    @tpl.command(name="list")
    @commands.is_owner()
    async def tpl_list(self, ctx: commands.Context):
        """List your Tidal playlists."""
        if not await self.check_ready(ctx):
            return
        playlists = await self.tidal.get_user_playlists()
        if not playlists:
            await ctx.send(embed=_error_embed("No playlists found."))
            return
        pages = []
        for start in range(0, len(playlists), TPL_LIST_PAGE_SIZE):
            chunk = playlists[start:start + TPL_LIST_PAGE_SIZE]
            desc = "\n".join(
                f"`{start + i + 1}.` {truncate(getattr(p, 'name', 'Unnamed'), 60)}"
                for i, p in enumerate(chunk)
            )
            embed = discord.Embed(
                title=f"Your Tidal Playlists ({len(playlists)} total)",
                description=desc,
                color=COLOR_TEAL,
            )
            pages.append(embed)
        if len(pages) == 1:
            await ctx.send(embed=pages[0])
        else:
            await SimpleMenu(pages).start(ctx)

    @tpl.command(name="create")
    @commands.is_owner()
    async def tpl_create(self, ctx: commands.Context, *, name: str):
        """Create a new Tidal playlist."""
        if not await self.check_ready(ctx):
            return
        pl = await self.tidal.create_user_playlist(name)
        if pl:
            await ctx.send(embed=_success_embed(f"Created playlist: **{truncate(name, 60)}**"))
        else:
            await ctx.send(embed=_error_embed(Messages.ERROR_PLAYLIST_WRITE_FAILED))

    @tpl.command(name="add")
    @commands.is_owner()
    async def tpl_add(self, ctx: commands.Context, playlist_id: str, *, query: str):
        """Add a track (by search or ISRC) to one of your playlists."""
        if not await self.check_ready(ctx):
            return
        pl = await self.tidal.get_user_playlist_by_id(playlist_id)
        if not pl:
            await ctx.send(embed=_error_embed(Messages.ERROR_NOT_USER_PLAYLIST))
            return
        track = None
        if ISRC_PATTERN.match(query):
            isrc = ISRC_PATTERN.match(query).group(1).upper()
            track = await self.tidal.get_track_by_isrc(isrc)
        if not track:
            results = await self.tidal.search(query)
            if results:
                track = results[0]
        if not track:
            await ctx.send(embed=_error_embed(Messages.ERROR_NO_TRACKS_FOUND))
            return
        track_id = getattr(track, "id", None)
        if not track_id:
            await ctx.send(embed=_error_embed(Messages.ERROR_NO_TRACKS_FOUND))
            return
        ok = await self.tidal.add_track_to_playlist(pl, track_id)
        if ok:
            name = getattr(track, "name", str(track_id))
            await ctx.send(embed=_success_embed(f"Added **{truncate(name, 60)}** to playlist."))
        else:
            await ctx.send(embed=_error_embed(Messages.ERROR_PLAYLIST_WRITE_FAILED))

    @tpl.command(name="remove")
    @commands.is_owner()
    async def tpl_remove(self, ctx: commands.Context, playlist_id: str, track_id: int):
        """Remove a track by ID from one of your playlists."""
        if not await self.check_ready(ctx):
            return
        pl = await self.tidal.get_user_playlist_by_id(playlist_id)
        if not pl:
            await ctx.send(embed=_error_embed(Messages.ERROR_NOT_USER_PLAYLIST))
            return
        ok = await self.tidal.remove_track_from_playlist(pl, track_id)
        if ok:
            await ctx.send(embed=_success_embed(f"Removed track `{track_id}` from playlist."))
        else:
            await ctx.send(embed=_error_embed(Messages.ERROR_PLAYLIST_WRITE_FAILED))

    @tpl.command(name="play")
    @commands.is_owner()
    async def tpl_play(self, ctx: commands.Context, playlist_id: str):
        """Queue one of your Tidal playlists."""
        if not await self.check_ready(ctx):
            return
        pl = await self.tidal.get_user_playlist_by_id(playlist_id)
        if not pl:
            await ctx.send(embed=_error_embed(Messages.ERROR_NOT_USER_PLAYLIST))
            return
        tracks = await self.tidal.get_items(pl)
        await self._process_track_list(ctx, tracks, getattr(pl, "name", playlist_id), lambda t: t, COLOR_TEAL)

    @commands.group(name="tidalsetup")
    @commands.is_owner()
    async def tidalsetup(self, ctx: commands.Context):
        """Configure Tidal, Spotify, and YouTube access (bot owner only)."""

    @tidalsetup.command(name="doctor")
    @commands.is_owner()
    async def tidalsetup_doctor(self, ctx: commands.Context) -> None:
        """Check native voice dependencies without accessing credentials."""
        from .playback.diagnostics import collect_diagnostics
        report = await collect_diagnostics(
            self.bot, self.backend, self.source_factory,
            tidal_authenticated=self.tidal._login_cache, guild=ctx.guild,
            deno_locator=self._native_deno_path,
            managed_deno_version=DENO_VERSION if self.runtime.locate("deno") else None,
        )
        await ctx.send(report.replace("[p]", ctx.clean_prefix))

    @tidalsetup.command(name="repair")
    @commands.is_owner()
    async def tidalsetup_repair(self, ctx: commands.Context) -> None:
        """Install and validate pinned FFmpeg and Deno in persistent cog data.

        Bot owner only. Downloads approximately 150-190 MB on supported hosts.
        No server access is required. Wait for completion, then reload this cog.
        Playback and doctor never trigger this installation automatically.
        """
        if self._closing:
            await ctx.send("TidalPlayerExp is unloading; load it before requesting repair.")
            return
        await ctx.send(
            "Checking/installing the cog-local FFmpeg and Deno runtime. "
            "This can take a few minutes; please wait before reloading the cog."
        )
        try:
            await self.runtime.repair()
        except RuntimeRepairError as error:
            # Never quote download URLs, local paths, or child-process output.
            code = error.code
            safe_code = (
                code if isinstance(code, str) and code.isascii()
                and code.replace("_", "").isalnum() and len(code) <= 64
                else "runtime_error"
            )
            log.warning("Native runtime repair failed (%s).", safe_code)
            await ctx.send(
                f"Native runtime repair failed ({safe_code}); the previous runtime was not replaced. "
                "Check available disk space, outbound HTTPS access, and executable permissions."
            )
            return
        except Exception as error:
            _log_provider_failure("Native runtime", "repair", error)
            await ctx.send("Native runtime repair failed; no successful installation was confirmed.")
            return
        message = (
            "FFmpeg and Deno passed validation. Run "
            f"`{ctx.clean_prefix}reload TidalPlayerExp`, then `{ctx.clean_prefix}tidalsetup doctor` "
            "and retry playback. No bot or server restart is needed."
        )
        if os.environ.get("IMAGEIO_FFMPEG_EXE"):
            message += " An administrator's IMAGEIO_FFMPEG_EXE override still takes precedence over managed FFmpeg."
        await ctx.send(message)

    @tidalsetup.command(name="spotify")
    @commands.is_owner()
    async def tidalsetup_spotify(self, ctx: commands.Context):
        """Open Red's secure modal for Spotify API credentials."""
        view = SetApiView(
            default_service="spotify",
            default_keys={"client_id": "", "client_secret": ""},
        )
        await ctx.send(
            embed=discord.Embed(
                title="Spotify API setup",
                description=(
                    "Create an app in the Spotify Developer Dashboard, then use the button "
                    "below to enter its client ID and client secret securely. Spotify requires "
                    "the app owner to have Premium while the app is in Development Mode."
                ),
                color=COLOR_GREEN,
            ),
            view=view,
        )

    @tidalsetup.command(name="spotifylogin")
    @commands.is_owner()
    async def tidalsetup_spotifylogin(self, ctx: commands.Context):
        """Authorize the Spotify account used for playlist imports."""
        if not SPOTIFY_AVAILABLE:
            await ctx.send(embed=_error_embed(Messages.ERROR_NO_SPOTIFY))
            return
        try:
            authorize_url, state = await self._begin_spotify_login(ctx.author.id)
            previous_view = self._spotify_login_views.pop(ctx.author.id, None)
            if previous_view is not None:
                previous_view.stop()
            view = SpotifyLoginView(self, ctx.author.id, state)
            self._spotify_login_views[ctx.author.id] = view
            await ctx.author.send(
                "Open this URL and authorize Spotify:\n"
                f"<{authorize_url}>\n\n"
                "After Spotify redirects you, the local page may fail to load because "
                "DripBot is hosted remotely. Copy the complete URL from your browser, "
                "then press **Finish Spotify login** below and paste it.",
                view=view,
            )
        except SpotifyLoginError as error:
            await ctx.send(embed=_error_embed(str(error)))
            return
        except discord.HTTPException:
            self._clear_spotify_login(ctx.author.id)
            await ctx.send(
                embed=_error_embed("I could not DM you. Enable DMs and try again.")
            )
            return
        except Exception as error:
            self._clear_spotify_login(ctx.author.id)
            _log_provider_failure("Spotify", "OAuth start", error)
            await ctx.send(
                embed=_error_embed("Could not start Spotify authentication.")
            )
            return
        await ctx.send(embed=_success_embed("Check your DMs to finish Spotify login."))

    @tidalsetup.command(name="spotifystatus")
    @commands.is_owner()
    async def tidalsetup_spotifystatus(self, ctx: commands.Context):
        """Check Spotify app and user authentication status."""
        if self._spotify_auth_manager is not None and self.sp is not None:
            await ctx.send(embed=_success_embed("Spotify user session is active."))
        elif self.sp is not None:
            await ctx.send(
                embed=_error_embed(
                    "Spotify app credentials are configured, but user OAuth is not connected. "
                    "Use `[p]tidalsetup spotifylogin`."
                )
            )
        else:
            await ctx.send(
                embed=_error_embed(
                    "Spotify is not configured. Use `[p]tidalsetup spotify` first."
                )
            )

    @tidalsetup.command(name="spotifylogout")
    @commands.is_owner()
    async def tidalsetup_spotifylogout(self, ctx: commands.Context):
        """Remove Spotify user authorization while keeping app credentials."""
        for owner_id in tuple(self._spotify_login_states):
            self._clear_spotify_login(owner_id)
        async with self._spotify_commit_lock:
            await self.bot.remove_shared_api_tokens("spotify", "refresh_token")
            await self._initialize_spotify_locked()
        await ctx.send(embed=_success_embed("Spotify user session logged out."))

    @tidalsetup.command(name="youtube")
    @commands.is_owner()
    async def tidalsetup_youtube(self, ctx: commands.Context):
        """Open Red's secure modal for a YouTube Data API key."""
        view = SetApiView(
            default_service="youtube",
            default_keys={"api_key": ""},
        )
        await ctx.send(
            embed=discord.Embed(
                title="YouTube API setup",
                description=(
                    "Enable YouTube Data API v3 in a Google Cloud project, then use the button "
                    "below to enter its API key securely."
                ),
                color=COLOR_RED,
            ),
            view=view,
        )

    @tidalsetup.command(name="login")
    @commands.is_owner()
    async def tidalsetup_login(self, ctx: commands.Context):
        """Start the Tidal device-code OAuth flow."""
        if not TIDALAPI_AVAILABLE:
            await ctx.send(embed=_error_embed(Messages.ERROR_NO_TIDALAPI))
            return
        try:
            login_url, future = await self.tidal._run_blocking(
                self.tidal.session.login_oauth, timeout=15.0
            )
            await ctx.author.send(
                f"Open this URL to authenticate with Tidal:\n<{login_url.verification_uri_complete}>\n"
                f"You have {login_url.expires_in} seconds."
            )
            await ctx.send(embed=_success_embed("Check your DMs for the Tidal login link."))
            await self.tidal._run_blocking(lambda: future.result(), timeout=120.0)
            def _get_state():
                return (
                    self.tidal.session.expiry_time,
                    self.tidal.session.token_type,
                    self.tidal.session.access_token,
                    self.tidal.session.refresh_token,
                )
            expiry_time, token_type, access, refresh = await self.tidal._run_blocking(_get_state, timeout=5.0)
            expiry_aware = _ensure_aware(expiry_time) if expiry_time else None
            snapshot = TokenSnapshot(
                token_type=token_type,
                access_token=access,
                refresh_token=refresh,
                expiry_time=int(expiry_aware.timestamp()) if expiry_aware else 0,
            )
            await self.tokens.replace(snapshot)
            self.tidal.invalidate_login_cache()
            await ctx.send(embed=_success_embed("Tidal authentication successful!"))
        except asyncio.TimeoutError:
            await ctx.send(embed=_error_embed("Authentication timed out. Please try again."))
        except Exception as error:
            _log_provider_failure("Tidal", "OAuth login", error)
            await ctx.send(embed=_error_embed("Authentication failed. Check logs for details."))

    @tidalsetup.command(name="logout")
    @commands.is_owner()
    async def tidalsetup_logout(self, ctx: commands.Context):
        """Clear stored Tidal tokens."""
        await self.tidal.logout()
        await ctx.send(embed=_success_embed(Messages.SUCCESS_TOKENS_CLEARED))

    @tidalsetup.command(name="status")
    @commands.is_owner()
    async def tidalsetup_status(self, ctx: commands.Context):
        """Check Tidal authentication status."""
        logged_in = await self.tidal.is_logged_in()
        if logged_in:
            await ctx.send(embed=_success_embed("Tidal session is active."))
        else:
            await ctx.send(embed=_error_embed("Not authenticated. Use `[p]tidalsetup login`."))


async def setup(bot):
    await bot.add_cog(TidalPlayerExp(bot))
