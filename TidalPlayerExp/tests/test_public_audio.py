"""Public audio boundaries, safe queue metadata, and isolated extraction."""

from __future__ import annotations

import asyncio
import importlib
import json
from dataclasses import FrozenInstanceError

import pytest

from TidalPlayerExp.playback import SourceKind, SourceReference, SourceResolutionError
from TidalPlayerExp.providers.urls import MalformedProviderURL, parse_provider_url
from TidalPlayerExp.providers.youtube_resolver import YouTubeResolver

SOUNDCLOUD = "https://soundcloud.com/artist/recording"
BANDCAMP = "https://artist.bandcamp.com/track/recording"


@pytest.mark.parametrize(
    ("url", "kind", "content_type", "canonical"),
    [
        (SOUNDCLOUD, "soundcloud", "track", SOUNDCLOUD),
        (" <https://m.soundcloud.com/artist/recording?utm_source=share> ", "soundcloud", "track", SOUNDCLOUD),
        ("https://www.soundcloud.com:443/artist/sets/my-set/", "soundcloud", "playlist", "https://soundcloud.com/artist/sets/my-set"),
        (BANDCAMP + "?from=discover", "bandcamp", "track", BANDCAMP),
        ("https://artist.bandcamp.com/album/recordings", "bandcamp", "album", "https://artist.bandcamp.com/album/recordings"),
    ],
)
def test_public_audio_urls_canonicalize_only_supported_shapes(url, kind, content_type, canonical):
    parsed = parse_provider_url(url)
    assert parsed is not None
    assert (parsed.provider.value, parsed.content_type, parsed.identifier) == (kind, content_type, canonical)


@pytest.mark.parametrize(
    "url",
    [
        "http://soundcloud.com/artist/recording",
        "https://soundcloud.com:444/artist/recording",
        "https://user:pass@soundcloud.com/artist/recording",
        "https://soundcloud.com.evil.test/artist/recording",
        "https://soundcloud.com/artist",
        "https://soundcloud.com/artist/likes",
        "https://soundcloud.com/artist/recording/s-",
        SOUNDCLOUD + "?secret_token=s-secret",
        SOUNDCLOUD + "?%73ecret_token=s-secret",
        "https://soundcloud.com/artist/../recording",
        "https://soundcloud.com/artist/%2e%2e",
        "https://soundcloud.com/artist%2frecording/other",
        "https://soundcloud.com/artist//recording",
        "https://soundcloud.com/artist/rec\nording",
        "https://soundcloud.com/artist/recording#secret_token=s-secret",
        "https://artist.bandcamp.com:444/track/recording",
        "https://artist.bandcamp.com/track/recording/secret",
        "https://www.bandcamp.com/track/recording",
        "https://nested.artist.bandcamp.com/track/recording",
        "https://artist.bandcamp.com/music",
        "https://artist.bandcamp.com/track/%2frecording",
        "https://artist.bandcamp.com/track/recording?token=secret",
    ],
)
def test_public_audio_urls_reject_private_malformed_and_other_websites(url):
    with pytest.raises(MalformedProviderURL):
        parse_provider_url(url)


@pytest.mark.parametrize("kind,url", [("soundcloud", SOUNDCLOUD), ("bandcamp", BANDCAMP)])
def test_public_audio_queue_reference_requires_canonical_track_url(kind, url):
    reference = SourceReference(SourceKind(kind), url)
    assert reference.identifier == url
    for invalid in (url + "?utm_source=share", url + "/", url.replace("recording", "../recording")):
        with pytest.raises(ValueError):
            SourceReference(reference.kind, invalid)
    with pytest.raises(ValueError):
        SourceReference(reference.kind, BANDCAMP if kind == "soundcloud" else SOUNDCLOUD)
    with pytest.raises(ValueError):
        SourceReference(reference.kind, "https://soundcloud.com/artist/sets/recordings" if kind == "soundcloud" else "https://artist.bandcamp.com/album/recordings")


class _Process:
    def __init__(self, output: bytes):
        self.stdout = asyncio.StreamReader()
        self.stdout.feed_data(output)
        self.stdout.feed_eof()
        self.returncode = 0

    async def wait(self):
        return self.returncode


def _resolver(tmp_path, payload):
    module = importlib.import_module("TidalPlayerExp.providers.public_audio")
    calls = []
    output = payload if isinstance(payload, bytes) else json.dumps(payload).encode()

    async def factory(*args, **kwargs):
        calls.append((args, kwargs))
        return _Process(output)

    package = tmp_path / "yt_dlp"
    package.mkdir(exist_ok=True)
    (package / "__main__.py").write_text("")

    def no_deno():
        raise AssertionError("Public audio must not need a JavaScript runtime")

    worker = YouTubeResolver(process_factory=factory, deno_locator=no_deno, yt_dlp_locator=lambda: str(tmp_path))
    return module.PublicAudioResolver(worker), worker, calls


def _metadata(url=SOUNDCLOUD, **changes):
    return {"webpage_url": url, "title": "  Original\n recording  ", "artist": "Artist", "uploader": "Uploader", "duration": 143.206,
            "thumbnail": "https://images.example.test/cover.jpg", "album": None, "availability": "public", **changes}


