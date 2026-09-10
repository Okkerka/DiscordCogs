"""Gain and seek options reach FFmpeg without changing the safe 100% fast path."""
import pytest

from TidalPlayerExp.playback.models import ResolvedSource
from TidalPlayerExp.tests.test_ffmpeg_source import _factory, _Process, ffmpeg_module


@pytest.mark.asyncio
@pytest.mark.parametrize("volume,copied", [(0, False), (100, True), (150, False)])
async def test_gain_transcodes_only_when_needed(ffmpeg_module, tmp_path, volume, copied):
    calls = []
    def spawn(argv, **kwargs):
        calls.append(argv)
        return _Process()
    factory, *_ = _factory(ffmpeg_module, tmp_path, spawn)
    audio = await factory.create(ResolvedSource("https://example.com/audio", {},
        codec="opus", sample_rate=48000, channels=2, volume=volume))
    try:
        argv = calls[0]
        assert argv[argv.index("-c:a") + 1] == ("copy" if copied else "libopus")
        if volume != 100:
            assert argv[argv.index("-af") + 1].startswith(f"volume={volume / 100:.2f}")
            assert ("alimiter=limit=1:level=false:latency=true" in argv[argv.index("-af") + 1]) == (volume > 100)
        else:
            assert "-af" not in argv
    finally:
        audio.cleanup()
        await factory.close()


@pytest.mark.asyncio
async def test_seek_is_bounded_numeric_input_before_media(ffmpeg_module, tmp_path):
    calls = []
    def spawn(argv, **kwargs):
        calls.append(argv)
        return _Process()
    factory, *_ = _factory(ffmpeg_module, tmp_path, spawn)
    audio = await factory.create(ResolvedSource("https://example.com/audio", {}, start_time=12.5))
    try:
        argv = calls[0]
        assert float(argv[argv.index("-ss") + 1]) == 12.5
        assert argv.index("-ss") < argv.index("-i")
        assert "-nostdin" in argv
    finally:
        audio.cleanup()
        await factory.close()
