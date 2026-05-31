"""Event-loop watchdog - a development-mode detector for blocking calls.

A coroutine that makes a *blocking* call - a synchronous database driver,
`time.sleep`, a CPU-heavy loop - freezes the whole event loop: every
other request stalls behind it. This watchdog notices the stall and logs
a warning carrying the stack of the blocked code, so the offending call
is easy to find.

It is opt-in via the `EVENT_LOOP_WATCHDOG` config key - when that key is
unset nothing is constructed and a production app pays nothing. It is a
development aid and is not meant to run in production.

A stall is only reported while the loop is actually *running*: an idle
loop (stopped between calls, or parked waiting for I/O) is not a stall,
so the in-memory test client - which drives the loop one request at a
time - does not produce false positives.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import threading
import time
import traceback


def _classify_block(first_stack: str, second_stack: str) -> str:
    """Turn two stack samples of the blocked loop thread into a hint.

    Two identical samples mean the loop thread is parked in one place - a
    blocking call or a sleep. Differing samples mean it is moving through
    code - CPU-bound work.
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

    A heartbeat callback re-arms itself on the loop every `interval`
    seconds; a separate daemon thread measures how long it has been since
    the last heartbeat *while the loop is running*. When that gap exceeds
    `stall_threshold` something is blocking the loop, and the watchdog
    logs a warning with the loop thread's current stack plus a
    prescriptive hint (blocking-I/O versus CPU-bound).

    Each distinct stall is reported once - the heartbeat counter is frozen
    for the stall's whole duration, and the watch thread reports a given
    counter value at most once.
    """

    __slots__ = (
        "_loop",
        "_interval",
        "_stall_threshold",
        "_logger",
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
    ) -> None:
        self._loop = loop
        self._interval = interval
        self._stall_threshold = stall_threshold
        self._logger = logger or logging.getLogger(__name__)
        self._loop_thread_id = 0
        self._last_beat = time.monotonic()
        # Bumped by every heartbeat - the loop thread is its only writer.
        # The watch thread reads it to report each distinct stall once.
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
                # The loop is idle (stopped, or between calls), not
                # blocked - keep the heartbeat fresh so the idle gap is
                # not mistaken for a stall once the loop resumes.
                self._last_beat = time.monotonic()
                continue
            elapsed = time.monotonic() - self._last_beat
            if elapsed <= self._stall_threshold:
                continue
            # A genuine stall while the loop is running. The beat counter
            # is frozen for the stall's duration, so report each value
            # once. Re-check `is_running()` to skip a loop that stopped
            # in the gap since the elapsed measurement.
            count = self._beat_count
            if count != last_reported and self._loop.is_running():
                last_reported = count
                self._report(elapsed)

    def _capture_stack(self) -> str:
        # sys._current_frames() is the only way to read another thread's
        # stack from outside it. It is a CPython-private API (leading
        # underscore, no cross-implementation guarantee), but there is no
        # public equivalent; this is acceptable for a dev-only diagnostic.
        frame = sys._current_frames().get(self._loop_thread_id)
        if frame is None:
            return "<event-loop stack unavailable>"
        return "".join(traceback.format_stack(frame))

    def _report(self, elapsed: float) -> None:
        """Log a stall - runs in the watchdog thread, never on the loop."""
        # Sample the loop thread's stack twice to classify the block.
        first = self._capture_stack()
        time.sleep(0.005)
        second = self._capture_stack()
        self._logger.warning(
            "event loop blocked for %.0f ms - %s\nBlocked loop stack:\n%s",
            elapsed * 1000.0,
            _classify_block(first, second),
            second,
        )
