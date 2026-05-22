"""Development-mode event-loop blocking watchdog.

A coroutine that makes a *blocking* call — a synchronous database driver,
`time.sleep`, a CPU-heavy loop — freezes the whole event loop: every
other request stalls behind it. This watchdog notices the stall and logs
a warning carrying the stack of the blocked code, so the offending call
is easy to find.

It is opt-in via the `EVENT_LOOP_WATCHDOG` config key — when that key is
unset nothing is constructed and a production app pays nothing. It is a
development aid and is not meant to run in production.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import threading
import time
import traceback


class EventLoopWatchdog:
    """Detects event-loop stalls and reports the blocked stack.

    A heartbeat callback re-arms itself on the loop every `interval`
    seconds; a separate daemon thread measures how long it has been since
    the last heartbeat. When that gap exceeds `stall_threshold` the loop
    is not getting a chance to run — something is blocking it — and the
    watchdog logs a warning with the loop thread's current stack plus a
    prescriptive hint (blocking-I/O versus CPU-bound).

    Each distinct stall is reported once; the next heartbeat re-arms the
    report so a subsequent stall is logged again.
    """

    __slots__ = (
        "_loop",
        "_interval",
        "_stall_threshold",
        "_logger",
        "_loop_thread_id",
        "_last_beat",
        "_beat_handle",
        "_thread",
        "_stopped",
        "_warned",
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
        self._logger = logger or logging.getLogger("veloce.watchdog")
        self._loop_thread_id = threading.get_ident()
        self._last_beat = time.monotonic()
        self._beat_handle: asyncio.TimerHandle | None = None
        self._thread: threading.Thread | None = None
        self._stopped = threading.Event()
        # True once the current stall has been reported; cleared by the
        # next heartbeat so each distinct stall is reported exactly once.
        self._warned = False

    def start(self) -> None:
        """Arm the watchdog. Must be called from inside the loop thread."""
        self._loop_thread_id = threading.get_ident()
        self._last_beat = time.monotonic()
        self._stopped.clear()
        self._warned = False
        self._beat_handle = self._loop.call_later(self._interval, self._beat)
        self._thread = threading.Thread(
            target=self._watch, name="veloce-event-loop-watchdog", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        """Disarm the watchdog and join its thread."""
        self._stopped.set()
        if self._beat_handle is not None:
            self._beat_handle.cancel()
            self._beat_handle = None
        thread = self._thread
        if thread is not None:
            thread.join(timeout=self._interval * 4)
            self._thread = None

    def _beat(self) -> None:
        """Heartbeat — runs on the event loop and re-arms itself."""
        self._last_beat = time.monotonic()
        self._warned = False
        if not self._stopped.is_set():
            self._beat_handle = self._loop.call_later(self._interval, self._beat)

    def _watch(self) -> None:
        """Watchdog-thread body — polls how stale the heartbeat is."""
        while not self._stopped.wait(self._interval):
            elapsed = time.monotonic() - self._last_beat
            if elapsed > self._stall_threshold and not self._warned:
                self._warned = True
                self._report(elapsed)

    def _capture_stack(self) -> str:
        frame = sys._current_frames().get(self._loop_thread_id)
        if frame is None:
            return "<event-loop stack unavailable>"
        return "".join(traceback.format_stack(frame))

    def _report(self, elapsed: float) -> None:
        """Log a stall — runs in the watchdog thread, never on the loop."""
        # Sample the loop thread's stack twice: a frozen stack means it is
        # parked in one call (blocking I/O / sleep); a moving stack means
        # it is actively executing (CPU-bound).
        first = self._capture_stack()
        time.sleep(0.005)
        second = self._capture_stack()
        if first == second:
            hint = (
                "the loop thread is parked in one call — most likely blocking "
                "I/O or a sleep. Use an async client, or move the call off the "
                "loop with asyncio.to_thread()."
            )
        else:
            hint = (
                "the loop thread is executing — CPU-bound work. Offload it to a "
                "process pool, or accept it if it is rare and short."
            )
        self._logger.warning(
            "event loop blocked for %.0f ms — %s\nBlocked loop stack:\n%s",
            elapsed * 1000.0,
            hint,
            second,
        )
