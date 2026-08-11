import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


async def _run_now(_handler, operation, **_kwargs):
    return operation()


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
async def test_spotify_playlist_stops_on_repeated_next_cursor(cog) -> None:
    item_one = {"track": {"name": "One"}}
    item_two = {"track": {"name": "Two"}}
    cog.sp = SimpleNamespace(
        playlist_tracks=MagicMock(
            side_effect=[
                {"items": [item_one], "next": "same-page"},
                {"items": [item_two], "next": "same-page"},
            ]
        )
    )
    with patch.object(type(cog.tidal), "_run_blocking", new=_run_now):
        tracks = await cog._fetch_all_spotify_tracks("playlist")

    assert tracks == [item_one, item_two]
    assert cog.sp.playlist_tracks.call_count == 2


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
        playlist_tracks=MagicMock(
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
    caplog.set_level(logging.WARNING, logger="red.tidalplayer")
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
