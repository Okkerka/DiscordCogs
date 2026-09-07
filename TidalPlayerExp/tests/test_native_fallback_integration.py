"""Exercise YouTube admission through the real session and cog event sink."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from TidalPlayerExp.playback.models import SourceKind
from TidalPlayerExp.playback.session import NativePlaybackSession
from TidalPlayerExp.providers.youtube_resolver import YouTubeVideoMetadata
from TidalPlayerExp.tests.test_native_session import Factory, Resolver, Voice


@pytest.mark.asyncio
@pytest.mark.parametrize("both_sources_fail", [False, True])
async def test_matched_youtube_uses_effective_source_or_one_error(cog, both_sources_fail, monkeypatch):
    voice, resolver, factory = Voice(), Resolver(), Factory()
    voice.guild.id = 1
    resolver.fail = {SourceKind.TIDAL}
    if both_sources_fail:
        resolver.fail.add(SourceKind.YOUTUBE)
    resolver.gate = asyncio.Event()
    completed = asyncio.Event()
    channel = SimpleNamespace(id=44, send=AsyncMock(side_effect=lambda **kwargs: completed.set()))
    ctx = SimpleNamespace(
        guild=voice.guild, channel=channel, author=SimpleNamespace(id=5), send=AsyncMock(),
    )
    session = NativePlaybackSession(1, voice, resolver, factory, cog)
    cog.backend = SimpleNamespace(get=AsyncMock(return_value=session))
    cog._prepare_playback_session = AsyncMock(return_value=session)
    cog._resend_controller_for_track_start = AsyncMock(side_effect=lambda **kwargs: completed.set())
    cog._schedule_controller_recommendations = Mock()
    video = YouTubeVideoMetadata("abcdefghijk", "Artist - Song (Official Audio)", "Artist", 180, None)
    cog.youtube_resolver.fetch_metadata = AsyncMock(return_value=video)
    cog.yt = None
    monkeypatch.setattr(type(cog.tidal), "is_logged_in", AsyncMock(return_value=True))
    monkeypatch.setattr(type(cog.tidal), "search", AsyncMock(return_value=[SimpleNamespace(
        id=123, name="Song", full_name="Song", artist=SimpleNamespace(name="Artist"),
        album=None, duration=180, audio_quality="HI_RES_LOSSLESS",
    )]))
    try:
        await cog._handle_youtube_video(ctx, video.video_id)
        await asyncio.wait_for(resolver.entered.wait(), 2)
        admitted = session.snapshot().current
        assert admitted.primary.kind is SourceKind.TIDAL
        assert admitted.fallback.identifier == "abcdefghijk"
        assert admitted.fallback_meta["title"] == "Artist - Song (Official Audio)"
        assert not cog._current_meta  # admission is not an actual playback announcement
        assert not voice.played
        resolver.gate.set()
        await asyncio.wait_for(completed.wait(), 2)
        assert [reference.kind for reference in resolver.calls] == [
            SourceKind.TIDAL, SourceKind.TIDAL, SourceKind.YOUTUBE,
        ]
        if both_sources_fail:
            assert len(channel.send.call_args_list) == 1
            assert not ctx.send.call_args_list
            assert not voice.played
            assert not cog._current_meta
        else:
            effective = session.snapshot().current
            assert effective.entry_id == admitted.entry_id
            assert effective.primary.kind is SourceKind.YOUTUBE
            assert len(voice.played) == 1
            assert cog._current_meta[1]["title"] == "Artist - Song (Official Audio)"
            assert cog._current_meta[1]["source"].casefold() == "youtube"
            assert cog._current_meta[1]["track_id"] is None
            assert cog._current_meta[1]["audio_resolution"] is None
            assert cog._current_meta[1]["share_url"] == "https://www.youtube.com/watch?v=abcdefghijk"
            assert not channel.send.call_args_list
    finally:
        await session.close()
