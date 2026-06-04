"""Lifecycle, setup-lock, spawn, and graceful-shutdown behaviour.

Covers the app-core findings: AsyncExitStack-driven lifespan unwind on partial
startup failure, the setup-after-first-request lock, ASGI
``lifespan.shutdown.failed`` reporting, app-scoped ``spawn`` tasks drained on
shutdown, and the native two-phase graceful drain.
"""

from __future__ import annotations

import asyncio
import builtins
import contextlib
import sys

import pytest

from veloce import SetupError, Veloce
from veloce.testclient import TestClient

# -- AsyncExitStack lifespan unwind ----------------------------------


@pytest.mark.asyncio
async def test_partial_startup_failure_unwinds_acquired_resources():
    """A startup handler that raises unwinds the lifespan CM already entered."""
    order: list[str] = []

    @contextlib.asynccontextmanager
    async def lifespan(app):
        order.append("cm-enter")
        try:
            yield
        finally:
            order.append("cm-exit")

    app = Veloce(lifespan=lifespan)

    @app.on_startup
    async def boom():
        order.append("startup")
        raise RuntimeError("startup failed")

    with pytest.raises(RuntimeError, match="startup failed"):
        await app._run_lifecycle("startup")

    # The lifespan CM entered before the failing handler must be exited during
    # the unwind, leaving no orphaned resource.
    assert order == ["cm-enter", "startup", "cm-exit"]


@pytest.mark.asyncio
async def test_clean_startup_then_shutdown_runs_teardowns_in_reverse():
    order: list[str] = []

    @contextlib.asynccontextmanager
    async def lifespan(app):
        order.append("cm-enter")
        yield
        order.append("cm-exit")

    app = Veloce(lifespan=lifespan)

    @app.on_shutdown
    async def first():
        order.append("shutdown-first")

    @app.on_shutdown
    async def second():
        order.append("shutdown-second")

    await app._run_lifecycle("startup")
    assert order == ["cm-enter"]
    await app._run_lifecycle("shutdown")
    # Stack unwinds LIFO: last-registered shutdown handler runs first, the
    # lifespan CM (entered first) exits last.
    assert order == ["cm-enter", "shutdown-second", "shutdown-first", "cm-exit"]


@pytest.mark.asyncio
@pytest.mark.skipif(sys.version_info < (3, 11), reason="ExceptionGroup requires 3.11+")
async def test_shutdown_runs_all_teardowns_and_groups_failures():
    ran: list[str] = []

    app = Veloce()

    @app.on_shutdown
    async def ok():
        ran.append("ok")

    @app.on_shutdown
    async def bad_a():
        ran.append("bad_a")
        raise ValueError("a")

    @app.on_shutdown
    async def bad_b():
        ran.append("bad_b")
        raise KeyError("b")

    await app._run_lifecycle("startup")
    with pytest.raises(BaseException) as excinfo:  # noqa: PT011
        await app._run_lifecycle("shutdown")

    # Every teardown ran even though two raised.
    assert set(ran) == {"ok", "bad_a", "bad_b"}
    group = excinfo.value
    # `BaseExceptionGroup` is a builtin from 3.11; this test is skipped below.
    base_group = builtins.BaseExceptionGroup
    assert isinstance(group, base_group)
    kinds = {type(e) for e in group.exceptions}
    assert ValueError in kinds and KeyError in kinds


@pytest.mark.asyncio
async def test_standalone_shutdown_without_startup_runs_handlers():
    """`_run_lifecycle('shutdown')` with no prior startup still runs handlers."""
    fired: list[str] = []
    app = Veloce()

    @app.on_shutdown
    async def cleanup():
        fired.append("done")

    await app._run_lifecycle("shutdown")
    assert fired == ["done"]


# -- Setup-after-first-request lock ----------------------------------


