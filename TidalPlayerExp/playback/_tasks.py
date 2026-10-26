"""Shared helpers for retaining detached playback tasks."""

from __future__ import annotations

import asyncio


def retain_task(tasks: set[asyncio.Task[None]], task: asyncio.Task[None]) -> None:
    """Track a task that outlived its waiter and consume any late failure."""

    def task_finished(finished: asyncio.Task[None]) -> None:
        tasks.discard(finished)
        if not finished.cancelled():
            finished.exception()

    tasks.add(task)
    task.add_done_callback(task_finished)
