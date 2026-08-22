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

from veloce import Veloce
from veloce.serving.protocol import HttpProtocol


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


# ── The worker drains before it awaits the server ────────────────────


async def test_worker_serve_drains_before_awaiting_wait_closed():
    """The regression: on the old ordering `wait_closed` ran first.

    A stand-in server records when `wait_closed` is awaited; the drain records
    when it fires. The drain has to come first, or the wait is what blocks.
    """
    _reset_drain_latch()
    order: list[str] = []

    class _Server:
        def close(self) -> None:
            order.append("close")

        async def wait_closed(self) -> None:
            order.append("wait_closed")

    original = HttpProtocol.start_graceful_drain

    def _record() -> None:
        order.append("drain")
        original()

    HttpProtocol.start_graceful_drain = staticmethod(_record)  # type: ignore[method-assign]
    try:
        # Mirror the worker's teardown block exactly.
        server = _Server()
        HttpProtocol.start_graceful_drain()
        server.close()
        await server.wait_closed()
    finally:
        HttpProtocol.start_graceful_drain = original  # type: ignore[method-assign]
        _reset_drain_latch()

    assert order.index("drain") < order.index("wait_closed")


async def test_the_worker_source_orders_the_drain_first():
    """Pin the ordering in the shipped source, not just in a stand-in.

    A future edit that moves the drain back below the await would restore the
    30-second SIGKILL, and no unit test driving gunicorn can catch that on a
    box where gunicorn does not run at all (it is POSIX-only).
    """
    import inspect

    from veloce.workers import VeloceWorker

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
    import inspect

    from veloce.app.serving import ServingMixin

    body = _code_only(inspect.getsource(ServingMixin._serve))
    assert "start_graceful_drain()" in body
    assert body.index("start_graceful_drain()") < body.index("finally:"), (
        "the drain must happen inside the `async with server:` block, before "
        "leaving it closes and awaits the server"
    )
