from TidalPlayerExp.playback.errors import PlaybackUnavailable, PlaybackError


def test_playback_unavailable_is_a_sanitized_provider_failure() -> None:
    assert issubclass(PlaybackUnavailable, PlaybackError)
    error = PlaybackUnavailable("https://stream.example/?token=secret", provider="secret")
    assert str(error) == "Playback unavailable"
    assert "secret" not in repr(vars(error))
