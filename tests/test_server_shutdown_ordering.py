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
from unittest import mock

from tests._protocol import _FakeTransport
from veloce import Veloce
from veloce.serving.protocol import HttpProtocol
from veloce.workers import VeloceWorker


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


class _RecordingServer:
    """An `asyncio.Server` stand-in that logs `close` and `wait_closed`.

    `async with server:` on a real server closes it and awaits `wait_closed()`
    on the way out, which is the half of the ordering that has to come second.
    """

    def __init__(self, order: list[str]) -> None:
        self._order = order

    async def __aenter__(self) -> _RecordingServer:
        return self

    async def __aexit__(self, *exc: object) -> None:
        self.close()
        await self.wait_closed()

    def close(self) -> None:
        self._order.append("close")

    async def wait_closed(self) -> None:
        self._order.append("wait_closed")


class _FakeGunicornSocket:
    """What gunicorn hands a worker: an object exposing the bound `.sock`."""

    sock = None


async def test_the_worker_drains_before_it_awaits_the_server():
    """The order is observed, not read off the source.

    This matched `inspect.getsource(VeloceWorker._serve)` and compared string
    offsets. Extracting the teardown into a helper would fail it while the
    ordering was unchanged, and a helper calling the two in the wrong order
    would pass it, because the text still read correctly - so it pinned the
    spelling of the function rather than what it does. Both calls are recorded
    here as they happen, wherever they are made from.
    """
    order: list[str] = []
    app = Veloce(openapi_url=None)
    loop = asyncio.get_running_loop()
    server = _RecordingServer(order)

    worker = VeloceWorker.__new__(VeloceWorker)
    worker.alive = True
    worker.timeout = 30
    worker.sockets = [_FakeGunicornSocket()]
    # `notify` and `_parent_alive` come from gunicorn's base class, which the
    # test environment does not install, so they are bound on the instance.
    worker.notify = lambda: None
    worker._parent_alive = lambda: True
    worker._stop = asyncio.Event()
    # Set before the loop runs, so the first `wait` returns and the teardown -
    # the only thing under test - happens immediately.
    worker._stop.set()

    async def fake_create_server(*args: object, **kwargs: object) -> _RecordingServer:
        return server

    with (
        mock.patch.object(VeloceWorker, "_veloce_app", lambda self: app),
        mock.patch.object(VeloceWorker, "_build_ssl_context", lambda self: None),
        mock.patch.object(loop, "create_server", fake_create_server),
        mock.patch.object(
            HttpProtocol, "start_graceful_drain", staticmethod(lambda: order.append("drain"))
        ),
    ):
        await worker._serve(loop)

    assert "drain" in order, "the worker no longer drains on its way out"
    assert "wait_closed" in order, "the worker no longer awaits the server"
    assert order.index("drain") < order.index("wait_closed"), (
        f"the drain must come first, or one idle keep-alive client holds "
        f"shutdown for KEEP_ALIVE_TIMEOUT; saw {order}"
    )


async def test_the_native_run_path_drains_before_it_awaits_the_server():
    """`app.run()` had the same inversion, via `async with server:`.

    Observed the same way and for the same reason; the previous form compared
    the offset of `start_graceful_drain()` against the offset of `finally:` in
    the source text of `ServingMixin._serve`.
    """
    order: list[str] = []
    app = Veloce(openapi_url=None)
    server = _RecordingServer(order)
    loop = asyncio.get_running_loop()

    async def fake_create_server(*args: object, **kwargs: object) -> _RecordingServer:
        return server

    def signals_that_fire_at_once(_loop: object, handler) -> tuple[bool, object]:
        """As if SIGTERM arrived the moment the server started listening."""
        handler()
        return True, None

    with (
        mock.patch.object(loop, "create_server", fake_create_server),
        mock.patch.object(app, "_install_shutdown_signals", signals_that_fire_at_once),
        mock.patch.object(app, "_restore_shutdown_signals", lambda _restore: None),
        mock.patch.object(
            HttpProtocol, "start_graceful_drain", staticmethod(lambda: order.append("drain"))
        ),
    ):
        await app._serve("127.0.0.1", 0)

    assert "drain" in order, "the native path no longer drains on its way out"
    assert "wait_closed" in order, "the native path no longer awaits the server"
    assert order.index("drain") < order.index("wait_closed"), (
        f"the drain must happen inside the `async with server:` block, before "
        f"leaving it closes and awaits the server; saw {order}"
    )
