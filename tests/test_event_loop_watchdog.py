"""F12 — development-mode event-loop blocking watchdog.

The watchdog spots a coroutine that blocks the event loop and logs a
warning with the blocked stack. It is opt-in via the `EVENT_LOOP_WATCHDOG`
config key, so an app that does not set it is never affected.
"""

from __future__ import annotations

import asyncio
import logging
import time

import pytest

from veloce import EventLoopWatchdog, Veloce
from veloce.watchdog import _classify_block


async def _wait_for_log(
    caplog, needle: str, *, attempts: int = 150, interval: float = 0.02
) -> bool:
    """Poll `caplog` until a record contains `needle`, or the attempts run out.

    The watchdog reports from a separate thread, so a busy runner may take a few
    extra loop ticks to schedule it; polling keeps the test fast on a healthy
    machine without flaking under load.
    """
    for _ in range(attempts):
        if any(needle in r.getMessage() for r in caplog.records):
            return True
        await asyncio.sleep(interval)
    return False


# ── classification (deterministic, no timing) ─────────────────────────


def test_classify_block_distinguishes_io_from_cpu():
    # Two identical stack samples → the loop thread is parked: blocking I/O.
    io_hint = _classify_block("frame-A\n", "frame-A\n")
    assert "blocking" in io_hint.lower()
    # Differing samples → the loop thread is moving: CPU-bound.
    cpu_hint = _classify_block("frame-A\n", "frame-B\n")
    assert "cpu-bound" in cpu_hint.lower()


# ── stall detection ───────────────────────────────────────────────────


async def test_watchdog_warns_when_the_loop_is_blocked(caplog):
    loop = asyncio.get_running_loop()
    watchdog = EventLoopWatchdog(loop, interval=0.02, stall_threshold=0.05)
    watchdog.start()
    try:
        with caplog.at_level(logging.WARNING, logger="veloce.watchdog"):
            # Blocking the loop is the whole point — that is what the
            # watchdog must catch.
            time.sleep(0.25)  # noqa: ASYNC251
            reported = await _wait_for_log(caplog, "event loop blocked")
    finally:
        watchdog.stop()

    assert reported


async def test_watchdog_report_names_the_blocking_frame(caplog):
    loop = asyncio.get_running_loop()
    watchdog = EventLoopWatchdog(loop, interval=0.02, stall_threshold=0.05)
    watchdog.start()
    try:
        with caplog.at_level(logging.WARNING, logger="veloce.watchdog"):
            time.sleep(0.25)  # noqa: ASYNC251 — deliberately blocks the loop
            await _wait_for_log(caplog, "event loop blocked")
    finally:
        watchdog.stop()

    blocked = [r for r in caplog.records if "event loop blocked" in r.getMessage()]
    assert blocked
    message = blocked[0].getMessage()
    # The warning carries the loop thread's stack, which names this test.
    assert "Blocked loop stack:" in message
    assert "test_event_loop_watchdog" in message


async def test_watchdog_silent_when_the_loop_is_healthy(caplog):
    loop = asyncio.get_running_loop()
    watchdog = EventLoopWatchdog(loop, interval=0.02, stall_threshold=0.1)
    watchdog.start()
    try:
        with caplog.at_level(logging.WARNING, logger="veloce.watchdog"):
            # Cooperative sleeps — the loop keeps ticking, no stall.
            for _ in range(20):
                await asyncio.sleep(0.01)
    finally:
        watchdog.stop()

    assert not any("event loop blocked" in r.getMessage() for r in caplog.records)


# ── lifecycle / misuse ────────────────────────────────────────────────


def test_stop_is_safe_without_start_and_idempotent():
    loop = asyncio.new_event_loop()
    try:
        watchdog = EventLoopWatchdog(loop)
        watchdog.stop()  # never started
        watchdog.stop()  # twice
    finally:
        loop.close()


async def test_double_start_raises():
    loop = asyncio.get_running_loop()
    watchdog = EventLoopWatchdog(loop, interval=0.02)
    watchdog.start()
    try:
        with pytest.raises(RuntimeError, match="already started"):
            watchdog.start()
    finally:
        watchdog.stop()


def test_start_off_the_loop_thread_raises():
    # A plain sync test — there is no running loop here, so start() must
    # refuse rather than capture the wrong thread for stack sampling.
    loop = asyncio.new_event_loop()
    try:
        watchdog = EventLoopWatchdog(loop)
        with pytest.raises(RuntimeError, match="event loop"):
            watchdog.start()
    finally:
        loop.close()


# ── no false positive on an idle loop ─────────────────────────────────


def test_no_false_positive_while_the_loop_is_idle(caplog):
    """The in-memory TestClient drives the loop one request at a time;
    the gaps between requests must not be reported as stalls."""
    app = Veloce(debug=True, openapi_url=None)
    app.config["EVENT_LOOP_WATCHDOG"] = True

    @app.get("/")
    async def index():
        return {"ok": True}

    client = app.test_client()
    try:
        with caplog.at_level(logging.WARNING, logger="veloce.watchdog"):
            client.get("/")
            time.sleep(0.3)  # the loop is stopped (idle) the whole time
            client.get("/")
    finally:
        if app._watchdog is not None:
            app._watchdog.stop()

    assert not any("event loop blocked" in r.getMessage() for r in caplog.records)


# ── app wiring ────────────────────────────────────────────────────────


def test_app_arms_and_disarms_the_watchdog_when_configured():
    app = Veloce(debug=True, openapi_url=None)
    app.config["EVENT_LOOP_WATCHDOG"] = True

    async def drive():
        await app._run_lifecycle("startup")
        armed = app._watchdog is not None
        await app._run_lifecycle("shutdown")
        return armed, app._watchdog

    armed, after_shutdown = asyncio.run(drive())
    assert armed is True
    assert after_shutdown is None  # shutdown disarmed it


def test_app_accepts_watchdog_tuning_options():
    app = Veloce(debug=True, openapi_url=None)
    app.config["EVENT_LOOP_WATCHDOG"] = {"interval": 0.02, "stall_threshold": 0.3}

    async def drive():
        await app._run_lifecycle("startup")
        wd = app._watchdog
        await app._run_lifecycle("shutdown")
        return wd

    wd = asyncio.run(drive())
    assert wd is not None
    assert wd._stall_threshold == 0.3


def test_app_does_not_arm_the_watchdog_by_default():
    app = Veloce(debug=True, openapi_url=None)

    async def drive():
        await app._run_lifecycle("startup")
        return app._watchdog

    assert asyncio.run(drive()) is None