def _source(url=SOUNDCLOUD, **changes):
    return {"webpage_url": url, "url": "https://cdn.example.test/audio?token=private-signature", "http_headers": {"User-Agent": "player", "Authorization": "secret", "Cookie": "secret"},
            "format_id": "http_mp3_128", "acodec": "mp3", "vcodec": "none", "asr": 44100, "audio_channels": 2,
            "duration": 143.206, "availability": "public", "has_drm": None, **changes}


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,url", [("soundcloud", SOUNDCLOUD), ("bandcamp", BANDCAMP)])
async def test_public_audio_metadata_detaches_safe_scalars_and_restricts_child_extractors(tmp_path, kind, url):
    resolver, worker, calls = _resolver(tmp_path, _metadata(url))
    reference = SourceReference(SourceKind(kind), url)
    metadata = await resolver.fetch_metadata(reference)
    assert metadata.reference == reference
    assert metadata.meta["title"] == "Original recording"
    assert metadata.meta["artist"] == "Artist"
    assert metadata.meta["duration"] == 143
    assert metadata.meta["share_url"] == url
    assert metadata.meta["source"] == kind
    with pytest.raises(TypeError):
        metadata.meta["title"] = "Changed"
    with pytest.raises(FrozenInstanceError):
        metadata.reference = reference
    args, kwargs = calls[0]
    assert args[-1] == url
    assert "--ignore-config" in args and "--no-plugin-dirs" in args
    assert "--netrc" not in args and "--netrc-cmd" not in args
    assert "--no-js-runtimes" in args and "--js-runtimes" not in args
    assert args[args.index("--use-extractors") + 1] == ("^soundcloud$" if kind == "soundcloud" else "^Bandcamp$")
    assert "--no-playlist" in args and "--flat-playlist" not in args
    assert kwargs["shell"] is False and kwargs["stderr"] == asyncio.subprocess.DEVNULL
    await worker.close()


@pytest.mark.asyncio
async def test_public_audio_metadata_constructor_validates_and_copies_metadata(tmp_path):
    resolver, worker, _ = _resolver(tmp_path, _metadata())
    metadata = await resolver.fetch_metadata(SourceReference(SourceKind("soundcloud"), SOUNDCLOUD))
    raw = dict(metadata.meta)
    detached = type(metadata)(metadata.reference, raw)
    raw["title"] = "changed"
    assert detached.meta["title"] == "Original recording"
    raw["url"] = "https://private.example/stream"
    with pytest.raises(ValueError):
        type(metadata)(metadata.reference, raw)
    await worker.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,url", [("soundcloud", SOUNDCLOUD), ("bandcamp", BANDCAMP)])
async def test_public_audio_resolve_normalizes_duration_and_redacts_sensitive_fields(tmp_path, kind, url):
    resolver, worker, calls = _resolver(tmp_path, _source(url))
    result = await resolver.resolve(SourceReference(SourceKind(kind), url))
    assert result.url == "https://cdn.example.test/audio?token=private-signature"
    assert dict(result.headers) == {"User-Agent": "player"}
    assert result.duration == 143 and result.codec == "mp3" and result.sample_rate == 44100
    assert "private-signature" not in repr(result) and "secret" not in repr(result)
    args, _ = calls[0]
    assert args[args.index("--format") + 1] == "bestaudio[acodec!=none][vcodec=none]"
    await resolver.close()
    assert worker._closed is False
    await worker.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("changes", [
    {"format_id": "hls_mp3_preview"}, {"format_id": "PREVIEW"}, {"vcodec": "h264"},
    {"acodec": "none"}, {"has_drm": True}, {"availability": "private"},
    {"availability": "premium_only"}, {"url": "http://cdn.example.test/audio"},
    {"url": "https://user:pass@cdn.example.test/audio"},
    {"url": "https://cdn.example.test/audio\nsecret"},
    {"http_headers": {"User-Agent": "okay\r\nAuthorization: secret"}},
    {"duration": float("nan")}, {"duration": float("inf")}, {"duration": -1}, {"duration": True},
    {"webpage_url": BANDCAMP}, {"webpage_url": "https://soundcloud.com/artist/other"},
])
async def test_public_audio_rejects_preview_drm_wrong_track_and_invalid_sources(tmp_path, changes):
    resolver, worker, _ = _resolver(tmp_path, _source(**changes))
    with pytest.raises(SourceResolutionError) as raised:
        await resolver.resolve(SourceReference(SourceKind("soundcloud"), SOUNDCLOUD))
    assert "secret" not in str(raised.value) and "private-signature" not in str(raised.value)
    await worker.close()


