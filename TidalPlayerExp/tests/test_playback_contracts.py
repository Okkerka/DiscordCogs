"""Behavioral tests for the backend-neutral playback contracts."""

from __future__ import annotations

import traceback
from collections.abc import Mapping
from typing import Any

import pytest

from TidalPlayerExp.domain.models import TrackMeta
from TidalPlayerExp.playback import (
    PlaybackBackend,
    PlaybackEntry,
    PlaybackError,
    PlaybackEventSink,
    PlaybackSession,
    PlaybackSnapshot,
    PlaybackStartError,
    PlaybackUnavailable,
    ResolvedSource,
    SourceKind,
    SourceReference,
    SourceResolutionError,
    SourceResolver,
)


def _meta() -> TrackMeta:
    return {
        "title": "Track",
        "artist": "Artist",
        "album": "Album",
        "duration": 180,
        "quality": "lossless",
        "image": None,
        "share_url": None,
        "audio_resolution": None,
        "track_id": 123,
    }


def test_source_kind_contains_only_stable_provider_kinds() -> None:
    assert [(kind.name, kind.value) for kind in SourceKind] == [
        ("TIDAL", "tidal"),
        ("TIDAL_VIDEO", "tidal_video"),
        ("YOUTUBE", "youtube"),
        ("SOUNDCLOUD", "soundcloud"),
        ("BANDCAMP", "bandcamp"),
        ("ATTACHMENT", "attachment"),
    ]


@pytest.mark.parametrize("identifier", ["", "0", "-1", "1.2", "track"])
@pytest.mark.parametrize("kind_value", ["tidal", "tidal_video"])
def test_tidal_source_reference_rejects_non_positive_decimal_identifier(
    identifier: str, kind_value: str
) -> None:
    kind = SourceKind(kind_value)
    with pytest.raises(ValueError) as caught:
        SourceReference(kind, identifier)

    if identifier:
        assert identifier not in str(caught.value)


@pytest.mark.parametrize("kind_value", ["tidal", "tidal_video"])
def test_tidal_source_reference_accepts_positive_decimal_identifier(
    kind_value: str,
) -> None:
    kind = SourceKind(kind_value)
    reference = SourceReference(kind, "00123")

    assert reference.identifier == "00123"


@pytest.mark.parametrize("identifier", ["short", "dQw4w9WgXcQ!", "dQw4w9WgXcQ0"])
def test_youtube_source_reference_requires_exact_video_id(identifier: str) -> None:
    with pytest.raises(ValueError) as caught:
        SourceReference(SourceKind.YOUTUBE, identifier)

    assert identifier not in str(caught.value)


def test_source_reference_rejects_unknown_kind_without_echoing_identifier() -> None:
    identifier = "private-id"

    with pytest.raises(ValueError) as caught:
        SourceReference("spotify", identifier)  # type: ignore[arg-type]

    assert identifier not in str(caught.value)


def test_resolved_source_redacts_media_values_and_freezes_headers() -> None:
    input_headers = {"Authorization": "Bearer secret-token"}
    source = ResolvedSource(
        "https://audio.example/secret-stream",
        input_headers,
        codec="aac",
        sample_rate=48_000,
        channels=2,
        duration=180,
    )
    input_headers["X-Added"] = "later"

    assert isinstance(source.headers, Mapping)
    assert dict(source.headers) == {"Authorization": "Bearer secret-token"}
    with pytest.raises(TypeError):
        source.headers["X-New"] = "value"  # type: ignore[index]
    assert repr(source) == "ResolvedSource(<redacted>)"
    assert "audio.example" not in repr(source)
    assert "secret-token" not in repr(source)


class _HostileMapping(Mapping[str, str]):
    def __iter__(self):
        raise RuntimeError(
            "signed-url=https://audio.example/private?token=header-secret"
        )

    def __len__(self) -> int:
        return 1

    def __getitem__(self, key: str) -> str:
        raise RuntimeError(key)


def _error_surface(error: BaseException) -> str:
    formatted = "".join(
        traceback.format_exception(type(error), error, error.__traceback__)
    )
    return "\n".join(
        (
            str(error),
            repr(error),
            formatted,
            repr(error.__cause__),
            repr(error.__context__),
        )
    )


