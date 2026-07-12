"""TidalPlayer package loader."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from redbot.core.bot import Red


async def setup(bot: Red) -> None:
    """Load the active cog without importing provider dependencies prematurely."""
    from .tidalplayer import setup as setup_cog

    await setup_cog(bot)
