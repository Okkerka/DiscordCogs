from typing import Any as _Any


__all__ = ("Deepwoken", "setup")


def __getattr__(name: str) -> _Any:
    if name == "Deepwoken":
        from .deepwoken import Deepwoken

        return Deepwoken
    raise AttributeError(name)


async def setup(bot):
    from .deepwoken import Deepwoken

    await bot.add_cog(Deepwoken(bot))
