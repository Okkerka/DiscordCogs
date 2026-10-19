"""Guards for component callbacks that must survive a stale interaction token."""
from __future__ import annotations

from typing import Any

import discord

__all__ = ["safe_defer"]


async def safe_defer(interaction: discord.Interaction, **kwargs: Any) -> bool:
    """Acknowledge an interaction; return False when its token is already gone.

    Component and modal callbacks have no error reply to fall back on, so a
    10062 (``discord.NotFound``) must abort the callback quietly instead of
    surfacing as an unhandled view exception. The command-side equivalent is
    ``commands.PlaybackCommands._defer``.
    """
    response = getattr(interaction, "response", None)
    if response is None:
        return True
    is_done = getattr(response, "is_done", None)
    if callable(is_done):
        try:
            if is_done():
                return True
        except Exception:
            # A foreign response object must not block acknowledging.
            pass
    defer = getattr(response, "defer", None)
    if defer is None:
        return True
    try:
        await defer(**kwargs)
    except discord.NotFound:
        return False
    return True