def test_resolved_source_rejects_hostile_or_non_mapping_headers_without_leaking_context() -> (
    None
):
    secret = "signed-url=https://audio.example/private?token=header-secret"
    for headers in (_HostileMapping(), [("Authorization", secret)]):
        with pytest.raises(ValueError) as caught:
            ResolvedSource("https://audio.example/stream", headers)  # type: ignore[arg-type]

        assert secret not in _error_surface(caught.value)
        assert caught.value.__cause__ is None
        assert caught.value.__context__ is None


@pytest.mark.parametrize(
    ("url", "field", "value"),
    [
        ("http://audio.example/secret", "url", None),
        ("https://audio.example:abc/secret", "url", None),
        ("https://audio.example:65536/secret", "url", None),
        ("https://audio.example/secret", "sample_rate", 0),
        ("https://audio.example/secret", "channels", -1),
        ("https://audio.example/secret", "duration", 0),
    ],
)
def test_resolved_source_rejects_invalid_media_values_without_echoing_them(
    url: str, field: str, value: int | None
) -> None:
    kwargs: dict[str, Any] = {field: value}
    with pytest.raises(ValueError) as caught:
        if field == "url":
            ResolvedSource(kwargs["url"], {})
        else:
            ResolvedSource(url, {}, **kwargs)

    assert url not in str(caught.value)
    if value is not None:
        assert str(value) not in str(caught.value)


def test_playback_entry_copies_metadata_and_rejects_duplicate_fallback() -> None:
    primary = SourceReference(SourceKind.TIDAL, "1")
    fallback = SourceReference(SourceKind.YOUTUBE, "dQw4w9WgXcQ")
    metadata = _meta()
    entry = PlaybackEntry("entry-1", primary, fallback, metadata, requester_id=42)
    metadata["title"] = "Changed by caller"

    assert entry.meta["title"] == "Track"
    assert entry.fallback == fallback

    with pytest.raises(ValueError):
        PlaybackEntry("entry-2", primary, primary, _meta(), requester_id=None)


def test_playback_entry_exposes_read_only_metadata() -> None:
    entry = PlaybackEntry(
        "entry-1",
        SourceReference(SourceKind.TIDAL, "1"),
        None,
        _meta(),
        requester_id=None,
    )

    with pytest.raises(TypeError):
        entry.meta["title"] = "caller mutation"  # type: ignore[index]


def test_fallback_metadata_is_validated_and_detached() -> None:
    metadata = _meta()
    entry = PlaybackEntry(
        "fallback",
        SourceReference(SourceKind.TIDAL, "1"),
        SourceReference(SourceKind.YOUTUBE, "dQw4w9WgXcQ"),
        _meta(),
        None,
        fallback_meta=metadata,
    )
    metadata["title"] = "changed"
    assert entry.fallback_meta is not None
    assert entry.fallback_meta["title"] == "Track"
    with pytest.raises(TypeError):
        entry.fallback_meta["title"] = "changed"
    with pytest.raises(ValueError):
        PlaybackEntry(
            "invalid",
            entry.primary,
            entry.fallback,
            _meta(),
            None,
            fallback_meta={"title": []},
        )  # type: ignore[typeddict-item]


