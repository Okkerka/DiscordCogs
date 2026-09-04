from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


async def _run_now(_handler, operation, **_kwargs):
    return operation()


@pytest.mark.asyncio
async def test_client_credentials_use_memory_cache_instead_of_dot_cache(cog) -> None:
    cog.bot.get_shared_api_tokens = AsyncMock(
        return_value={"client_id": "client", "client_secret": "secret"}
    )
    module = __import__(cog.__class__.__module__, fromlist=["unused"])
    memory_cache = object()

    with (
        patch.object(module, "MemoryCacheHandler", return_value=memory_cache) as cache_factory,
        patch.object(module, "SpotifyClientCredentials") as credentials_factory,
        patch.object(module.spotipy, "Spotify") as spotify_factory,
        patch.object(type(cog.tidal), "_run_blocking", new=_run_now),
    ):
        await cog._initialize_spotify()

    cache_factory.assert_called_once_with()
    credentials_factory.assert_called_once_with(
        "client", "secret", cache_handler=memory_cache
    )
    spotify_factory.assert_called_once_with(
        client_credentials_manager=credentials_factory.return_value,
        requests_timeout=15.0,
    )


def test_spotify_callback_requires_exact_loopback_uri_and_state(cog) -> None:
    valid = "http://127.0.0.1:2402/callback?code=one-use-code&state=expected"

    assert cog._parse_spotify_callback(valid, "expected") == "one-use-code"

    with pytest.raises(ValueError, match="redirect"):
        cog._parse_spotify_callback(
            "https://attacker.invalid/callback?code=stolen&state=expected",
            "expected",
        )
    with pytest.raises(ValueError, match="state"):
        cog._parse_spotify_callback(valid, "different")


@pytest.mark.asyncio
async def test_spotify_oauth_exchange_persists_token_and_activates_client(cog) -> None:
    cog.bot.get_shared_api_tokens = AsyncMock(
        return_value={"client_id": "client", "client_secret": "secret"}
    )
    cog._spotify_login_states[42] = ("expected", 10_000.0)
    token_info = {
        "access_token": "access",
        "refresh_token": "refresh",
        "expires_at": 20_000,
        "scope": "playlist-read-private playlist-read-collaborative",
    }
    oauth = SimpleNamespace(get_access_token=MagicMock(return_value=token_info))

    async def activate(_self) -> None:
        cog._spotify_auth_manager = object()
        cog.sp = object()

    with (
        patch.object(type(cog), "_new_spotify_oauth", return_value=oauth),
        patch.object(type(cog), "_initialize_spotify", new=activate),
        patch.object(type(cog.tidal), "_run_blocking", new=_run_now),
        patch("asyncio.get_running_loop") as get_loop,
    ):
        get_loop.return_value.time.return_value = 1_000.0
        await cog._complete_spotify_login(
            42,
            "http://127.0.0.1:2402/callback?code=one-use-code&state=expected",
        )

    oauth.get_access_token.assert_called_once_with(
        "one-use-code", as_dict=True, check_cache=False
    )
    assert cog.bot.set_shared_api_tokens.await_args.kwargs == {
        "refresh_token": "refresh"
    }
    assert 42 not in cog._spotify_login_states
    assert cog._spotify_auth_manager is not None


def test_spotify_playlist_item_supports_current_item_field(cog) -> None:
    item = {
        "item": {
            "name": "Track",
            "artists": [{"name": "Artist"}],
            "external_ids": {"isrc": "USAAA0000001"},
        }
    }

    module = __import__(cog.__class__.__module__, fromlist=["unused"])
    assert module._spotify_item_to_query(item) == "isrc:USAAA0000001"


@pytest.mark.asyncio
async def test_saved_spotify_oauth_session_restores_and_persists_refresh(cog) -> None:
    saved = {
        "access_token": "",
        "refresh_token": "old-refresh",
        "expires_at": 0,
        "scope": "playlist-read-private playlist-read-collaborative",
    }
    refreshed = {
        **saved,
        "access_token": "fresh",
        "refresh_token": "new-refresh",
        "expires_at": 20_000,
    }
    cog.bot.get_shared_api_tokens = AsyncMock(
        return_value={
            "client_id": "client",
            "client_secret": "secret",
            "refresh_token": "old-refresh",
        }
    )
    oauth = SimpleNamespace(validate_token=MagicMock(return_value=refreshed))
    module = __import__(cog.__class__.__module__, fromlist=["unused"])

    with (
        patch.object(type(cog), "_new_spotify_oauth", return_value=oauth),
        patch.object(module.spotipy, "Spotify") as spotify_factory,
        patch.object(type(cog.tidal), "_run_blocking", new=_run_now),
    ):
        await cog._initialize_spotify()

    oauth.validate_token.assert_called_once_with(saved)
    spotify_factory.assert_called_once_with(auth_manager=oauth, requests_timeout=15.0)
    assert cog.bot.set_shared_api_tokens.await_args.kwargs == {
        "refresh_token": "new-refresh"
    }
    assert cog._spotify_auth_manager is oauth


