import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


async def _run_now(_handler, operation, **_kwargs):
    return operation()


@pytest.mark.asyncio
@pytest.mark.parametrize("share", [
    "https://open.spotify.com/track/4uLU6hMCjMI75M1A2tKUQC",
    "https://open.spotify.com/intl-de/track/4uLU6hMCjMI75M1A2tKUQC",
    "https://open.spotify.com/embed/track/4uLU6hMCjMI75M1A2tKUQC",
    "https://open.spotify.com/intl-pt-BR/embed/track/4uLU6hMCjMI75M1A2tKUQC",
])
async def test_spotify_share_links_reach_the_provider_with_their_identifier(cog, share) -> None:
    """A routed Spotify link must not be rejected by a second, stricter parse."""
    ctx = SimpleNamespace(guild=SimpleNamespace(id=1), send=AsyncMock())
    cog._defer = AsyncMock()
    cog._initialized = True
    cog.check_ready = AsyncMock(return_value=True)
    cog._prepare_playback_session = AsyncMock(return_value=object())
    handler = AsyncMock()
    cog._handle_spotify_track = handler

    await cog._play_request(ctx, query=share)

    handler.assert_awaited_once_with(ctx, "4uLU6hMCjMI75M1A2tKUQC")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method_name", "attribute"),
    [("_initialize_spotify", "sp"), ("_initialize_youtube", "yt")],
)
async def test_missing_provider_credentials_clear_existing_client(
    cog, method_name: str, attribute: str
) -> None:
    setattr(cog, attribute, object())
    cog.bot.get_shared_api_tokens = AsyncMock(return_value={})

    await getattr(cog, method_name)()

    assert getattr(cog, attribute) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("service_name", "initializer"),
    [("spotify", "_initialize_spotify"), ("youtube", "_initialize_youtube")],
)
async def test_shared_token_update_reinitializes_provider(
    cog, service_name: str, initializer: str
) -> None:
    with patch.object(type(cog), initializer, new=AsyncMock()) as initialize:
        await cog.on_red_api_tokens_update(service_name, {})

    if service_name == "spotify":
        initialize.assert_awaited_once_with(tokens={})
    else:
        initialize.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_spotify_playlist_stops_on_repeated_next_cursor(cog) -> None:
    item_one = {"track": {"name": "One"}}
    item_two = {"track": {"name": "Two"}}
    cog.sp = SimpleNamespace(
        playlist_items=MagicMock(
            side_effect=[
                {"items": [item_one], "next": "same-page"},
                {"items": [item_two], "next": "same-page"},
            ]
        )
    )
    with patch.object(type(cog.tidal), "_run_blocking", new=_run_now):
        tracks = await cog._fetch_all_spotify_tracks("playlist")

    assert tracks == [item_one, item_two]
    assert cog.sp.playlist_items.call_count == 2


@pytest.mark.asyncio
async def test_spotify_album_stops_on_repeated_next_cursor(cog) -> None:
    first = {"name": "One"}
    second = {"name": "Two"}
    cog.sp = SimpleNamespace(
        album=MagicMock(
            return_value={
                "name": "Album",
                "tracks": {"items": [first], "next": "same-page"},
            }
        ),
        _get=MagicMock(
            return_value={"items": [second], "next": "same-page"}
        ),
    )
    with patch.object(type(cog.tidal), "_run_blocking", new=_run_now):
        tracks, name = await cog._fetch_all_spotify_album_tracks("album")

    assert (tracks, name) == ([first, second], "Album")
    assert cog.sp._get.call_count == 1


@pytest.mark.asyncio
async def test_spotify_playlist_rejects_malformed_items(cog) -> None:
    cog.sp = SimpleNamespace(
        playlist_items=MagicMock(
            return_value={"items": "not-a-list", "next": "page"}
        )
    )
    with patch.object(type(cog.tidal), "_run_blocking", new=_run_now):
        assert await cog._fetch_all_spotify_tracks("playlist") == []


@pytest.mark.asyncio
async def test_spotify_album_rejects_malformed_items(cog) -> None:
    cog.sp = SimpleNamespace(
        album=MagicMock(
            return_value={
                "name": "Album",
                "tracks": {"items": "not-a-list", "next": "page"},
            }
        ),
        _get=MagicMock(side_effect=AssertionError("malformed items must stop pagination")),
    )
    with patch.object(type(cog.tidal), "_run_blocking", new=_run_now):
        tracks, name = await cog._fetch_all_spotify_album_tracks("album")

    assert (tracks, name) == ([], "Album")
    cog.sp._get.assert_not_called()


