from TidalPlayer.providers.errors import PlaybackUnavailable, ProviderFailure


def test_playback_unavailable_is_a_sanitized_provider_failure() -> None:
    assert issubclass(PlaybackUnavailable, ProviderFailure)
    assert issubclass(PlaybackUnavailable, RuntimeError)