def test_setup_locks_after_first_request_outside_debug():
    app = Veloce(openapi_url=None)

    @app.get("/a")
    async def a():
        return {"ok": True}

    asyncio.run(app.handle_request(_get("/a")))

    with pytest.raises(SetupError):

        @app.get("/late")
        async def late():
            return {}

    with pytest.raises(SetupError):

        @app.before_request
        async def hook(request):
            return None


def test_setup_lock_relaxed_under_debug():
    app = Veloce(debug=True, openapi_url=None)

    @app.get("/a")
    async def a():
        return {}

    asyncio.run(app.handle_request(_get("/a")))

    # DEBUG keeps setup mutable for hot-reload ergonomics.
    @app.get("/late")
    async def late():
        return {}


def test_setup_lock_relaxed_under_testclient():
    app = Veloce(openapi_url=None)

    @app.get("/a")
    async def a():
        return {"v": 1}

    with TestClient(app) as client:
        assert client.get("/a").status_code == 200

        # In-memory test dispatch relaxes the lock so a test can keep wiring.
        @app.get("/b")
        async def b():
            return {"v": 2}

        assert client.get("/b").status_code == 200


# -- app.spawn -------------------------------------------------------


@pytest.mark.asyncio
async def test_spawn_runs_and_is_drained_on_shutdown():
    app = Veloce()
    started = asyncio.Event()
    cancelled: list[str] = []

    async def worker():
        started.set()
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            cancelled.append("worker")
            raise

    await app._run_lifecycle("startup")
    task = app.spawn(worker(), name="worker")
    await started.wait()
    assert app.get_spawned_task("worker") is task

    await app._run_lifecycle("shutdown")
    assert task.cancelled() or task.done()
    assert cancelled == ["worker"]
    # Registry is cleared after the drain.
    assert app.get_spawned_task("worker") is None


@pytest.mark.asyncio
async def test_task_spawned_in_on_shutdown_is_drained():
    # The spawned-task drain runs AFTER the on_shutdown handlers, so a task a
    # teardown callback spawns via app.spawn(...) is still cancelled and drained
    # by the end of shutdown instead of surviving past it.
    app = Veloce()
    started = asyncio.Event()
    cancelled: list[str] = []

    async def late_worker():
        started.set()
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            cancelled.append("late")
            raise

    spawned: list[asyncio.Task] = []

    @app.on_shutdown
    async def spawn_on_teardown():
        task = app.spawn(late_worker(), name="late")
        spawned.append(task)
        # Let the task reach its first await so cancellation has something to
        # interrupt; the drain that follows teardown must still reap it.
        await started.wait()

    await app._run_lifecycle("startup")
    await app._run_lifecycle("shutdown")

    assert spawned, "on_shutdown handler did not spawn a task"
    task = spawned[0]
    assert task.cancelled() or task.done()
    assert cancelled == ["late"]
    # Registry is cleared by the post-teardown drain.
    assert app.get_spawned_task("late") is None


@pytest.mark.asyncio
async def test_spawn_duplicate_name_raises():
    app = Veloce()

    async def idle():
        await asyncio.sleep(3600)

    app.spawn(idle(), name="dup")
    dup_coro = idle()
    try:
        with pytest.raises(ValueError, match="already exists"):
            app.spawn(dup_coro, name="dup")
    finally:
        dup_coro.close()
    await app._run_lifecycle("shutdown")


@pytest.mark.asyncio
async def test_cancel_spawned_task_by_name():
    app = Veloce()

    async def idle():
        await asyncio.sleep(3600)

    task = app.spawn(idle(), name="c")
    assert app.cancel_spawned_task("c") is True
    assert app.cancel_spawned_task("missing") is False
    with contextlib.suppress(asyncio.CancelledError):
        await task


def test_spawn_without_running_loop_raises():
    app = Veloce()

    async def idle():
        await asyncio.sleep(0)

    coro = idle()
    try:
        with pytest.raises(RuntimeError, match="running event loop"):
            app.spawn(coro)
    finally:
        coro.close()


# -- ASGI lifespan.shutdown.failed -----------------------------------


