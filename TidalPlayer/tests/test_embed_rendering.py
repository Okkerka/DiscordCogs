"""
Phase-0 characterization tests: now-playing embed rendering.

Captures the exact visual contract of _build_now_playing_embed so any
future move of this method into ui/embeds.py is verified to be
behaviour-preserving.

The embed this characterizes is the compact 'Playing from Tidal' embed
that MUST NOT be visually changed during the refactor.
"""
from __future__ import annotations

from typing import Any

import pytest


def _make_meta(
    *,
    title: str = "Good 4 U",
    artist: str = "Olivia Rodrigo",
    album: str | None = "SOUR",
    duration: int = 178,
    quality: str = "LOSSLESS",
    image: str | None = "https://example.com/cover.jpg",
    share_url: str | None = "https://listen.tidal.com/track/12345",
    audio_resolution: str | None = None,
    track_id: int | None = 12345,
) -> dict[str, Any]:
    return {
        "title": title,
        "artist": artist,
        "album": album,
        "duration": duration,
        "quality": quality,
        "image": image,
        "share_url": share_url,
        "audio_resolution": audio_resolution,
        "track_id": track_id,
    }


class TestNowPlayingEmbed:
    def test_embed_title_is_playing_from_tidal(self, cog):
        embed = cog._build_now_playing_embed(_make_meta())
        assert embed.title == "Playing from Tidal"

    def test_embed_description_contains_track_title(self, cog):
        embed = cog._build_now_playing_embed(_make_meta(title="Blinding Lights"))
        assert "Blinding Lights" in embed.description

    def test_embed_description_contains_artist(self, cog):
        embed = cog._build_now_playing_embed(_make_meta(artist="The Weeknd"))
        assert "The Weeknd" in embed.description

    def test_embed_description_contains_album_in_italics(self, cog):
        embed = cog._build_now_playing_embed(_make_meta(album="After Hours"))
        # album should appear wrapped in underscores (markdown italics)
        assert "_After Hours_" in embed.description

    def test_embed_description_no_album_section_when_none(self, cog):
        embed = cog._build_now_playing_embed(_make_meta(album=None))
        lines = embed.description.split("\n")
        # Without album there should be exactly 2 lines: title + artist
        assert len(lines) == 2

    def test_embed_has_quality_field(self, cog):
        embed = cog._build_now_playing_embed(_make_meta(quality="LOSSLESS"))
        field_names = [f["name"] for f in embed.fields]
        assert "Quality" in field_names

    def test_quality_label_lossless(self, cog):
        embed = cog._build_now_playing_embed(_make_meta(quality="LOSSLESS"))
        quality_field = next(f for f in embed.fields if f["name"] == "Quality")
        assert quality_field["value"] == "LOSSLESS (FLAC)"

    def test_quality_label_hi_res(self, cog):
        embed = cog._build_now_playing_embed(_make_meta(quality="HI_RES_LOSSLESS"))
        quality_field = next(f for f in embed.fields if f["name"] == "Quality")
        assert quality_field["value"] == "HI-RES LOSSLESS (FLAC)"

    def test_audio_resolution_overrides_quality_label(self, cog):
        meta = _make_meta(
            quality="HI_RES_LOSSLESS",
            audio_resolution="HI-RES LOSSLESS (24-bit / 96kHz)",
        )
        embed = cog._build_now_playing_embed(meta)
        quality_field = next(f for f in embed.fields if f["name"] == "Quality")
        assert quality_field["value"] == "HI-RES LOSSLESS (24-bit / 96kHz)"

    def test_embed_has_tidal_link_field(self, cog):
        embed = cog._build_now_playing_embed(_make_meta())
        field_names = [f["name"] for f in embed.fields]
        assert "Open in TIDAL" in field_names

    def test_tidal_link_field_value_format(self, cog):
        share_url = "https://listen.tidal.com/track/12345"
        embed = cog._build_now_playing_embed(_make_meta(share_url=share_url))
        link_field = next(f for f in embed.fields if f["name"] == "Open in TIDAL")
        assert f"[Listen]({share_url})" == link_field["value"]

    def test_no_tidal_link_field_when_no_share_url(self, cog):
        embed = cog._build_now_playing_embed(_make_meta(share_url=None))
        field_names = [f["name"] for f in embed.fields]
        assert "Open in TIDAL" not in field_names

    def test_footer_contains_duration(self, cog):
        embed = cog._build_now_playing_embed(_make_meta(duration=178))
        # 178 seconds = 02:58
        assert "02:58" in embed._footer

    def test_footer_duration_hours(self, cog):
        embed = cog._build_now_playing_embed(_make_meta(duration=3661))
        # 3661 seconds = 1:01:01
        assert "1:01:01" in embed._footer

    def test_thumbnail_set_when_image_present(self, cog):
        url = "https://example.com/cover.jpg"
        embed = cog._build_now_playing_embed(_make_meta(image=url))
        assert embed._thumbnail == url

    def test_no_thumbnail_when_image_none(self, cog):
        embed = cog._build_now_playing_embed(_make_meta(image=None))
        assert embed._thumbnail is None

    def test_field_count_with_share_url(self, cog):
        """Exactly 2 fields: Quality + Open in TIDAL."""
        embed = cog._build_now_playing_embed(_make_meta())
        assert len(embed.fields) == 2

    def test_field_count_without_share_url(self, cog):
        """Exactly 1 field: Quality only."""
        embed = cog._build_now_playing_embed(_make_meta(share_url=None))
        assert len(embed.fields) == 1

    def test_title_is_bold_in_description(self, cog):
        embed = cog._build_now_playing_embed(_make_meta(title="Levitating"))
        assert "**Levitating**" in embed.description
