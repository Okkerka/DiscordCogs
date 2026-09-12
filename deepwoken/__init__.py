import asyncio
from typing import Any as _Any


__all__ = ("Deepwoken", "setup")


def __getattr__(name: str) -> _Any:
    if name == "Deepwoken":
        from .deepwoken import Deepwoken

        return Deepwoken
    raise AttributeError(name)


async def setup(bot):
    from .deepwoken import Deepwoken

    cog = await asyncio.to_thread(Deepwoken, bot)
    await bot.add_cog(cog)
