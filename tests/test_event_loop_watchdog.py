"""F12 — development-mode event-loop blocking watchdog.

The watchdog spots a coroutine that blocks the event loop and logs a
warning with the blocked stack. It is opt-in via the `EVENT_LOOP_WATCHDOG`
config key, so an app that does not set it is never affected.
"""

from __future__ import annotations

import asyncio
import logging
import time

from veloce import EventLoopWatchdog, Veloce

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
            await asyncio.sleep(0.1)  # let the watchdog thread report
    finally:
        watchdog.stop()

    assert any("event loop blocked" in r.getMessage() for r in caplog.records)


async def test_watchdog_report_includes_the_blocked_stack(caplog):
    loop = asyncio.get_running_loop()
    watchdog = EventLoopWatchdog(loop, interval=0.02, stall_threshold=0.05)
    watchdog.start()
    try:
        with caplog.at_level(logging.WARNING, logger="veloce.watchdog"):
            time.sleep(0.25)  # noqa: ASYNC251 — deliberately blocks the loop
            await asyncio.sleep(0.1)
    finally:
        watchdog.stop()

    blocked = [r for r in caplog.records if "event loop blocked" in r.getMessage()]
    assert blocked
    # The warning carries a stack and a prescriptive hint.
    assert "Blocked loop stack:" in blocked[0].getMessage()


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


def test_stop_is_safe_without_start_and_idempotent():
    loop = asyncio.new_event_loop()
    try:
        watchdog = EventLoopWatchdog(loop)
        watchdog.stop()  # never started
        watchdog.stop()  # twice
    finally:
        loop.close()


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


def test_app_does_not_arm_the_watchdog_by_default():
    app = Veloce(debug=True, openapi_url=None)

    async def drive():
        await app._run_lifecycle("startup")
        return app._watchdog

    assert asyncio.run(drive()) is None
