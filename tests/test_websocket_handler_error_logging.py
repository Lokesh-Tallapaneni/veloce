"""A WebSocket handler that raises is reported, even when its close is cancelled.

`_run_websocket` closes with 1011 and re-raises so the driving server logs the
failure. Under an ASGI server that works: the exception leaves the app and the
server logs it. On the native transport it did not, and the reason is a race
rather than a missing call.

The close awaits — RFC 6455 Sec. 5.5.1 is a handshake, not a write. A peer that
has already gone brings `connection_lost` in, which cancels the dispatch task
mid-handshake. The re-raise never runs, the task ends *cancelled*, and the
done-callback treated a cancellation as nothing to report. The handler's
exception was destroyed with the frame that held it: nothing was logged even at
`DEBUG`, on the built-in server only.

So the exception is recorded on the socket before the close, and the native
done-callback reads it back when the task was cancelled.
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from veloce import Veloce, WebSocket, status
from veloce._protocol_constants import ROUTE_METHOD_WEBSOCKET
from veloce.exceptions import WebSocketException, WebSocketRequestValidationError
from veloce.serving.protocol import HttpProtocol


class _FakeTransport(asyncio.Transport):
    """Minimal raw transport; the close handshake has somewhere to write."""

    def __init__(self) -> None:
        super().__init__()
        self.writes: list[bytes] = []
        self.closed = False

    def write(self, data: bytes) -> None:
        self.writes.append(data)

    def writelines(self, data) -> None:
        self.writes.append(b"".join(bytes(chunk) for chunk in data))

    def close(self) -> None:
        self.closed = True

    def is_closing(self) -> bool:
        return self.closed


def _native_ws() -> WebSocket:
    """An unaccepted native socket; the handler under test accepts it."""
    return WebSocket(_FakeTransport(), {"sec-websocket-key": "dGhlIHNhbXBsZSBub25jZQ=="})


async def _run(handler) -> WebSocket:
    """Drive `handler` through the shared dispatch core on a native socket."""
    app = Veloce(openapi_url=None)
    app.add_route("/ws", handler, methods=[ROUTE_METHOD_WEBSOCKET])
    route_info = app.match(ROUTE_METHOD_WEBSOCKET, "/ws").route_info
    ws = _native_ws()
    ws.path_params = {}
    with pytest.raises(BaseException):  # noqa: B017 - the exit shape is per-test
        await app._run_websocket(ws, route_info)
    return ws


# ── The exception is recorded where a cancellation cannot destroy it ──


async def test_a_handler_exception_is_recorded_on_the_socket():
    async def boom(ws: WebSocket):
        await ws.accept()
        raise RuntimeError("ws-kaboom-marker")

    ws = await _run(boom)
    assert isinstance(ws._handler_exc, RuntimeError)
    assert str(ws._handler_exc) == "ws-kaboom-marker"


async def test_the_exception_survives_a_cancellation_during_the_close():
    """The race itself: cancel while the close handshake is still waiting.

    This is what `connection_lost` does when the peer has gone. Before the fix
    the handler's exception died with the frame and the task reported only a
    cancellation.
    """

    async def boom(ws: WebSocket):
        await ws.accept()
        raise RuntimeError("ws-kaboom-marker")

    app = Veloce(openapi_url=None)
    app.add_route("/ws", boom, methods=[ROUTE_METHOD_WEBSOCKET])
    route_info = app.match(ROUTE_METHOD_WEBSOCKET, "/ws").route_info
    ws = _native_ws()
    ws.path_params = {}

    task = asyncio.get_event_loop().create_task(app._run_websocket(ws, route_info))
    # Let it raise and reach the close handshake, which waits for a peer close
    # frame that this transport will never deliver.
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert task.cancelled()
    assert isinstance(ws._handler_exc, RuntimeError)
    assert str(ws._handler_exc) == "ws-kaboom-marker"


async def test_that_cancelled_task_is_then_reported(caplog):
    """End to end through the native callback: the race no longer loses it."""

    async def boom(ws: WebSocket):
        await ws.accept()
        raise RuntimeError("ws-kaboom-marker")

    app = Veloce(openapi_url=None)
    app.add_route("/ws", boom, methods=[ROUTE_METHOD_WEBSOCKET])
    route_info = app.match(ROUTE_METHOD_WEBSOCKET, "/ws").route_info
    ws = _native_ws()
    ws.path_params = {}

    task = asyncio.get_event_loop().create_task(app._run_websocket(ws, route_info))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    with caplog.at_level(logging.ERROR):
        HttpProtocol._ws_task_done(ws, task)
    assert "ws-kaboom-marker" in caplog.text


async def test_a_clean_exit_records_nothing():
    async def fine(ws: WebSocket):
        await ws.accept()

    app = Veloce(openapi_url=None)
    app.add_route("/ws", fine, methods=[ROUTE_METHOD_WEBSOCKET])
    route_info = app.match(ROUTE_METHOD_WEBSOCKET, "/ws").route_info
    ws = _native_ws()
    ws.path_params = {}
    await app._run_websocket(ws, route_info)
    assert ws._handler_exc is None


async def test_an_application_driven_close_records_nothing():
    """`WebSocketException` is the app closing deliberately, not a failure."""

    async def closing(ws: WebSocket):
        await ws.accept()
        raise WebSocketException(code=status.WS_1008_POLICY_VIOLATION, reason="nope")

    app = Veloce(openapi_url=None)
    app.add_route("/ws", closing, methods=[ROUTE_METHOD_WEBSOCKET])
    route_info = app.match(ROUTE_METHOD_WEBSOCKET, "/ws").route_info
    ws = _native_ws()
    ws.path_params = {}
    await app._run_websocket(ws, route_info)
    assert ws._handler_exc is None


async def test_a_validation_failure_records_nothing():
    async def bad(ws: WebSocket):
        await ws.accept()
        raise WebSocketRequestValidationError([])

    app = Veloce(openapi_url=None)
    app.add_route("/ws", bad, methods=[ROUTE_METHOD_WEBSOCKET])
    route_info = app.match(ROUTE_METHOD_WEBSOCKET, "/ws").route_info
    ws = _native_ws()
    ws.path_params = {}
    await app._run_websocket(ws, route_info)
    assert ws._handler_exc is None


# ── The native done-callback reports it ──────────────────────────────


def _cancelled_task() -> asyncio.Task:
    async def never():  # pragma: no cover - cancelled before it finishes
        await asyncio.sleep(3600)

    loop = asyncio.get_event_loop()
    task = loop.create_task(never())
    task.cancel()
    return task


async def test_a_cancelled_task_still_reports_the_recorded_exception(caplog):
    """The defect: a cancellation was treated as nothing to report."""
    task = _cancelled_task()
    with pytest.raises(asyncio.CancelledError):
        await task
    ws = _native_ws()
    ws._handler_exc = RuntimeError("ws-kaboom-marker")

    with caplog.at_level(logging.ERROR):
        HttpProtocol._ws_task_done(ws, task)

    records = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(records) == 1
    assert "ws-kaboom-marker" in caplog.text
    assert records[0].exc_info is not None


async def test_a_cancellation_with_nothing_recorded_reports_nothing(caplog):
    """A connection cancelled for any other reason is not an error."""
    task = _cancelled_task()
    with pytest.raises(asyncio.CancelledError):
        await task
    ws = _native_ws()

    with caplog.at_level(logging.ERROR):
        HttpProtocol._ws_task_done(ws, task)

    assert [r for r in caplog.records if r.levelno >= logging.ERROR] == []


async def test_an_uncancelled_failure_is_still_reported(caplog):
    """The path that already worked must keep working."""

    async def raiser():
        raise RuntimeError("ws-kaboom-marker")

    task = asyncio.get_event_loop().create_task(raiser())
    with pytest.raises(RuntimeError):
        await task
    ws = _native_ws()

    with caplog.at_level(logging.ERROR):
        HttpProtocol._ws_task_done(ws, task)

    assert "ws-kaboom-marker" in caplog.text


async def test_a_clean_task_reports_nothing(caplog):
    async def fine():
        return None

    task = asyncio.get_event_loop().create_task(fine())
    await task
    ws = _native_ws()

    with caplog.at_level(logging.ERROR):
        HttpProtocol._ws_task_done(ws, task)

    assert [r for r in caplog.records if r.levelno >= logging.ERROR] == []


async def test_the_task_is_dropped_from_the_active_set_either_way():
    """The callback's other job must survive the new branch."""
    task = _cancelled_task()
    with pytest.raises(asyncio.CancelledError):
        await task
    HttpProtocol._active_tasks.add(task)
    HttpProtocol._ws_task_done(_native_ws(), task)
    assert task not in HttpProtocol._active_tasks
