"""Restore optional voice imports missed before Red exposes Downloader's lib."""

from __future__ import annotations

import importlib
import logging

log = logging.getLogger("red.tidalplayerexp.voice")


def initialize_voice_runtime() -> None:
    """Bind successfully imported crypto libraries without reloading Discord.

    Red imports discord.py before appending Downloader's dependency directory
    to sys.path. Retrying those optional imports at cog load fixes that ordering
    even after a bot restart. Ready libraries and VoiceClient classes are kept;
    failed imports never enable voice or bypass Discord's encryption checks.
    """
    client = importlib.import_module("discord.voice_client")
    state = importlib.import_module("discord.voice_state")
    gateway = importlib.import_module("discord.gateway")
    importlib.invalidate_caches()

    if not client.has_nacl:
        try:
            nacl = importlib.import_module("nacl")
            secret = importlib.import_module("nacl.secret")
            utils = importlib.import_module("nacl.utils")
            if not all(callable(value) for value in (secret.Aead, secret.SecretBox, utils.random)):
                raise ImportError("Incompatible PyNaCl API")
        except Exception as error:  # noqa: BLE001 - optional native imports can fail during initialization
            log.warning("PyNaCl unavailable (%s); update requirements and reload TidalPlayerExp", type(error).__name__)
        else:
            client.__dict__.update(nacl=nacl, has_nacl=True)
            client.VoiceClient.warn_nacl = False

    if not (client.has_dave and state.has_dave):
        try:
            davey = importlib.import_module("davey")
            if (
                not isinstance(davey.DAVE_PROTOCOL_VERSION, int)
                or davey.DAVE_PROTOCOL_VERSION <= 0
                or not callable(davey.DaveSession)
                or not isinstance(davey.CommitWelcome, type)
            ):
                raise ImportError("Incompatible DAVE API")
            # The gateway needs these bindings later, not just at connect time.
            _ = davey.ProposalsOperationType.append, davey.ProposalsOperationType.revoke
        except Exception as error:  # noqa: BLE001 - optional native imports can fail during initialization
            log.warning("DAVE unavailable (%s); update requirements and reload TidalPlayerExp", type(error).__name__)
        else:
            gateway.__dict__["davey"] = davey
            state.__dict__.update(davey=davey, has_dave=True)
            client.__dict__["has_dave"] = True  # discord.py imports this flag by value.
            client.VoiceClient.warn_dave = False