@pytest.mark.asyncio
async def test_initialize_apis_does_not_log_raw_provider_exception(cog, caplog) -> None:
    caplog.set_level(logging.WARNING, logger="red.tidalplayerexp")
    secret = "provider-secret"
    with (
        patch.object(type(cog.tidal), "initialize", new=AsyncMock()),
        patch.object(
            type(cog),
            "_initialize_spotify",
            new=AsyncMock(
                side_effect=RuntimeError(
                    f"https://provider.invalid/?token={secret}"
                )
            ),
        ),
        patch.object(type(cog), "_initialize_youtube", new=AsyncMock()),
        patch.object(type(cog.tidal), "start_refresh_loop", new=MagicMock()),
    ):
        await cog._initialize_apis()

    assert secret not in caplog.text
    assert "https://" not in caplog.text
    assert "RuntimeError" in caplog.text


@pytest.mark.asyncio
async def test_own_refresh_token_write_does_not_rebuild_spotify(cog) -> None:
    """A rotated token persisted by the cog must not blank the live client."""
    cog.sp = object()
    cog._spotify_fingerprint = ("client", "secret", "refresh-a")
    with patch.object(type(cog), "_initialize_spotify_locked", new=AsyncMock()) as rebuild:
        await cog.on_red_api_tokens_update(
            "spotify",
            {"client_id": "client", "client_secret": "secret", "refresh_token": "refresh-a"},
        )
        rebuild.assert_not_awaited()

        await cog.on_red_api_tokens_update(
            "spotify",
            {"client_id": "client", "client_secret": "secret", "refresh_token": "refresh-b"},
        )
    rebuild.assert_awaited_once()


@pytest.mark.asyncio
async def test_missing_spotify_client_is_rebuilt_lazily(cog) -> None:
    """A transient init failure must recover on the next use, not only on reload."""
    client = object()

    async def rebuild() -> None:
        cog.sp = client

    cog.sp = None
    cog._spotify_retry_after = 0.0
    with patch.object(type(cog), "_initialize_spotify_locked", new=AsyncMock(side_effect=rebuild)):
        assert await cog._spotify_ready() is True
    assert cog.sp is client
    assert cog._spotify_retry_after == 0.0


@pytest.mark.asyncio
async def test_failed_spotify_rebuild_is_throttled(cog) -> None:
    """Repeated uses must not start a provider build on every request."""
    rebuild = AsyncMock()
    cog.sp = None
    cog._spotify_retry_after = 0.0
    with patch.object(type(cog), "_initialize_spotify_locked", new=rebuild):
        assert await cog._spotify_ready() is False
        assert await cog._spotify_ready() is False
    rebuild.assert_awaited_once()
    assert cog._spotify_retry_after > 0


@pytest.mark.asyncio
async def test_missing_youtube_client_is_rebuilt_lazily(cog, monkeypatch) -> None:
    import sys
    import types

    from TidalPlayerExp import tidalplayer as module

    transport = types.ModuleType("TidalPlayerExp.providers.google_transport")
    transport.IsolatedHttpRequest = object
    monkeypatch.setitem(sys.modules, "TidalPlayerExp.providers.google_transport", transport)
    monkeypatch.setattr(module, "YOUTUBE_API_AVAILABLE", True)
    cog.bot.get_shared_api_tokens = AsyncMock(return_value={"api_key": "key"})
    client = object()
    monkeypatch.setattr(module, "build", MagicMock(return_value=client))
    cog.yt = None
    cog._youtube_retry_after = 0.0

    assert await cog._youtube_ready() is True
    assert cog.yt is client
    assert cog._youtube_retry_after == 0.0


@pytest.mark.asyncio
async def test_failed_youtube_rebuild_is_throttled(cog, monkeypatch) -> None:
    import sys
    import types

    from TidalPlayerExp import tidalplayer as module

    transport = types.ModuleType("TidalPlayerExp.providers.google_transport")
    transport.IsolatedHttpRequest = object
    monkeypatch.setitem(sys.modules, "TidalPlayerExp.providers.google_transport", transport)
    monkeypatch.setattr(module, "YOUTUBE_API_AVAILABLE", True)
    cog.bot.get_shared_api_tokens = AsyncMock(return_value={"api_key": "key"})
    monkeypatch.setattr(module, "build", MagicMock(side_effect=RuntimeError("down")))
    cog.yt = None
    cog._youtube_retry_after = 0.0

    assert await cog._youtube_ready() is False
    assert await cog._youtube_ready() is False
    assert cog.yt is None
    assert cog._youtube_retry_after > 0
