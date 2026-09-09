"""Regression coverage for TidalPlayerExp playback failure handling."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from TidalPlayerExp.tests.conftest import make_entry
from TidalPlayerExp.domain.matching import select_confident_youtube_tidal_track


def _tidal_candidate(title: str, artist: str):
    return SimpleNamespace(name=title, full_name=title, artist=SimpleNamespace(name=artist))


@pytest.mark.parametrize(
    ("video_title", "channel"),
    [
        ("AZALI - Rivals", "AZALI"),
        ("AZALI - Rivals (Official Audio)", "AZALI - Topic"),
        ("AZALI - Rivals [Official Music Video]", "Independent Label"),
    ],
)
def test_youtube_tidal_match_accepts_only_title_and_artist_identity(
    video_title: str, channel: str
) -> None:
    candidate = _tidal_candidate("Rivals", "AZALI")

    assert select_confident_youtube_tidal_track(video_title, channel, [candidate]) is candidate


@pytest.mark.parametrize(
    ("video_title", "channel", "tidal_title", "tidal_artist"),
    [
        ("AZALI - Rivals (Piano Cover)", "AZALI", "Rivals", "AZALI"),
        ("AZALI - Rivals Remix", "AZALI", "Rivals", "AZALI"),
        ("AZALI - Rivals (Live)", "AZALI", "Rivals", "AZALI"),
        ("AZALI - Rivals", "AZALI", "Rival", "AZALI"),
        ("Someone Else - Rivals", "Someone Else", "Rivals", "AZALI"),
        ("AZALI - Rivals", "AZALI", "", "AZALI"),
        ("AZALI - Rivals", "AZALI", "Rivals", ""),
    ],
)
def test_youtube_tidal_match_rejects_uncertain_recordings(
    video_title: str,
    channel: str,
    tidal_title: str,
    tidal_artist: str,
) -> None:
    candidate = _tidal_candidate(tidal_title, tidal_artist)

    assert select_confident_youtube_tidal_track(video_title, channel, [candidate]) is None


@pytest.mark.parametrize(
    "variant",
    ["Cover", "Remix", "Live", "Karaoke", "Instrumental", "Nightcore", "Sped Up", "Slowed Down", "Reverb", "Remastered", "Acoustic"],
)
@pytest.mark.parametrize("direction", ["video", "tidal"])
def test_youtube_tidal_recording_variants_must_match_symmetrically(variant: str, direction: str) -> None:
    video_title = f"AZALI - Rivals{f' ({variant})' if direction == 'video' else ''}"
    tidal_title = f"Rivals{f' ({variant})' if direction == 'tidal' else ''}"
    candidate = _tidal_candidate(tidal_title, "AZALI")

    assert select_confident_youtube_tidal_track(video_title, "AZALI", [candidate]) is None


def test_youtube_tidal_match_accepts_equal_recording_variant() -> None:
    candidate = _tidal_candidate("Rivals (Acoustic)", "AZALI")

    assert select_confident_youtube_tidal_track("AZALI - Rivals (Acoustic)", "AZALI", [candidate]) is candidate


@pytest.mark.parametrize(
    ("video_variant", "tidal_variant"),
    [("Remaster", "Remastered"), ("Remastered", "Remaster"), ("Slowed", "Slowed Down"), ("Slowed Down", "Slowed")],
)
def test_youtube_tidal_match_canonicalizes_recording_variant_aliases(
    video_variant: str, tidal_variant: str
) -> None:
    candidate = _tidal_candidate(f"Rivals ({tidal_variant})", "AZALI")

    assert select_confident_youtube_tidal_track(
        f"AZALI - Rivals ({video_variant})", "AZALI", [candidate]
    ) is candidate


def test_youtube_tidal_match_canonicalizes_hyphenated_sped_up() -> None:
    candidate = _tidal_candidate("Rivals (Sped Up)", "AZALI")

    assert select_confident_youtube_tidal_track(
        "AZALI - Rivals (Sped-up)", "AZALI", [candidate]
    ) is candidate


def test_youtube_tidal_match_selects_best_eligible_candidate_instead_of_first() -> None:
    weaker = _tidal_candidate("Rivals", "AZALI feat. Someone")
    stronger = _tidal_candidate("AZALI Rivals", "AZALI")

    assert select_confident_youtube_tidal_track("AZALI - Rivals", "AZALI", [weaker, stronger]) is stronger


@pytest.mark.parametrize(
    "video_title",
    ["Lana Del Rey - Love Song (Official Audio)", "Love Song"],
)
@pytest.mark.parametrize("exact_first", [False, True])
def test_youtube_tidal_match_prefers_complete_title_regardless_of_catalog_order(
    video_title: str, exact_first: bool,
) -> None:
    shorter = _tidal_candidate("Love", "Lana Del Rey")
    exact = _tidal_candidate("Love Song", "Lana Del Rey")
    candidates = [exact, shorter] if exact_first else [shorter, exact]

    assert select_confident_youtube_tidal_track(video_title, "Lana Del Rey", candidates) is exact


@pytest.mark.parametrize(
    ("video_title", "tidal_title"),
    [
        ("Lana Del Rey - Love Song (Official Audio)", "Love"),
        ("Love Song", "Love"),
        ("Love", "Love Song"),
        ("Lana Del Rey - Love Song Extended", "Love Song"),
        ("Lana Del Rey - Song Love", "Love Song"),
    ],
)
def test_youtube_tidal_match_rejects_incomplete_or_reordered_title(
    video_title: str, tidal_title: str,
) -> None:
    candidate = _tidal_candidate(tidal_title, "Lana Del Rey")

    assert select_confident_youtube_tidal_track(video_title, "Lana Del Rey", [candidate]) is None


@pytest.mark.parametrize(
    ("video_title", "channel", "tidal_title", "artist"),
    [
        ("Love Song", "Lana Del Rey - Topic", "Love Song", "Lana Del Rey"),
        ("Lana Del Rey - Love Song [Official Music Video]", "Record Label", "Love Song", "Lana Del Rey"),
        ("Love Song - Lana Del Rey", "Record Label", "Love Song", "Lana Del Rey"),
        ("宇多田ヒカル - 光 (Official Audio)", "宇多田ヒカル", "光", "宇多田ヒカル"),
        ("光", "宇多田ヒカル", "光", "宇多田ヒカル"),
    ],
)
def test_youtube_tidal_match_preserves_complete_title_with_artist_and_display_markers(
    video_title: str, channel: str, tidal_title: str, artist: str,
) -> None:
    candidate = _tidal_candidate(tidal_title, artist)

    assert select_confident_youtube_tidal_track(video_title, channel, [candidate]) is candidate


@pytest.mark.asyncio
async def test_unverifiable_playlist_owner_is_not_returned_for_write(cog) -> None:
    playlist = SimpleNamespace(creator=None)
    with patch.object(type(cog.tidal), "get_playlist", new=AsyncMock(return_value=playlist)):
        assert await cog.tidal.get_user_playlist_by_id("playlist-id") is None


@pytest.mark.asyncio
async def test_track_start_publishes_native_metadata(cog, native_session) -> None:
    entry = make_entry(337293380, title="Next Track", artist="Next Artist")
    native_session.current = entry
    cog._schedule_controller_recommendations = MagicMock()
    with patch.object(type(cog), "_resend_controller_for_track_start", new=AsyncMock()) as resend:
        await cog.track_started(23, entry)
    assert cog._current_meta[23] == entry.meta
    resend.assert_awaited_once_with(guild_id=23)
    cog._schedule_controller_recommendations.assert_called_once_with(23)


@pytest.mark.asyncio
async def test_get_url_is_preferred_without_calling_get_stream(cog) -> None:
    class Track:
        id = 530850206

        def __init__(self) -> None:
            self.get_stream_calls = 0

        def get_url(self) -> str:
            return "https://stream/legacy"

        def get_stream(self):
            self.get_stream_calls += 1
            raise AssertionError("get_stream must not be called when get_url works")

    track = Track()
    with patch.object(type(cog.tidal), "get_track", new=AsyncMock(return_value=track)):
        assert await cog.tidal.get_stream_url(track) == "https://stream/legacy"

    assert track.get_stream_calls == 0

@pytest.mark.asyncio
async def test_failed_playback_reports_static_error_without_advancing_state(cog, native_session):
    current = make_entry()
    native_session.current = current
    channel = SimpleNamespace(send=AsyncMock())
    cog._playback_channels[1] = channel
    cog._current_meta[1] = current.meta
    await cog.track_failed(1, make_entry(2), "https://media.example/?token=secret")
    channel.send.assert_awaited_once()
    assert "secret" not in channel.send.call_args.kwargs["embed"].description
    assert cog._current_meta[1] == current.meta
    native_session.skip.assert_not_awaited()
    native_session.enqueue.assert_not_awaited()
