"""Attachment admission keeps signed Discord CDN URLs private and bounded."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from TidalPlayerExp.playback.errors import SourceResolutionError
from TidalPlayerExp.playback.models import SourceKind, SourceReference
from TidalPlayerExp.providers.attachments import AttachmentResolver

_NOW = 1_700_000_000.0


def _attachment(
    *,
    url: str | None = None,
    filename: str = "mix.mp3",
    content_type: str | None = "audio/mpeg",
    size: int = 1024,
) -> SimpleNamespace:
    url = url or (
        "https://cdn.discordapp.com/attachments/123456789/987654321/mix.mp3"
        f"?ex={int(_NOW + 300):x}&is=abc&hm=signed-secret"
    )
    return SimpleNamespace(url=url, filename=filename, content_type=content_type, size=size)


def test_register_keeps_signed_cdn_url_out_of_reference_metadata_and_repr():
    resolver = AttachmentResolver(clock=lambda: _NOW)
    attachment = _attachment()

    reference, meta = resolver.register(attachment)

    assert reference.kind is SourceKind.ATTACHMENT
    assert len(reference.identifier) == 32
    assert all(character in "0123456789abcdef" for character in reference.identifier)
    assert attachment.url not in repr(reference)
    assert meta == {
        "title": "mix", "artist": "Uploaded file", "album": None, "duration": 0,
        "quality": "File audio", "audio_resolution": None, "track_id": None,
        "image": None, "share_url": None, "source": "Uploaded file",
    }
    assert attachment.url not in repr(next(iter(resolver._entries.values())))


@pytest.mark.parametrize("filename,mime", [
    ("song.mp3", None), ("song.flac", "application/octet-stream"),
    ("song.wav", "Audio/Wav; charset=binary"), ("song.m4a", ""),
    ("clip.mp4", "video/mp4"), ("clip.MP4", "application/octet-stream"),
    ("clip.webm", "video/webm"), ("clip.mov", "video/quicktime"),
    ("clip.mkv", "video/x-matroska"), ("clip.avi", "video/x-msvideo"),
    ("clip.m4v", "video/x-m4v"), ("clip.mpeg", "video/mpeg"),
])
def test_audio_and_video_uploads_accept_optional_or_advisory_mime(filename, mime):
    resolver = AttachmentResolver(clock=lambda: _NOW)
    upload = _attachment(filename=filename, content_type=mime,
        url=f"https://cdn.discordapp.com/attachments/123/456/{filename}")
    reference, meta = resolver.register(upload)
    assert reference.kind is SourceKind.ATTACHMENT
    assert meta["title"] == filename.rsplit(".", 1)[0]


@pytest.mark.parametrize("filename,mime", [("page.html", "video/mp4"), ("program.exe", None), ("list.m3u8", "audio/mpegurl"), ("photo.jpg", "application/octet-stream")])
def test_media_mime_cannot_bypass_extension_allowlist(filename, mime):
    resolver = AttachmentResolver(clock=lambda: _NOW)
    with pytest.raises(ValueError, match="supported"):
        resolver.register(_attachment(filename=filename, content_type=mime))


@pytest.mark.asyncio
async def test_closed_registry_rejects_new_uploads():
    resolver = AttachmentResolver(clock=lambda: _NOW)
    await resolver.close()
    with pytest.raises(ValueError):
        resolver.register(_attachment())


@pytest.mark.asyncio
async def test_upload_resolution_requires_media_only_demuxing():
    resolver = AttachmentResolver(clock=lambda: _NOW)
    reference, _ = resolver.register(_attachment())
    assert (await resolver.resolve(reference)).media_only is True


@pytest.mark.asyncio
async def test_resolve_returns_the_private_signed_url_only_for_a_live_opaque_reference():
    resolver = AttachmentResolver(clock=lambda: _NOW)
    attachment = _attachment()
    reference, _ = resolver.register(attachment)

    resolved = await resolver.resolve(reference)

    assert resolved.url == attachment.url
    assert dict(resolved.headers) == {}
    assert attachment.url not in repr(resolved)


@pytest.mark.parametrize(
    ("attachment", "message"),
    [
        (_attachment(url="https://example.com/attachments/123456789/987654321/mix.mp3"), "Discord CDN"),
        (_attachment(url="https://cdn.discordapp.com:444/attachments/123456789/987654321/mix.mp3"), "Discord CDN"),
        (_attachment(url="https://cdn.discordapp.com/attachments/123456789/987654321/%2fother.mp3"), "valid attachment"),
        (_attachment(filename="notes.txt", url="https://cdn.discordapp.com/attachments/123456789/987654321/notes.txt", content_type="text/plain"), "supported audio"),
        (_attachment(size=0), "between 1 byte and 50 MiB"),
        (_attachment(size=50 * 1024 * 1024 + 1), "between 1 byte and 50 MiB"),
        (_attachment(content_type="text/plain"), "supported audio"),
    ],
)
def test_register_rejects_non_cdn_malformed_unsupported_or_oversized_attachments(attachment, message):
    resolver = AttachmentResolver(clock=lambda: _NOW)

    with pytest.raises(ValueError, match=message):
        resolver.register(attachment)


@pytest.mark.asyncio
async def test_expired_and_unknown_references_have_one_safe_reupload_message():
    now = [_NOW]
    resolver = AttachmentResolver(clock=lambda: now[0])
    reference, _ = resolver.register(_attachment())
    now[0] += 301

    for candidate in (reference, SourceReference(SourceKind.ATTACHMENT, "f" * 32)):
        with pytest.raises(SourceResolutionError, match="Re-upload the file") as raised:
            await resolver.resolve(candidate)
        assert "signed-secret" not in str(raised.value)


def test_capacity_rejects_new_attachment_without_evicting_an_unexpired_reference():
    resolver = AttachmentResolver(clock=lambda: _NOW, max_entries=1)
    first, _ = resolver.register(_attachment())

    with pytest.raises(ValueError, match="(?i)too many uploaded files"):
        resolver.register(_attachment(filename="next.flac", content_type="audio/flac"))

    assert first.identifier in resolver._entries


@pytest.mark.asyncio
async def test_close_discards_all_private_urls():
    resolver = AttachmentResolver(clock=lambda: _NOW)
    resolver.register(_attachment())

    await resolver.close()

    assert not resolver._entries
