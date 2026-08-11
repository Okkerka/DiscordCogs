"""URL, ISRC, and formatting regression tests."""
from __future__ import annotations

import importlib
import sys

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from TidalPlayer.providers.urls import (
    MalformedProviderURL,
    ProviderKind,
    ProviderURL,
    parse_provider_url,
)

MODULE_NAME = "TidalPlayer.tidalplayer"


@pytest.mark.parametrize(
    "url",
    [
        "https://tidal.com/track/not-a-number",
        "https://tidal.com/album/12x",
        "https://tidal.com/video/-1",
    ],
)
def test_strict_tidal_numeric_media_rejects_non_numeric_ids(url: str) -> None:
    with pytest.raises(MalformedProviderURL):
        parse_provider_url(url)


@pytest.fixture(scope="module")
def mod():
    sys.modules.pop(MODULE_NAME, None)
    return importlib.import_module(MODULE_NAME)


# ---------------------------------------------------------------------------
# Spotify patterns
# ---------------------------------------------------------------------------

class TestSpotifyPlaylistPattern:
    def test_valid(self, mod):
        assert mod.SPOTIFY_PLAYLIST_PATTERN.search(
            "https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M"
        )

    def test_no_match_track(self, mod):
        assert not mod.SPOTIFY_PLAYLIST_PATTERN.search(
            "https://open.spotify.com/track/4uLU6hMCjMI75M1A2tKUQC"
        )

    def test_extracts_id(self, mod):
        m = mod.SPOTIFY_PLAYLIST_PATTERN.search(
            "https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M"
        )
        assert m and m.group(1) == "37i9dQZF1DXcBWIGoYBM5M"


class TestSpotifyTrackPattern:
    def test_valid(self, mod):
        assert mod.SPOTIFY_TRACK_PATTERN.search(
            "https://open.spotify.com/track/4uLU6hMCjMI75M1A2tKUQC"
        )

    def test_no_match_album(self, mod):
        assert not mod.SPOTIFY_TRACK_PATTERN.search(
            "https://open.spotify.com/album/1NAmidJlEaVgA3MpcPFYGq"
        )


class TestSpotifyAlbumPattern:
    def test_valid(self, mod):
        assert mod.SPOTIFY_ALBUM_PATTERN.search(
            "https://open.spotify.com/album/1NAmidJlEaVgA3MpcPFYGq"
        )


# ---------------------------------------------------------------------------
# YouTube provider URLs
# ---------------------------------------------------------------------------

YOUTUBE_VIDEO_ID = "dQw4w9WgXcQ"


@pytest.mark.parametrize(
    "url",
    [
        f"https://www.youtube.com/watch?v={YOUTUBE_VIDEO_ID}",
        f"https://m.youtube.com/watch?v={YOUTUBE_VIDEO_ID}",
        f"https://music.youtube.com/watch?v={YOUTUBE_VIDEO_ID}",
        f"https://youtu.be/{YOUTUBE_VIDEO_ID}",
        f"https://www.youtube.com/shorts/{YOUTUBE_VIDEO_ID}",
        f"https://youtube.com/live/{YOUTUBE_VIDEO_ID}",
        f"https://youtube.com/embed/{YOUTUBE_VIDEO_ID}",
    ],
)
def test_strict_youtube_video_shapes(url: str) -> None:
    assert parse_provider_url(url) == ProviderURL(
        ProviderKind.YOUTUBE, "video", YOUTUBE_VIDEO_ID
    )


def test_strict_youtube_normalizes_one_discord_angle_pair() -> None:
    assert parse_provider_url(
        f"  <https://www.youtube.com/watch?v={YOUTUBE_VIDEO_ID}>  "
    ) == ProviderURL(ProviderKind.YOUTUBE, "video", YOUTUBE_VIDEO_ID)


@pytest.mark.parametrize("query", ["<azali rivals", "azali rivals>"])
def test_non_url_search_text_with_unmatched_angles_remains_a_search(query: str) -> None:
    assert parse_provider_url(query) is None


def test_strict_youtube_playlist_takes_precedence_over_video() -> None:
    playlist_id = "PLrEnWoR732-BHrPp_Pm8_VleD68f9s14-"
    assert parse_provider_url(
        f"https://www.youtube.com/watch?v={YOUTUBE_VIDEO_ID}&list={playlist_id}"
    ) == ProviderURL(ProviderKind.YOUTUBE, "playlist", playlist_id)


@pytest.mark.parametrize(
    "url",
    [
        "http://www.youtube.com/watch?v=dQw4w9WgXcQ",
        "https://user@example.com/watch?v=dQw4w9WgXcQ",
        "https://youtube.example/watch?v=dQw4w9WgXcQ",
        "https://www.youtube.com/watch?v=too-short",
        "https://www.youtube.com/watch?v=dQw4w9WgXc!",
        "https://youtu.be/dQw4w9WgXcQ/extra",
        "https://www.youtube.com/channel/dQw4w9WgXcQ",
        "<<https://www.youtube.com/watch?v=dQw4w9WgXcQ>>",
    ],
)
def test_strict_youtube_rejects_malformed_or_unsupported_urls(url: str) -> None:
    with pytest.raises(MalformedProviderURL):
        parse_provider_url(url)


# ---------------------------------------------------------------------------
# ISRC pattern
# ---------------------------------------------------------------------------

class TestISRCPattern:
    VALID = [
        "isrc:USUM71703861",
        "isrc:GBF088761084",
        "ISRC:USUM71703861",  # case-insensitive flag
    ]
    INVALID = [
        "USUM71703861",       # missing prefix
        "isrc:USUM717038",    # too short
        "isrc:12345678901234",  # wrong format
        "isrc:",              # empty
    ]

    def test_valid_isrc(self, mod):
        for s in self.VALID:
            assert mod.ISRC_PATTERN.match(s), f"Expected ISRC match: {s}"

    def test_invalid_isrc(self, mod):
        for s in self.INVALID:
            assert not mod.ISRC_PATTERN.match(s), f"Expected no ISRC match: {s}"

    def test_extracts_isrc_code(self, mod):
        m = mod.ISRC_PATTERN.match("isrc:USUM71703861")
        assert m and m.group(1).upper() == "USUM71703861"

    @given(st.text(max_size=40))
    @settings(max_examples=200)
    def test_no_crash_on_arbitrary_input(self, mod, s: str):
        """ISRC regex must never raise on arbitrary strings."""
        mod.ISRC_PATTERN.match(s)  # must not raise


# ---------------------------------------------------------------------------
# truncate helper
# ---------------------------------------------------------------------------

class TestTruncate:
    def test_short_string_unchanged(self, mod):
        assert mod.truncate("hello", 10) == "hello"

    def test_exact_length_unchanged(self, mod):
        assert mod.truncate("hello", 5) == "hello"

    def test_long_string_truncated(self, mod):
        result = mod.truncate("a" * 20, 10)
        assert result == "a" * 7 + "..."
        assert len(result) == 10

    @given(st.text(max_size=300), st.integers(min_value=3, max_value=300))
    @settings(max_examples=300)
    def test_result_never_exceeds_limit(self, mod, text: str, limit: int):
        result = mod.truncate(text, limit)
        assert len(result) <= limit