@pytest.mark.asyncio
async def test_asgi_lifespan_shutdown_failed_message():
    app = Veloce()

    @app.on_shutdown
    async def bad():
        raise RuntimeError("teardown exploded")

    incoming = [
        {"type": "lifespan.startup"},
        {"type": "lifespan.shutdown"},
    ]
    sent: list[dict] = []

    async def receive():
        return incoming.pop(0)

    async def send(message):
        sent.append(message)

    await app({"type": "lifespan"}, receive, send)

    types = [m["type"] for m in sent]
    assert "lifespan.startup.complete" in types
    assert "lifespan.shutdown.failed" in types
    failed = next(m for m in sent if m["type"] == "lifespan.shutdown.failed")
    assert "teardown exploded" in failed["message"]


# -- Native two-phase graceful drain ---------------------------------


def test_begin_drain_closes_idle_connection():
    from veloce.serving.protocol import HttpProtocol

    loop = asyncio.new_event_loop()
    try:
        proto = HttpProtocol(Veloce(openapi_url=None), loop)
        transport = _RecordingTransport()
        proto.connection_made(transport)
        # Idle keep-alive connection: drain closes it at once.
        proto.begin_drain()
        assert proto._draining is True
        assert transport.closed is True
    finally:
        loop.close()


def test_start_graceful_drain_flips_live_connections():
    from veloce.serving.protocol import HttpProtocol

    loop = asyncio.new_event_loop()
    try:
        HttpProtocol.reset_graceful_drain()
        proto = HttpProtocol(Veloce(openapi_url=None), loop)
        transport = _RecordingTransport()
        proto.connection_made(transport)
        HttpProtocol.start_graceful_drain()
        assert proto._draining is True
    finally:
        HttpProtocol.reset_graceful_drain()
        loop.close()


def test_draining_serves_inflight_request_and_declines_pipelined_followup():
    """Two pipelined requests: with the connection draining, the in-flight
    request is served in full and the pipelined follow-up is declined (the
    connection closes at the boundary instead of cancelling mid-pipeline)."""
    from veloce.serving.protocol import HttpProtocol

    loop = asyncio.new_event_loop()
    try:
        HttpProtocol.reset_graceful_drain()
        app = Veloce(openapi_url=None)

        @app.get("/a")
        async def a(request):  # noqa: ANN001, ANN202
            return {"who": "A"}

        @app.get("/b")
        async def b(request):  # noqa: ANN001, ANN202
            return {"who": "B"}

        from tests.test_server_protocol import _FakeTransport

        proto = HttpProtocol(app, loop)
        transport = _FakeTransport()
        proto.connection_made(transport)
        proto.data_received(b"GET /a HTTP/1.1\r\nHost: x\r\n\r\nGET /b HTTP/1.1\r\nHost: x\r\n\r\n")
        # Quiesce before the loop runs: the already-popped request A completes,
        # then the loop closes at the boundary without serving B.
        proto.begin_drain()
        if proto._server_loop is not None:
            loop.run_until_complete(proto._server_loop)

        emitted = b"".join(transport.writes)
        assert b'"who":"A"' in emitted
        assert b'"who":"B"' not in emitted
        assert transport.closed is True
    finally:
        HttpProtocol.reset_graceful_drain()
        loop.close()


# -- Helpers ---------------------------------------------------------


def _get(path: str):
    from veloce.http.request import Request

    return Request(method="GET", path=path, query_string="", headers={}, body=b"")


class _RecordingTransport:
    """Minimal full-duplex transport stand-in for protocol unit tests."""

    def __init__(self) -> None:
        self.closed = False
        self._buf: list[bytes] = []

    def write(self, data: bytes) -> None:
        self._buf.append(data)

    def pause_reading(self) -> None:
        pass

    def resume_reading(self) -> None:
        pass

    def set_write_buffer_limits(self, high: int | None = None, low: int | None = None) -> None:
        pass

    def is_closing(self) -> bool:
        return self.closed

    def close(self) -> None:
        self.closed = True
