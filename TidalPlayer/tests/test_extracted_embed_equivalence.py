"""Characterize the Phase-1 canonical embed factory through the active cog."""
from __future__ import annotations

from typing import Any

import pytest

def _snapshot(embed: Any) -> tuple[Any, ...]:
    return (
        embed.title,
        embed.description,
        embed.color,
        tuple((field["name"], field["value"], field["inline"]) for field in embed.fields),
        embed._footer,
        embed._thumbnail,
    )


@pytest.mark.parametrize(
    "meta",
    [
        {
            "title": "Normal Track", "artist": "Artist", "album": "Album",
            "duration": 178, "quality": "LOSSLESS", "image": "https://example.com/a.jpg",
            "share_url": "https://listen.tidal.com/track/1", "audio_resolution": None,
            "track_id": 1,
        },
        {
            "title": "Long Track", "artist": "Artist", "album": None,
            "duration": 3661, "quality": "HI_RES_LOSSLESS", "image": None,
            "share_url": None, "audio_resolution": None, "track_id": 2,
        },
    ],
    ids=["album-url-thumbnail-standard-duration", "no-album-no-url-no-thumbnail-hires-hour"],
)
def test_active_cog_rendering_matches_extracted_canonical_factory(cog, meta: dict[str, Any]) -> None:
    """The active method is a thin compatibility wrapper over the extracted factory."""
    from TidalPlayer.ui.embeds import make_now_playing_embed

    assert _snapshot(cog._build_now_playing_embed(meta)) == _snapshot(make_now_playing_embed(meta))


def test_factory_preserves_audio_resolution_override(cog) -> None:
    from TidalPlayer.ui.embeds import make_now_playing_embed

    embed = make_now_playing_embed(
        {
            "title": "Track", "artist": "Artist", "album": None, "duration": 60,
            "quality": "HI_RES_LOSSLESS", "image": None, "share_url": None,
            "audio_resolution": "HI-RES LOSSLESS (24-bit / 96kHz)", "track_id": 1,
        }
    )
    assert embed.fields[0]["value"] == "HI-RES LOSSLESS (24-bit / 96kHz)"
