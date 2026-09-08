"""Practical media share links preserve routing and strict provider boundaries."""

import pytest

from TidalPlayerExp.providers.urls import (
    MalformedProviderURL,
    ProviderKind,
    ProviderURL,
    parse_provider_url,
)


@pytest.mark.parametrize("host", ["tidal.com", "www.tidal.com", "listen.tidal.com"])
@pytest.mark.parametrize("prefix", ["", "browse/"])
@pytest.mark.parametrize("suffix", ["/u", "/u/", "/", "?u", "/u?utm_source=share"])
@pytest.mark.parametrize("kind,identifier", [
    ("track", "388177717"),
    ("album", "303636706"),
    ("video", "120274048"),
    ("playlist", "37d994d7-8890-4e24-8e01-bc8be92477aa"),
    ("mix", "001abcdef0123456789"),
])
def test_tidal_share_variants_keep_the_media_identity(host, prefix, suffix, kind, identifier):
    assert parse_provider_url(f"https://{host}/{prefix}{kind}/{identifier}{suffix}") == ProviderURL(
        ProviderKind.TIDAL, kind, identifier,
    )


@pytest.mark.parametrize("url", [
    "https://youtube.com/watch/?v=dQw4w9WgXcQ&t=45s",
    "https://www.youtube.com/watch/?v=dQw4w9WgXcQ&list=PL-example&index=3",
    "https://m.youtube.com/watch/?v=dQw4w9WgXcQ&feature=shared",
    "https://music.youtube.com/watch/?v=dQw4w9WgXcQ&si=share-id",
    "https://youtu.be/dQw4w9WgXcQ/?si=share-id&t=90",
    "https://www.youtube.com/shorts/dQw4w9WgXcQ/?feature=share",
    "https://youtube.com/live/dQw4w9WgXcQ/?list=PL-example",
    "https://youtube.com/embed/dQw4w9WgXcQ/?start=90#t=1m30s",
])
def test_youtube_share_variants_keep_video_routing(url):
    assert parse_provider_url(url) == ProviderURL(ProviderKind.YOUTUBE, "video", "dQw4w9WgXcQ")


def test_explicit_youtube_playlist_accepts_trailing_slash_and_tracking():
    assert parse_provider_url("https://music.youtube.com/playlist/?list=PL-example&si=share-id") == ProviderURL(
        ProviderKind.YOUTUBE, "playlist", "PL-example",
    )


@pytest.mark.parametrize("host", ["youtube-nocookie.com", "www.youtube-nocookie.com"])
def test_privacy_enhanced_embed_keeps_video_identity(host):
    assert parse_provider_url(f"https://{host}/embed/dQw4w9WgXcQ/?start=10") == ProviderURL(
        ProviderKind.YOUTUBE, "video", "dQw4w9WgXcQ",
    )


@pytest.mark.parametrize("url", [
    "https://tidal.com/track/388177717/u/extra",
    "https://tidal.com/track/388177717//u",
    "https://tidal.com//track/388177717",
    "https://tidal.com/browse//track/388177717/u",
    "https://tidal.com/track/388177717//",
    "https://tidal.com/track/１２３/u",
    "https://tidal.com/track/0/u",
    "https://tidal.com/playlist/../u",
    "https://tidal.com/mix/bad%2Fidentifier/u",
    "https://tidal.com/artist/123/u",
    "https://tidal.com:444/track/388177717/u",
    "https://user@tidal.com/track/388177717/u",
    "https://tidal.com.evil.test/track/388177717/u",
    "https://youtube.com:444/watch?v=dQw4w9WgXcQ",
    "https://youtube.com/watch//?v=dQw4w9WgXcQ",
    "https://youtu.be//dQw4w9WgXcQ/",
    "https://youtube.com/watch/?v=dQw4w9WgXcQ&v=dQw4w9WgXcQ",
    "https://youtube.com/watch/?v=dQw4w9WgXcQ&list=one&list=two",
    "https://youtube.com/playlist/?list=one&v=dQw4w9WgXcQ",
    "https://youtube.com/wat\nch?v=dQw4w9WgXcQ",
    "https://youtube.com/wa\ttch?v=dQw4w9WgXcQ",
    "https://youtube.com/@artist",
    "https://youtube-nocookie.com/watch?v=dQw4w9WgXcQ",
    "https://youtube-nocookie.com/playlist?list=PL-example",
    "https://youtube-nocookie.com.evil.test/embed/dQw4w9WgXcQ",
    "https://short.example/media",
])
def test_share_tolerance_does_not_accept_ambiguous_or_untrusted_links(url):
    with pytest.raises(MalformedProviderURL):
        parse_provider_url(url)


def test_private_soundcloud_token_is_separate_from_display_identifier():
    parsed = parse_provider_url("https://soundcloud.com/artist/recording/s-FakeShareToken")
    assert parsed is not None
    assert parsed.identifier == "https://soundcloud.com/artist/recording"
    assert parsed.secret_token == "s-FakeShareToken"
    assert "FakeShareToken" not in repr(parsed)