@pytest.mark.asyncio
async def test_begin_spotify_login_creates_owner_bound_expiring_state(cog) -> None:
    cog.bot.get_shared_api_tokens = AsyncMock(
        return_value={"client_id": "client", "client_secret": "secret"}
    )
    oauth = SimpleNamespace(get_authorize_url=MagicMock(return_value="https://accounts.spotify.test"))

    with (
        patch.object(type(cog), "_new_spotify_oauth", return_value=oauth) as oauth_factory,
        patch("secrets.token_urlsafe", return_value="state"),
        patch("asyncio.get_running_loop") as get_loop,
    ):
        get_loop.return_value.time.return_value = 1_000.0
        url, state = await cog._begin_spotify_login(42)

    assert url == "https://accounts.spotify.test"
    assert state == "state"
    assert cog._spotify_login_states == {42: ("state", 1_600.0)}
    oauth_factory.assert_called_once_with("client", "secret", state="state")
    oauth.get_authorize_url.assert_called_once_with()


@pytest.mark.asyncio
async def test_spotifylogin_dms_authorization_link_and_private_completion_view(cog) -> None:
    author = SimpleNamespace(id=42, send=AsyncMock())
    ctx = SimpleNamespace(author=author, send=AsyncMock())

    with patch.object(
        type(cog),
        "_begin_spotify_login",
        new=AsyncMock(return_value=("https://accounts.spotify.test/authorize", "state")),
    ):
        await cog.tidalsetup_spotifylogin(ctx)

    dm = author.send.await_args
    assert "https://accounts.spotify.test/authorize" in dm.args[0]
    assert dm.kwargs["view"].owner_id == 42
    assert "dm" in ctx.send.await_args.kwargs["embed"].description.lower()


@pytest.mark.asyncio
async def test_spotify_callback_modal_rejects_non_owner_without_exchange(cog) -> None:
    module = __import__(cog.__class__.__module__, fromlist=["unused"])
    modal = module.SpotifyCallbackModal(cog, owner_id=42)
    interaction = SimpleNamespace(
        user=SimpleNamespace(id=99),
        response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock()),
        edit_original_response=AsyncMock(),
    )
    cog.bot.is_owner = AsyncMock(return_value=False)

    with patch.object(type(cog), "_complete_spotify_login", new=AsyncMock()) as complete:
        await modal.on_submit(interaction)

    interaction.response.send_message.assert_awaited_once()
    interaction.response.defer.assert_not_awaited()
    complete.assert_not_awaited()


@pytest.mark.asyncio
async def test_spotify_callback_modal_defers_then_completes_login(cog) -> None:
    module = __import__(cog.__class__.__module__, fromlist=["unused"])
    modal = module.SpotifyCallbackModal(cog, owner_id=42)
    modal.callback_url.value = (
        "http://127.0.0.1:2402/callback?code=one-use-code&state=expected"
    )
    interaction = SimpleNamespace(
        user=SimpleNamespace(id=42),
        response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock()),
        edit_original_response=AsyncMock(),
    )
    cog.bot.is_owner = AsyncMock(return_value=True)

    with patch.object(type(cog), "_complete_spotify_login", new=AsyncMock()) as complete:
        await modal.on_submit(interaction)

    interaction.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
    complete.assert_awaited_once_with(42, str(modal.callback_url.value))
    embed = interaction.edit_original_response.await_args.kwargs["embed"]
    assert "successful" in embed.description.lower()


@pytest.mark.asyncio
async def test_spotify_callback_modal_does_not_echo_unexpected_exception_text(cog) -> None:
    module = __import__(cog.__class__.__module__, fromlist=["unused"])
    modal = module.SpotifyCallbackModal(cog, owner_id=42)
    modal.callback_url.value = "callback"
    interaction = SimpleNamespace(
        user=SimpleNamespace(id=42),
        response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock()),
        edit_original_response=AsyncMock(),
    )
    cog.bot.is_owner = AsyncMock(return_value=True)
    secret = "https://provider.invalid/?code=secret"

    with patch.object(
        type(cog),
        "_complete_spotify_login",
        new=AsyncMock(side_effect=ValueError(secret)),
    ):
        await modal.on_submit(interaction)

    embed = interaction.edit_original_response.await_args.kwargs["embed"]
    assert secret not in embed.description
    assert "failed" in embed.description.lower()


@pytest.mark.asyncio
async def test_spotifylogout_removes_only_user_token_and_keeps_app_credentials(cog) -> None:
    ctx = SimpleNamespace(send=AsyncMock())

    with patch.object(type(cog), "_initialize_spotify", new=AsyncMock()) as initialize:
        await cog.tidalsetup_spotifylogout(ctx)

    cog.bot.remove_shared_api_tokens.assert_awaited_once_with("spotify", "refresh_token")
    initialize.assert_awaited_once_with()
    assert "logged out" in ctx.send.await_args.kwargs["embed"].description.lower()