@pytest.mark.parametrize(
    "metadata",
    [
        list(_meta().items()),
        {**_meta(), "title": ["nested mutable value"]},
        {**_meta(), "title": 123},
        {key: value for key, value in _meta().items() if key != "artist"},
    ],
)
def test_playback_entry_rejects_non_mapping_or_invalid_metadata(
    metadata: object,
) -> None:
    with pytest.raises(ValueError) as caught:
        PlaybackEntry(
            "entry-1",
            SourceReference(SourceKind.TIDAL, "1"),
            None,
            metadata,  # type: ignore[arg-type]
            requester_id=None,
        )

    assert "nested mutable value" not in _error_surface(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


@pytest.mark.parametrize("entry_id", ["", "x" * 65])
def test_playback_entry_rejects_empty_or_overlong_entry_id(entry_id: str) -> None:
    with pytest.raises(ValueError) as caught:
        PlaybackEntry(
            entry_id,
            SourceReference(SourceKind.TIDAL, "1"),
            None,
            _meta(),
            requester_id=None,
        )

    if entry_id:
        assert entry_id not in str(caught.value)


def test_playback_snapshot_copies_queue_to_tuple() -> None:
    entry = PlaybackEntry(
        "entry-1",
        SourceReference(SourceKind.TIDAL, "1"),
        None,
        _meta(),
        requester_id=None,
    )
    queued = [entry]
    snapshot = PlaybackSnapshot(
        current=entry, queued=queued, paused=False, channel_id=99
    )  # type: ignore[arg-type]
    queued.append(entry)

    assert snapshot.queued == (entry,)
    assert isinstance(snapshot.queued, tuple)


@pytest.mark.parametrize(
    "error_type",
    [PlaybackError, PlaybackUnavailable, SourceResolutionError, PlaybackStartError],
)
def test_playback_errors_do_not_retain_or_echo_unsafe_message(
    error_type: type[PlaybackError],
) -> None:
    unsafe = "https://audio.example/secret?token=top-secret"
    error = error_type(unsafe)

    assert unsafe not in error.args
    assert unsafe not in str(error)
    assert unsafe not in repr(error)
    assert error.__cause__ is None


class _ResolverFake:
    async def resolve(self, reference: SourceReference) -> ResolvedSource:
        return ResolvedSource("https://audio.example/stream", {})

    async def close(self) -> None:
        return None


class _SinkFake:
    async def track_started(self, guild_id: int, entry: PlaybackEntry) -> None:
        return None

    async def track_failed(
        self, guild_id: int, entry: PlaybackEntry, reason: str
    ) -> None:
        return None

    async def queue_ended(self, guild_id: int, previous: PlaybackEntry | None) -> None:
        return None


class _SessionFake:
    guild_id = 1

    def snapshot(self) -> PlaybackSnapshot:
        return PlaybackSnapshot(None, (), False, None)

    async def enqueue(
        self, entry: PlaybackEntry, *, start_if_idle: bool = True, next_up: bool = False,
    ) -> bool:
        return start_if_idle or next_up

    async def remove(self, index: int) -> PlaybackEntry | None:
        return None

    async def clear_queue(self) -> int:
        return 0

    async def move(self, index: int, destination: int) -> bool:
        return False

    async def shuffle_queue(self) -> bool:
        return False

    async def set_repeat(self, mode: str) -> None:
        return None

    async def set_volume(self, percent: int) -> None:
        return None

    async def seek(self, seconds: float) -> bool:
        return False

    async def resume_queue(self) -> bool:
        return False

    async def skip(self) -> bool:
        return True

    async def set_paused(self, paused: bool) -> bool:
        return paused

    async def stop(self, *, clear_queue: bool = True) -> None:
        return None

    async def close(self) -> None:
        return None


class _BackendFake:
    async def get(self, guild_id: int) -> PlaybackSession | None:
        return None

    async def connect(self, guild: Any, channel: Any) -> PlaybackSession:
        return _SessionFake()

    async def close_guild(self, guild_id: int) -> None:
        return None

    async def close(self) -> None:
        return None


def test_protocol_compatible_fakes_can_be_checked_at_runtime() -> None:
    assert isinstance(_ResolverFake(), SourceResolver)
    assert isinstance(_SinkFake(), PlaybackEventSink)
    assert isinstance(_SessionFake(), PlaybackSession)
    assert isinstance(_BackendFake(), PlaybackBackend)


def test_playback_package_exports_only_contract_names() -> None:
    from TidalPlayerExp import playback

    expected = {
        "PlaybackBackend",
        "PlaybackEntry",
        "PlaybackError",
        "PlaybackEventSink",
        "PlaybackSession",
        "PlaybackSnapshot",
        "PlaybackStartError",
        "PlaybackUnavailable",
        "ResolvedSource",
        "SourceKind",
        "SourceReference",
        "SourceResolutionError",
        "SourceResolver",
    }
    assert set(playback.__all__) == expected
