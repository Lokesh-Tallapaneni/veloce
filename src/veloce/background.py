"""Background tasks — run work after response is sent."""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Callable
from typing import Any

logger = logging.getLogger("veloce.background")


class BackgroundTask:
    """A single background task."""

    __slots__ = ("func", "args", "kwargs")

    def __init__(self, func: Callable, *args: Any, **kwargs: Any) -> None:
        self.func = func
        self.args = args
        self.kwargs = kwargs

    async def run(self) -> None:
        if inspect.iscoroutinefunction(self.func):
            await self.func(*self.args, **self.kwargs)
        else:
            # Offload sync tasks to executor to avoid blocking the event loop
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, lambda: self.func(*self.args, **self.kwargs))


class BackgroundTasks:
    """Collection of background tasks to run after response."""

    def __init__(self) -> None:
        self._tasks: list[BackgroundTask] = []

    def add_task(self, func: Callable, *args: Any, **kwargs: Any) -> None:
        self._tasks.append(BackgroundTask(func, *args, **kwargs))

    async def run_all(self) -> None:
        for task in self._tasks:
            try:
                await task.run()
            except Exception:
                logger.exception("Background task %s raised an exception", task.func.__name__)
