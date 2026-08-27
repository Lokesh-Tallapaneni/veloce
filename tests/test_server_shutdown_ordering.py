"""Shutdown drains live connections before it waits on the server.

Since Python 3.12 `asyncio.Server.wait_closed()` genuinely waits for every
accepted connection to finish. Both native serving paths used to close the
server and await that BEFORE flipping connections into draining, so a single
idle keep-alive client held shutdown for the whole `KEEP_ALIVE_TIMEOUT` - 75
seconds by default. Under gunicorn that outlasts `graceful_timeout` (30s), so
the master SIGKILLs the worker mid-wait: `_shutdown` never runs, the app's
shutdown hooks never fire, and in-flight work is cut.

The ordering is the whole fix, so these tests assert the ordering itself rather
than a wall-clock duration that would be flaky on a loaded machine.
"""

from __future__ import annotations

import asyncio
import inspect

from veloce import Veloce
from veloce.app.serving import ServingMixin
from veloce.serving.protocol import HttpProtocol
from veloce.workers import VeloceWorker


class _FakeTransport(asyncio.Transport):
    def __init__(self) -> None:
        super().__init__()
        self.writes: list[bytes] = []
        self.closed = False

    def write(self, data: bytes) -> None:
        self.writes.append(data)

    def close(self) -> None:
        self.closed = True

    def is_closing(self) -> bool:
        return self.closed

    def get_extra_info(self, name: str, default: object = None) -> object:
        return default

    def pause_reading(self) -> None:
        return None

    def resume_reading(self) -> None:
        return None


def _code_only(source: str) -> str:
    """Drop comment lines so a comment naming a call is not read as the call."""
    return "\n".join(line for line in source.splitlines() if not line.lstrip().startswith("#"))


def _reset_drain_latch() -> None:
    """Clear the process-wide latch so tests do not leak state into each other."""
    reset = getattr(HttpProtocol, "reset_graceful_drain", None)
    if callable(reset):
        reset()


# ── The latch closes an idle keep-alive connection ───────────────────


async def test_graceful_drain_quiesces_an_idle_connection():
    """This is what makes `wait_closed()` return promptly."""
    _reset_drain_latch()
    app = Veloce(openapi_url=None)
    proto = HttpProtocol(app, asyncio.get_running_loop())
    proto.connection_made(_FakeTransport())
    try:
        assert proto._draining is False
        HttpProtocol.start_graceful_drain()
        assert proto._draining is True
    finally:
        _reset_drain_latch()


async def test_a_connection_admitted_after_the_latch_starts_draining():
    """The race window: a client accepted just as shutdown begins."""
    _reset_drain_latch()
    app = Veloce(openapi_url=None)
    HttpProtocol.start_graceful_drain()
    try:
        late = HttpProtocol(app, asyncio.get_running_loop())
        late.connection_made(_FakeTransport())
        assert late._draining is True
    finally:
        _reset_drain_latch()


# ── The worker drains before it awaits the server ────────────────
#
# `test_worker_serve_drains_before_awaiting_wait_closed` used to live here. It
# built a stand-in server, called `start_graceful_drain()`, `close()` and
# `wait_closed()` in that order, and then asserted that the drain came first -
# the order of four statements the test body had just written. `VeloceWorker.
# _serve` was never invoked, so it could not fail.
#
# Driving the real `_serve` needs a gunicorn worker, and gunicorn is POSIX-only,
# so it cannot run on every box this suite does. The two source-inspection tests
# below are the guard instead: they read the shipped teardown block and assert
# the ordering there. The *behaviour* the ordering exists for - an idle
# keep-alive connection quiescing, and a connection admitted mid-shutdown
# starting drained - is covered by the two tests above, against the real
# `HttpProtocol`.


async def test_the_worker_source_orders_the_drain_first():
    """Pin the ordering in the shipped source, not just in a stand-in.

    A future edit that moves the drain back below the await would restore the
    30-second SIGKILL, and no unit test driving gunicorn can catch that on a
    box where gunicorn does not run at all (it is POSIX-only).
    """
    body = inspect.getsource(VeloceWorker._serve)
    # Scope to the teardown block: an earlier `wait_closed()` lives in the
    # start-up failure path, which is a different question. Comments are
    # stripped because the ones explaining this very ordering name both calls.
    teardown = _code_only(body[body.rindex("finally:") :])
    assert "start_graceful_drain()" in teardown, "the worker no longer drains on its way out"
    assert teardown.index("start_graceful_drain()") < teardown.index("wait_closed()"), (
        "the drain must precede wait_closed(), or an idle keep-alive connection "
        "holds shutdown past gunicorn's graceful_timeout"
    )


async def test_the_native_run_path_orders_the_drain_first():
    """`app.run()` had the same inversion via `async with server:`."""
    body = _code_only(inspect.getsource(ServingMixin._serve))
    assert "start_graceful_drain()" in body
    assert body.index("start_graceful_drain()") < body.index("finally:"), (
        "the drain must happen inside the `async with server:` block, before "
        "leaving it closes and awaits the server"
    )
