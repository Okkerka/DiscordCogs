"""Secret SoundCloud shares remain playable without becoming public metadata."""

from __future__ import annotations

from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from TidalPlayerExp.domain.public_audio_urls import parse_public_audio_url
from TidalPlayerExp.playback.errors import SourceResolutionError
from TidalPlayerExp.playback.models import SourceKind, SourceReference
from TidalPlayerExp.providers.urls import parse_provider_url
from TidalPlayerExp.tests.test_public_audio import (
    SOUNDCLOUD,
    _metadata,
    _resolver,
    _source,
)

TOKEN = "s-TestSecret123"
SHARE = f"{SOUNDCLOUD}/{TOKEN}"


def test_shared_url_separates_secret_from_public_identity():
    provider, kind, url, token = parse_public_audio_url(SHARE + "?utm_source=share")
    assert (provider, kind, url, token) == ("soundcloud", "track", SOUNDCLOUD, TOKEN)
    parsed = parse_provider_url(SHARE)
    assert parsed.identifier == SOUNDCLOUD and parsed.secret_token == TOKEN
    reference = SourceReference(SourceKind.SOUNDCLOUD, parsed.identifier, secret_token=parsed.secret_token)
    assert TOKEN not in repr(parsed) + repr(reference)
    assert reference != replace(reference, secret_token=None)


@pytest.mark.parametrize("url", [
    SOUNDCLOUD + "/s-", SOUNDCLOUD + "/s-secret/extra", SOUNDCLOUD + "/s-%2Fsecret",
    SOUNDCLOUD + "/s-secret\\escape", SOUNDCLOUD + "/s-secret\n",
    "https://soundcloud.com/artist/sets/set/s-secret",
    "https://soundcloud.com.evil.test/artist/song/s-secret",
    "https://user:pass@soundcloud.com/artist/song/s-secret",
    SOUNDCLOUD + "/s-secret?secret_token=s-different",
    SOUNDCLOUD + "/s-secret#fragment",
])
def test_shared_url_does_not_relax_other_url_boundaries(url):
    with pytest.raises(ValueError):
        parse_public_audio_url(url)


@pytest.mark.parametrize("kind,identifier", [
    (SourceKind.YOUTUBE, "Y8lBsNGPGSQ"), (SourceKind.TIDAL, "123"),
    (SourceKind.BANDCAMP, "https://artist.bandcamp.com/track/song"),
])
def test_secret_token_is_not_accepted_for_other_providers(kind, identifier):
    with pytest.raises(ValueError):
        SourceReference(kind, identifier, secret_token=TOKEN)


@pytest.mark.asyncio
@pytest.mark.parametrize("returned_url", [SOUNDCLOUD, SHARE])
async def test_private_share_metadata_keeps_token_only_in_internal_reference(tmp_path, returned_url):
    resolver, worker, calls = _resolver(tmp_path, _metadata(returned_url, availability="private"))
    reference = SourceReference(SourceKind.SOUNDCLOUD, SOUNDCLOUD, secret_token=TOKEN)
    try:
        metadata = await resolver.fetch_metadata(reference)
        assert metadata.reference == reference
        assert metadata.meta["share_url"] is None
        assert TOKEN not in repr(metadata) + repr(dict(metadata.meta))
        assert calls[0][0][-1] == SHARE
    finally:
        await worker.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("returned_url", [SOUNDCLOUD, SHARE])
async def test_shared_audio_preserves_full_song_checks_and_token_free_identity(tmp_path, returned_url):
    resolver, worker, calls = _resolver(tmp_path, _source(returned_url, availability="private"))
    reference = SourceReference(SourceKind.SOUNDCLOUD, SOUNDCLOUD, secret_token=TOKEN)
    try:
        result = await resolver.resolve(reference)
        assert result.duration == 143
        assert TOKEN not in repr(result)
        assert calls[0][0][-1] == SHARE
    finally:
        await worker.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("changes", [
    {"webpage_url": SOUNDCLOUD + "-other"},
    {"webpage_url": SOUNDCLOUD + "/s-OtherSecret"},
    {"availability": "premium_only"}, {"has_drm": True}, {"format_id": "hls_preview"},
])
async def test_share_token_does_not_authorize_wrong_track_drm_or_previews(tmp_path, changes):
    resolver, worker, _calls = _resolver(tmp_path, _source(**{"availability": "private", **changes}))
    reference = SourceReference(SourceKind.SOUNDCLOUD, SOUNDCLOUD, secret_token=TOKEN)
    try:
        with pytest.raises(SourceResolutionError) as error:
            await resolver.resolve(reference)
        assert TOKEN not in str(error.value)
    finally:
        await worker.close()


@pytest.mark.asyncio
async def test_provider_echo_is_redacted_before_display_truncation(tmp_path):
    resolver, worker, _calls = _resolver(tmp_path, _metadata(
        availability="private", title="x" * 190 + TOKEN, artist="x" * 90 + TOKEN,
        album=TOKEN, thumbnail="https://cdn.example.test/" + TOKEN,
    ))
    try:
        item = await resolver.fetch_metadata(SourceReference(SourceKind.SOUNDCLOUD, SOUNDCLOUD, secret_token=TOKEN))
        assert "s-Test" not in repr(item.meta)
        assert item.meta["image"] is None
    finally:
        await worker.close()


@pytest.mark.asyncio
async def test_shared_track_admission_never_echoes_token(cog, native_session, tmp_path):
    from TidalPlayerExp.tests.test_youtube_fallback import _context

    resolver, worker, _calls = _resolver(tmp_path, _metadata(availability="private"))
    try:
        item = await resolver.fetch_metadata(SourceReference(SourceKind.SOUNDCLOUD, SOUNDCLOUD, secret_token=TOKEN))
    finally:
        await worker.close()
    cog._initialized = True
    cog.public_audio_resolver.fetch_metadata = AsyncMock(return_value=item)
    ctx = _context()
    await cog.tplay(ctx, query=SHARE)
    cog.public_audio_resolver.fetch_metadata.assert_awaited_once_with(item.reference)
    entry = native_session.entries[0]
    assert entry.primary.secret_token == TOKEN
    assert TOKEN not in repr(entry) + repr(ctx.send.call_args_list)
