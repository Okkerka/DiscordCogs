"""Small Red Audio/Lavalink seam, isolated from command and provider logic."""

from __future__ import annotations

from typing import Any

from .errors import PlaybackUnavailable


class RedAudioGateway:
    """Adapt the currently installed Red Audio bridge at one explicit boundary."""

    def __init__(self, bridge: Any | None) -> None:
        self._bridge = bridge

    @property
    def available(self) -> bool:
        return self._bridge is not None

    async def get_player(self, guild_id: int, voice_channel: Any | None = None) -> Any:
        if self._bridge is None:
            raise PlaybackUnavailable("Audio bridge is unavailable")
        try:
            return self._bridge.get_player(guild_id)
        except Exception:
            if voice_channel is None:
                raise PlaybackUnavailable("No active audio player") from None
        try:
            await self._bridge.connect(voice_channel)
            return self._bridge.get_player(guild_id)
        except Exception as error:
            raise PlaybackUnavailable("Unable to connect an audio player") from error
