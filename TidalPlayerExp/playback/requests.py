"""Task-local ownership for commands that await provider metadata before queueing."""
from __future__ import annotations

import asyncio
from contextvars import ContextVar
from dataclasses import dataclass
from functools import wraps
from typing import Any, Callable

from ..ui.embeds import Messages, error_embed


@dataclass
class PlaybackRequest:
    cog: Any
    context: Any
    generation: int
    batch: asyncio.Event | None = None
    next_up: bool = False


_request: ContextVar[PlaybackRequest | None] = ContextVar("tidalplayer_request", default=None)


def current_request(cog: Any, ctx: Any) -> PlaybackRequest | None:
    """Return only this command's ownership, never another guild's request."""
    request = _request.get()
    return request if request is not None and request.cog is cog and request.context is ctx else None


def request_is_cancelled(cog: Any, ctx: Any) -> bool:
    request = current_request(cog, ctx)
    return bool(request is not None and (
        request.generation != cog._stop_generations[ctx.guild.id]
        or (request.batch is not None and request.batch.is_set())
    ))


def playback_request(*, batch: bool = False) -> Callable:
    """Keep the original stop generation across nested handlers and provider awaits.

    Collection ownership starts before the initial lookup, so stop covers the
    whole import, not just its queue-admission loop.
    """
    def decorate(operation: Callable) -> Callable:
        @wraps(operation)
        async def guarded(cog: Any, ctx: Any, *args: Any, **kwargs: Any) -> Any:
            if getattr(ctx, "guild", None) is None:
                return await operation(cog, ctx, *args, **kwargs)
            request = current_request(cog, ctx)
            token = None
            if request is None:
                request = PlaybackRequest(cog, ctx, cog._stop_generations[ctx.guild.id])
                token = _request.set(request)
            owned_batch = None
            try:
                if request_is_cancelled(cog, ctx):
                    await ctx.send(embed=error_embed("Playback request cancelled. Please try again."))
                    return None
                if batch and request.batch is None:
                    owned_batch = cog._claim_batch(ctx.guild.id)
                    if owned_batch is None:
                        await ctx.send(embed=error_embed(Messages.ERROR_BATCH_IN_PROGRESS))
                        return None
                    request.batch = owned_batch
                return await operation(cog, ctx, *args, **kwargs)
            finally:
                if owned_batch is not None:
                    cog._release_batch(ctx.guild.id, owned_batch)
                    request.batch = None
                if token is not None:
                    _request.reset(token)
        return guarded
    return decorate
