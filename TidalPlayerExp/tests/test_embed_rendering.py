"""Display vocabulary for the queue card and the Components V2 controller."""
from __future__ import annotations

import importlib
from typing import Any

import pytest


@pytest.fixture()
def make_queue_embed(cog):
    return importlib.import_module("TidalPlayerExp.ui.embeds").make_queue_embed


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
    source: str | None = None,
) -> dict[str, Any]:
    meta = {
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
    if source is not None:
        meta["source"] = source
    return meta


class TestQueueEmbed:
    def test_description_contains_track_title(self, make_queue_embed):
        embed = make_queue_embed(_make_meta(title="Blinding Lights"))
        assert "Blinding Lights" in embed.description

    def test_description_contains_artist(self, make_queue_embed):
        embed = make_queue_embed(_make_meta(artist="The Weeknd"))
        assert "The Weeknd" in embed.description

    def test_description_contains_album_in_italics(self, make_queue_embed):
        embed = make_queue_embed(_make_meta(album="After Hours"))
        # album should appear wrapped in underscores (markdown italics)
        assert "_After Hours_" in embed.description

    def test_description_no_album_section_when_none(self, make_queue_embed):
        embed = make_queue_embed(_make_meta(album=None))
        lines = embed.description.split("\n")
        # Without album there should be exactly 2 lines: title + artist
        assert len(lines) == 2

    def test_thumbnail_set_when_image_present(self, make_queue_embed):
        url = "https://example.com/cover.jpg"
        embed = make_queue_embed(_make_meta(image=url))
        assert embed.thumbnail.url == url

    def test_no_thumbnail_when_image_none(self, make_queue_embed):
        embed = make_queue_embed(_make_meta(image=None))
        assert embed.thumbnail.url is None

    def test_single_link_field(self, make_queue_embed):
        embed = make_queue_embed(_make_meta())
        assert len(embed.fields) == 1
        assert _field_value(embed, "Open in TIDAL") is not None


def test_youtube_queue_embed_uses_source_link(make_queue_embed) -> None:
    url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    embed = make_queue_embed(_make_meta(source="YouTube", share_url=url))

    assert _field_value(embed, "Open in YouTube") == f"[Listen]({url})"
    assert _field_value(embed, "Open in TIDAL") is None


def test_controller_track_info_is_source_aware(cog) -> None:
    controller = importlib.import_module("TidalPlayerExp.ui.controller")
    url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"

    info = controller._track_info(
        _make_meta(source="YouTube", quality="YouTube", share_url=url),
        autoplay_enabled=False,
    )

    assert "## Playing from YouTube" in info
    assert f"[Open in YouTube]({url})" in info


def test_controller_track_info_defaults_to_tidal(cog) -> None:
    controller = importlib.import_module("TidalPlayerExp.ui.controller")
    url = "https://listen.tidal.com/track/12345"

    info = controller._track_info(_make_meta(share_url=url), autoplay_enabled=True)

    assert "## Playing from Tidal" in info
    assert f"[Open in TIDAL]({url})" in info


@pytest.mark.parametrize("source,label", [("soundcloud", "SoundCloud"), ("bandcamp", "Bandcamp")])
def test_public_audio_source_labels_do_not_claim_tidal_quality(cog, source, label):
    controller = importlib.import_module("TidalPlayerExp.ui.controller")
    info = controller._track_info(
        _make_meta(source=source, quality="Public audio", share_url=None),
        autoplay_enabled=False,
    )

    assert f"## Playing from {label}" in info
    assert f"**Source:** {label} audio" in info
    assert "**Catalog quality:**" not in info
    assert "**Delivery:** Discord Opus" in info


def test_controller_quality_labels_match_catalog_states(cog) -> None:
    controller = importlib.import_module("TidalPlayerExp.ui.controller")

    assert "**Catalog quality:** LOSSLESS (FLAC)" in controller._track_info(
        _make_meta(quality="LOSSLESS"), autoplay_enabled=False
    )
    info = controller._track_info(
        _make_meta(quality="HI_RES_LOSSLESS", audio_resolution="HI-RES LOSSLESS (24-bit / 96kHz)"),
        autoplay_enabled=False,
    )
    assert "**Catalog quality:** HI-RES LOSSLESS (24-bit / 96kHz)" in info


@pytest.mark.parametrize("duration", [None, 0, -1])
def test_unknown_duration_does_not_claim_zero_length(cog, make_queue_embed, duration) -> None:
    controller = importlib.import_module("TidalPlayerExp.ui.controller")
    meta = _make_meta(duration=duration, source="YouTube")

    assert "**Duration:** Unknown" in controller._track_info(meta, autoplay_enabled=False)
    assert "Duration: Unknown" in _footer_text(make_queue_embed(meta))


def test_controller_separates_catalog_quality_from_discord_delivery(cog) -> None:
    controller = importlib.import_module("TidalPlayerExp.ui.controller")

    info = controller._track_info(
        _make_meta(quality="HI_RES_LOSSLESS", audio_resolution="24-bit / 96kHz"),
        autoplay_enabled=False,
    )
    assert "**Delivery:** Discord Opus" in info
    assert "**Quality:**" not in info
    assert "**Catalog quality:** 24-bit / 96kHz" in info

    yt_info = controller._track_info(
        _make_meta(source="YouTube", quality="HI_RES_LOSSLESS", audio_resolution="24-bit / 96kHz"),
        autoplay_enabled=False,
    )
    assert "LOSSLESS" not in yt_info
    assert "96kHz" not in yt_info
    assert "**Source:** YouTube audio" in yt_info