@pytest.mark.asyncio
async def test_spotify_playlist_requires_user_oauth_before_api_request(cog) -> None:
    cog.sp = SimpleNamespace(playlist=MagicMock())
    cog._spotify_auth_manager = None
    ctx = SimpleNamespace(send=AsyncMock())

    await cog._handle_spotify_playlist(
        ctx,
        "https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M",
    )

    cog.sp.playlist.assert_not_called()
    embed = ctx.send.await_args.kwargs["embed"]
    assert "spotifylogin" in embed.description.lower()


@pytest.mark.asyncio
async def test_spotify_playlist_pagination_accepts_current_item_shape(cog) -> None:
    wrapper = {"item": {"name": "Track", "artists": [{"name": "Artist"}]}}
    cog.sp = SimpleNamespace(
        playlist_items=MagicMock(return_value={"items": [wrapper], "next": None})
    )

    with patch.object(type(cog.tidal), "_run_blocking", new=_run_now):
        tracks = await cog._fetch_all_spotify_tracks("playlist")

    assert tracks == [wrapper]
    fields = cog.sp.playlist_items.call_args.kwargs["fields"]
    assert fields == "items(item(name,artists(name),external_ids)),next"


@pytest.mark.asyncio
async def test_spotify_request_persists_token_rotated_by_auth_manager(cog) -> None:
    refreshed = {
        "access_token": "fresh",
        "refresh_token": "rotated",
        "expires_at": 20_000,
        "scope": "playlist-read-private playlist-read-collaborative",
    }
    cache = SimpleNamespace(get_cached_token=MagicMock(return_value=refreshed))
    cog._spotify_auth_manager = SimpleNamespace(cache_handler=cache)
    cog._spotify_refresh_token = "old"
    cog.sp = SimpleNamespace(track=MagicMock(return_value={"name": "Track"}))

    with patch.object(type(cog.tidal), "_run_blocking", new=_run_now):
        result = await cog._run_spotify(lambda client: client.track("id"), timeout=15.0)

    assert result == {"name": "Track"}
    assert cog.bot.set_shared_api_tokens.await_args.kwargs == {
        "refresh_token": "rotated"
    }


@pytest.mark.asyncio
async def test_old_spotify_login_view_timeout_cannot_cancel_new_login(cog) -> None:
    module = __import__(cog.__class__.__module__, fromlist=["unused"])
    old_view = module.SpotifyLoginView(cog, owner_id=42, state="old")
    new_view = module.SpotifyLoginView(cog, owner_id=42, state="new")
    cog._spotify_login_states[42] = ("new", 2_000.0)
    cog._spotify_login_views[42] = new_view

    await old_view.on_timeout()

    assert cog._spotify_login_states[42][0] == "new"
    assert cog._spotify_login_views[42] is new_view


@pytest.mark.asyncio
async def test_expired_spotify_login_stops_and_removes_its_view(cog) -> None:
    module = __import__(cog.__class__.__module__, fromlist=["unused"])
    view = module.SpotifyLoginView(cog, owner_id=42, state="expired")
    cog._spotify_login_states[42] = ("expired", 1.0)
    cog._spotify_login_views[42] = view

    with patch("asyncio.get_running_loop") as get_loop:
        get_loop.return_value.time.return_value = 2.0
        with pytest.raises(ValueError, match="expired"):
            await cog._complete_spotify_login(42, "unused")

    assert 42 not in cog._spotify_login_states
    assert 42 not in cog._spotify_login_views
    assert view.stopped is True


@pytest.mark.asyncio
async def test_spotify_login_fails_if_saved_session_cannot_activate(cog) -> None:
    cog.bot.get_shared_api_tokens = AsyncMock(
        return_value={"client_id": "client", "client_secret": "secret"}
    )
    cog._spotify_login_states[42] = ("expected", 10_000.0)
    oauth = SimpleNamespace(
        get_access_token=MagicMock(
            return_value={
                "access_token": "access",
                "refresh_token": "refresh",
                "expires_at": 20_000,
                "scope": "playlist-read-private playlist-read-collaborative",
            }
        )
    )

    with (
        patch.object(type(cog), "_new_spotify_oauth", return_value=oauth),
        patch.object(type(cog), "_initialize_spotify", new=AsyncMock()),
        patch.object(type(cog.tidal), "_run_blocking", new=_run_now),
        patch("asyncio.get_running_loop") as get_loop,
    ):
        get_loop.return_value.time.return_value = 1_000.0
        with pytest.raises(RuntimeError, match="activate"):
            await cog._complete_spotify_login(
                42,
                "http://127.0.0.1:2402/callback?code=one-use-code&state=expected",
            )
