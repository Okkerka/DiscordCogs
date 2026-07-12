"""
Phase-0 characterization tests: URL and ISRC parsing.

Documents CURRENT behaviour of the regex-based parsers as a regression
baseline.  When Phase-2 replaces these with strict urllib.parse.urlsplit
parsers the failing cases below will be updated (not silently green-lit).

Each test is explicitly labelled STRICT (must match) or PERMISSIVE
(currently matches but should be rejected by the future strict parser —
marked with a comment so the engineer knows to flip the assertion).
"""
from __future__ import annotations

import importlib
import sys

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

MODULE_NAME = "TidalPlayer.tidalplayer"


@pytest.fixture(scope="module")
def mod():
    sys.modules.pop(MODULE_NAME, None)
    return importlib.import_module(MODULE_NAME)


# ---------------------------------------------------------------------------
# Tidal URL patterns
# ---------------------------------------------------------------------------

class TestTidalTrackPattern:
    VALID = [
        "https://tidal.com/browse/track/12345678",
        "https://tidal.com/track/12345678",
        "http://tidal.com/browse/track/12345678",  # PERMISSIVE: http accepted
    ]
    INVALID = [
        "https://tidal.com/browse/album/12345678",
        "https://tidal.com/browse/playlist/abc-def",
        "https://listen.tidal.com/track/12345678",  # subdomain — currently rejected
    ]

    def test_valid_matches(self, mod):
        pattern = mod.TIDAL_URL_PATTERNS["track"]
        for url in self.VALID:
            assert pattern.search(url), f"Expected match: {url}"

    def test_invalid_no_match(self, mod):
        pattern = mod.TIDAL_URL_PATTERNS["track"]
        for url in self.INVALID[:2]:
            assert not pattern.search(url), f"Expected no match: {url}"

    def test_extracts_numeric_id(self, mod):
        pattern = mod.TIDAL_URL_PATTERNS["track"]
        m = pattern.search("https://tidal.com/browse/track/99887766")
        assert m and m.group(1) == "99887766"

    def test_legacy_pattern_accepts_listen_subdomain(self, mod):
        """Phase 1 preserves the monolith's permissive regular-expression match."""
        assert mod.TIDAL_URL_PATTERNS["track"].search("https://listen.tidal.com/track/12345678")


class TestTidalAlbumPattern:
    def test_valid(self, mod):
        pattern = mod.TIDAL_URL_PATTERNS["album"]
        assert pattern.search("https://tidal.com/browse/album/11223344")

    def test_extracts_id(self, mod):
        pattern = mod.TIDAL_URL_PATTERNS["album"]
        m = pattern.search("https://tidal.com/browse/album/11223344")
        assert m and m.group(1) == "11223344"


class TestTidalPlaylistPattern:
    def test_valid_uuid_style(self, mod):
        pattern = mod.TIDAL_URL_PATTERNS["playlist"]
        url = "https://tidal.com/browse/playlist/a1b2c3d4-e5f6-7890-abcd-ef1234567890"
        assert pattern.search(url)

    def test_extracts_uuid(self, mod):
        pattern = mod.TIDAL_URL_PATTERNS["playlist"]
        pid = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
        m = pattern.search(f"https://tidal.com/browse/playlist/{pid}")
        assert m and m.group(1) == pid


class TestTidalMixPattern:
    def test_valid(self, mod):
        pattern = mod.TIDAL_URL_PATTERNS["mix"]
        assert pattern.search("https://tidal.com/browse/mix/01234ABCDE")


class TestTidalVideoPattern:
    def test_valid(self, mod):
        pattern = mod.TIDAL_URL_PATTERNS["video"]
        assert pattern.search("https://tidal.com/browse/video/55443322")


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
# YouTube playlist pattern
# ---------------------------------------------------------------------------

class TestYouTubePlaylistPattern:
    def test_valid_watch_with_list(self, mod):
        assert mod.YOUTUBE_PLAYLIST_PATTERN.search(
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=PLrEnWoR732-BHrPp_Pm8_VleD68f9s14-"
        )

    def test_valid_playlist_url(self, mod):
        assert mod.YOUTUBE_PLAYLIST_PATTERN.search(
            "https://www.youtube.com/playlist?list=PLrEnWoR732-BHrPp_Pm8_VleD68f9s14-"
        )

    def test_extracts_list_id(self, mod):
        m = mod.YOUTUBE_PLAYLIST_PATTERN.search(
            "https://www.youtube.com/playlist?list=PLrEnWoR732-BHrPp_Pm8_VleD68f9s14-"
        )
        assert m and m.group(1) == "PLrEnWoR732-BHrPp_Pm8_VleD68f9s14-"

    def test_no_match_plain_watch(self, mod):
        # A plain watch URL without a list param must NOT match
        assert not mod.YOUTUBE_PLAYLIST_PATTERN.search(
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
        )


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
