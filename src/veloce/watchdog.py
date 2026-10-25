"""Event-loop watchdog — a development-mode detector for blocking calls.

A blocking call inside a coroutine freezes the whole loop, so this logs a
warning carrying the stack of the blocked code. Opt-in via the
`EVENT_LOOP_WATCHDOG` config key; a development aid, not for production.

A stall is only reported while the loop is actually *running*, so an idle loop
(the in-memory test client drives one request at a time) is not a false positive.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import threading
import time
import traceback
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Callable
    from types import FrameType


def _classify_block(first_stack: str, second_stack: str) -> str:
    """Turn two stack samples of the blocked loop thread into a hint.

    Identical samples mean the thread is parked; differing ones, CPU-bound work.
    """
    if first_stack == second_stack:
        return (
            "the loop thread is parked in one call - most likely blocking "
            "I/O or a sleep. Use an async client, or move the call off the "
            "loop with asyncio.to_thread()."
        )
    return (
        "the loop thread is executing - CPU-bound work. Offload it to a "
        "process pool, or accept it if it is rare and short."
    )


class EventLoopWatchdog:
    """Detects event-loop stalls and reports the blocked stack.

    A heartbeat callback re-arms itself on the loop every `interval` seconds
    while a daemon thread measures the gap since the last beat; a gap over
    `stall_threshold` is logged with the loop thread's stack and a hint. Each
    distinct stall is reported once, since the beat counter is frozen for its
    whole duration.
    """

    __slots__ = (
        "_loop",
        "_interval",
        "_stall_threshold",
        "_logger",
        "_attributor",
        "_loop_thread_id",
        "_last_beat",
        "_beat_count",
        "_beat_handle",
        "_thread",
        "_stopped",
    )

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        *,
        interval: float = 0.05,
        stall_threshold: float = 0.1,
        logger: logging.Logger | None = None,
        attributor: Callable[[FrameType], str | None] | None = None,
    ) -> None:
        self._loop = loop
        self._interval = interval
        self._stall_threshold = stall_threshold
        self._logger = logger or logging.getLogger(__name__)
        # Maps the blocked loop frame to the route it is executing. The app
        # builds it from its route table; the watchdog only invokes it.
        self._attributor = attributor
        self._loop_thread_id = 0
        self._last_beat = time.monotonic()
        # The loop thread is its only writer; the watch thread only reads it.
        self._beat_count = 0
        self._beat_handle: asyncio.TimerHandle | None = None
        self._thread: threading.Thread | None = None
        self._stopped = threading.Event()

    def start(self) -> None:
        """Arm the watchdog. Must be called from inside the loop thread."""
        if self._thread is not None and self._thread.is_alive():
            raise RuntimeError("EventLoopWatchdog is already started")
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is not self._loop:
            raise RuntimeError(
                "EventLoopWatchdog.start() must be called from the thread running its event loop"
            )
        self._loop_thread_id = threading.get_ident()
        self._last_beat = time.monotonic()
        self._stopped.clear()
        self._beat_handle = self._loop.call_later(self._interval, self._beat)
        self._thread = threading.Thread(
            target=self._watch, name="veloce-event-loop-watchdog", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        """Disarm the watchdog and join its thread. Safe to call twice."""
        self._stopped.set()
        if self._beat_handle is not None:
            self._beat_handle.cancel()
            self._beat_handle = None
        thread = self._thread
        if thread is not None:
            thread.join(timeout=self._interval * 4)
            self._thread = None

    def _beat(self) -> None:
        """Heartbeat - runs on the event loop and re-arms itself."""
        self._last_beat = time.monotonic()
        self._beat_count += 1
        if not self._stopped.is_set():
            self._beat_handle = self._loop.call_later(self._interval, self._beat)

    def _watch(self) -> None:
        """Watchdog-thread body - polls how stale the heartbeat is."""
        last_reported = -1
        while not self._stopped.wait(self._interval):
            if not self._loop.is_running():
                # Idle, not blocked: keep the beat fresh so the gap is not read
                # as a stall once the loop resumes.
                self._last_beat = time.monotonic()
                continue
            elapsed = time.monotonic() - self._last_beat
            if elapsed <= self._stall_threshold:
                continue
            # The frozen beat counter is reported once; `is_running()` is
            # re-checked to skip a loop that stopped since the measurement.
            count = self._beat_count
            if count != last_reported and self._loop.is_running():
                last_reported = count
                self._report(elapsed)

    def _loop_frame(self) -> FrameType | None:
        # CPython-private, but the only way to read another thread's stack from
        # outside it - acceptable for a dev-only diagnostic.
        return sys._current_frames().get(self._loop_thread_id)

    @staticmethod
    def _format_stack(frame: FrameType | None) -> str:
        if frame is None:
            return "<event-loop stack unavailable>"
        return "".join(traceback.format_stack(frame))

    def _capture_stack(self) -> str:
        return self._format_stack(self._loop_frame())

    def _attribution(self, frame: FrameType | None) -> str:
        """Return " while serving <route>" for the frame, or "" if unattributable.

        Any attributor failure degrades silently; the stack still pinpoints the call.
        """
        if self._attributor is None or frame is None:
            return ""
        try:
            label = self._attributor(frame)
        except Exception:
            return ""
        return f" while serving {label}" if label else ""

    def _report(self, elapsed: float) -> None:
        """Log a stall - runs in the watchdog thread, never on the loop."""
        # Two samples, to classify the block.
        first = self._capture_stack()
        time.sleep(0.005)
        frame = self._loop_frame()
        second = self._format_stack(frame)
        self._logger.warning(
            "event loop blocked for %.0f ms%s - %s\nBlocked loop stack:\n%s",
            elapsed * 1000.0,
            self._attribution(frame),
            _classify_block(first, second),
            second,
        )
