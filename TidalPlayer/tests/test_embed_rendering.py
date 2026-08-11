"""
Characterization tests for the canonical now-playing embed factory.

Captures the exact visual contract of ui.embeds.make_now_playing_embed so
future changes remain behaviour-preserving.

The embed this characterizes is the compact 'Playing from Tidal' embed
that MUST NOT be visually changed during the refactor.
"""
from __future__ import annotations

import importlib
from typing import Any

import pytest


@pytest.fixture()
def make_now_playing_embed(cog):
    return importlib.import_module("TidalPlayer.ui.embeds").make_now_playing_embed


def _field_value(embed, name: str) -> str | None:
    for field in embed.fields:
        field_name = field.get("name") if isinstance(field, dict) else field.name
        if field_name == name:
            return field.get("value") if isinstance(field, dict) else field.value
    return None


def _footer_text(embed) -> str:
    footer = getattr(embed, "footer", None)
    if text := getattr(footer, "text", None):
        return text
    raw_footer = getattr(embed, "_footer", "")
    return raw_footer.get("text", "") if isinstance(raw_footer, dict) else raw_footer


def _thumbnail_url(embed) -> str | None:
    thumbnail = getattr(embed, "thumbnail", None)
    if url := getattr(thumbnail, "url", None):
        return url
    raw_thumbnail = getattr(embed, "_thumbnail", None)
    return raw_thumbnail.get("url") if isinstance(raw_thumbnail, dict) else raw_thumbnail

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
    def test_embed_title_is_playing_from_tidal(self, make_now_playing_embed):
        embed = make_now_playing_embed(_make_meta())
        assert embed.title == "Playing from Tidal"

    def test_embed_description_contains_track_title(self, make_now_playing_embed):
        embed = make_now_playing_embed(_make_meta(title="Blinding Lights"))
        assert "Blinding Lights" in embed.description

    def test_embed_description_contains_artist(self, make_now_playing_embed):
        embed = make_now_playing_embed(_make_meta(artist="The Weeknd"))
        assert "The Weeknd" in embed.description

    def test_embed_description_contains_album_in_italics(self, make_now_playing_embed):
        embed = make_now_playing_embed(_make_meta(album="After Hours"))
        # album should appear wrapped in underscores (markdown italics)
        assert "_After Hours_" in embed.description

    def test_embed_description_no_album_section_when_none(self, make_now_playing_embed):
        embed = make_now_playing_embed(_make_meta(album=None))
        lines = embed.description.split("\n")
        # Without album there should be exactly 2 lines: title + artist
        assert len(lines) == 2

    def test_embed_has_quality_field(self, make_now_playing_embed):
        embed = make_now_playing_embed(_make_meta(quality="LOSSLESS"))
        assert _field_value(embed, "Quality") is not None

    def test_quality_label_lossless(self, make_now_playing_embed):
        embed = make_now_playing_embed(_make_meta(quality="LOSSLESS"))
        assert _field_value(embed, "Quality") == "LOSSLESS (FLAC)"

    def test_quality_label_hi_res(self, make_now_playing_embed):
        embed = make_now_playing_embed(_make_meta(quality="HI_RES_LOSSLESS"))
        assert _field_value(embed, "Quality") == "HI-RES LOSSLESS (FLAC)"

    def test_audio_resolution_overrides_quality_label(self, make_now_playing_embed):
        meta = _make_meta(
            quality="HI_RES_LOSSLESS",
            audio_resolution="HI-RES LOSSLESS (24-bit / 96kHz)",
        )
        embed = make_now_playing_embed(meta)
        assert _field_value(embed, "Quality") == "HI-RES LOSSLESS (24-bit / 96kHz)"

    def test_embed_has_tidal_link_field(self, make_now_playing_embed):
        embed = make_now_playing_embed(_make_meta())
        assert _field_value(embed, "Open in TIDAL") is not None

    def test_tidal_link_field_value_format(self, make_now_playing_embed):
        share_url = "https://listen.tidal.com/track/12345"
        embed = make_now_playing_embed(_make_meta(share_url=share_url))
        assert _field_value(embed, "Open in TIDAL") == f"[Listen]({share_url})"

    def test_no_tidal_link_field_when_no_share_url(self, make_now_playing_embed):
        embed = make_now_playing_embed(_make_meta(share_url=None))
        assert _field_value(embed, "Open in TIDAL") is None

    def test_footer_contains_duration(self, make_now_playing_embed):
        embed = make_now_playing_embed(_make_meta(duration=178))
        # 178 seconds = 02:58
        assert "02:58" in _footer_text(embed)

    def test_footer_duration_hours(self, make_now_playing_embed):
        embed = make_now_playing_embed(_make_meta(duration=3661))
        # 3661 seconds = 1:01:01
        assert "1:01:01" in _footer_text(embed)

    def test_thumbnail_set_when_image_present(self, make_now_playing_embed):
        url = "https://example.com/cover.jpg"
        embed = make_now_playing_embed(_make_meta(image=url))
        assert _thumbnail_url(embed) == url

    def test_no_thumbnail_when_image_none(self, make_now_playing_embed):
        embed = make_now_playing_embed(_make_meta(image=None))
        assert _thumbnail_url(embed) is None

    def test_field_count_with_share_url(self, make_now_playing_embed):
        """Exactly 2 fields: Quality + Open in TIDAL."""
        embed = make_now_playing_embed(_make_meta())
        assert len(embed.fields) == 2

    def test_field_count_without_share_url(self, make_now_playing_embed):
        """Exactly 1 field: Quality only."""
        embed = make_now_playing_embed(_make_meta(share_url=None))
        assert len(embed.fields) == 1

    def test_title_is_bold_in_description(self, make_now_playing_embed):
        embed = make_now_playing_embed(_make_meta(title="Levitating"))
        assert "**Levitating**" in embed.description
