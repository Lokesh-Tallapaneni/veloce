"""Background tasks - run work after response is sent."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from veloce._internal import _is_async_callable, offload

_logger = logging.getLogger(__name__)


class BackgroundTask:
    """A single background task."""

    __slots__ = ("func", "args", "kwargs")

    def __init__(self, func: Callable, *args: Any, **kwargs: Any) -> None:
        self.func = func
        self.args = args
        self.kwargs = kwargs

    async def run(self) -> None:
        """Execute the background task."""
        if _is_async_callable(self.func):
            await self.func(*self.args, **self.kwargs)
        else:
            await offload(self.func, *self.args, **self.kwargs)


class BackgroundTasks:
    """Collection of background tasks to run after response."""

    __slots__ = ("_tasks",)

    def __init__(self) -> None:
        self._tasks: list[BackgroundTask] = []

    def add_task(self, func: Callable, *args: Any, **kwargs: Any) -> None:
        """Append a task to the queue."""
        self._tasks.append(BackgroundTask(func, *args, **kwargs))

    async def run_all(self) -> None:
        """Execute all queued tasks sequentially."""
        for task in self._tasks:
            try:
                await task.run()
            except Exception:
                _logger.exception(
                    "Background task %s raised an exception",
                    getattr(task.func, "__name__", repr(task.func)),
                )