@pytest.mark.asyncio
async def test_bandcamp_flat_collection_uses_track_urls_skips_bad_entries_and_deduplicates(tmp_path):
    entries = [
        {"url": BANDCAMP, "title": "First", "duration": "NA", "artist": "NA", "uploader": "NA", "thumbnail": "NA"},
        {"webpage_url": BANDCAMP + "?from=album", "title": "Duplicate"},
        {"url": "https://evil.example/track/recording", "title": "Other website"},
        {"url": SOUNDCLOUD, "title": "Other provider"},
        {"url": BANDCAMP + "-2", "title": "Second", "duration": 22.5},
        {"url": BANDCAMP + "-bad", "title": {"nested": "invalid"}},
    ]
    resolver, worker, calls = _resolver(tmp_path, "\n".join(json.dumps(entry) for entry in entries).encode())
    result = await resolver.fetch_collection("https://artist.bandcamp.com/album/recordings", 6)
    assert [item.reference.identifier for item in result] == [BANDCAMP, BANDCAMP + "-2"]
    assert result[0].meta["artist"] == "artist" and result[0].meta["duration"] == 0
    assert result[0].meta["image"] is None and result[1].meta["duration"] == 22
    assert len(calls) == 1
    args, _ = calls[0]
    assert "--flat-playlist" in args and "--lazy-playlist" in args
    assert args[args.index("--playlist-end") + 1] == "6"
    assert args[args.index("--use-extractors") + 1] == "^Bandcamp$,^Bandcamp:album$"
    assert "%(webpage_url,url)j" in args[args.index("--print") + 1]
    await worker.close()


@pytest.mark.asyncio
async def test_soundcloud_flat_collection_retains_provider_and_original_order(tmp_path):
    resolver, worker, calls = _resolver(tmp_path, json.dumps(_metadata()).encode())
    result = await resolver.fetch_collection("https://soundcloud.com/artist/sets/recordings")
    assert len(result) == 1 and result[0].reference.kind is SourceKind("soundcloud")
    args, _ = calls[0]
    assert args[args.index("--use-extractors") + 1] == "^soundcloud$,^soundcloud:set$"
    assert args[args.index("--playlist-end") + 1] == "100"
    await worker.close()


@pytest.mark.asyncio
async def test_soundcloud_flat_entries_can_have_only_a_public_url_and_album(tmp_path):
    # yt-dlp SoundcloudPlaylistBaseIE emits URL-transparent entries without titles.
    payload = {"webpage_url": SOUNDCLOUD + "-mix", "title": "NA", "artist": "NA", "album": "A set"}
    resolver, worker, calls = _resolver(tmp_path, json.dumps(payload).encode())
    try:
        result = await resolver.fetch_collection("https://soundcloud.com/artist/sets/recordings")
        assert len(result) == 1
        assert result[0].meta["title"] == "recording mix"
        assert result[0].meta["artist"] == "artist"
        assert result[0].meta["album"] == "A set"
        assert len(calls) == 1
    finally:
        await worker.close()


@pytest.mark.asyncio
async def test_real_extractor_projection_serializes_missing_values_as_json_null(tmp_path):
    """Use yt-dlp's real formatting against local metadata, without media I/O."""
    from TidalPlayerExp.providers.public_audio import _METADATA_TEMPLATE
    document = tmp_path / "metadata.json"
    document.write_text(json.dumps({
        "id": "123", "title": "A recording", "extractor": "Bandcamp",
        "webpage_url": BANDCAMP, "url": "https://cdn.example.test/audio.mp3",
        "ext": "mp3", "format_id": "mp3", "acodec": "mp3", "vcodec": "none",
    }), encoding="utf-8")
    worker = YouTubeResolver()
    args = worker._common_args(worker._deno_locator(), worker._yt_dlp_locator())
    args += ["--load-info-json", str(document), "--print", _METADATA_TEMPLATE]
    try:
        output = await worker._run_child(args, deadline=15, ceiling=16 * 1024)
        result = json.loads(output)
        assert result["title"] == "A recording"
        assert result["thumbnail"] is None
        assert result["duration"] is None
        assert result["artist"] is None
    finally:
        await worker.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [0, -1, 101, True, 1.5])
async def test_public_audio_collection_rejects_invalid_limits_before_spawning(tmp_path, limit):
    resolver, worker, calls = _resolver(tmp_path, b"")
    with pytest.raises(SourceResolutionError):
        await resolver.fetch_collection("https://soundcloud.com/artist/sets/recordings", limit)
    assert not calls
    await worker.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("payload,limit", [(b"x" * (400 * 1024 + 1), 100), (b"{}\n{}", 1)], ids=["byte-cap", "entry-cap"])
async def test_public_audio_collection_rejects_oversized_output(tmp_path, payload, limit):
    resolver, worker, _ = _resolver(tmp_path, payload)
    with pytest.raises(SourceResolutionError):
        await resolver.fetch_collection("https://soundcloud.com/artist/sets/recordings", limit)
    await worker.close()


@pytest.mark.asyncio
async def test_public_audio_invalid_references_do_not_spawn(tmp_path):
    resolver, worker, calls = _resolver(tmp_path, b"")
    for method in (resolver.resolve, resolver.fetch_metadata):
        with pytest.raises(SourceResolutionError):
            await method(SourceReference(SourceKind.YOUTUBE, "dQw4w9WgXcQ"))
    with pytest.raises(SourceResolutionError):
        await resolver.fetch_collection(SOUNDCLOUD)
    assert not calls
    await worker.close()
