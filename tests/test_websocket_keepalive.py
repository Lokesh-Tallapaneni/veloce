"""WebSocket idle-timeout tests (WS-3).

Cover the opt-in `idle_timeout` keep-alive: a silent peer trips a clean
RFC 6455 `1001 Going Away` close and the handler sees `WebSocketDisconnect`;
activity resets the window; `idle_timeout=None` preserves the prior
behavior; and a smaller per-call `timeout` still wins over the idle window.
"""

from __future__ import annotations

import asyncio

import pytest

from tests._ws_frames import client_frame as _client_frame
from veloce.exceptions import WebSocketDisconnect
from veloce.status import WS_1001_GOING_AWAY
from veloce.websocket import WebSocket


class _FakeTransport:
    """Minimal asyncio.Transport stand-in for raw-mode WebSocket tests."""

    def __init__(self) -> None:
        self.writes: list[bytes] = []
        self.closed = False

    def write(self, data: bytes) -> None:
        self.writes.append(data)

    def writelines(self, chunks) -> None:
        for chunk in chunks:
            self.writes.append(bytes(chunk))

    def close(self) -> None:
        self.closed = True


def _asgi_ws(
    receive,
    sent: list[dict],
    idle_timeout: float | None = None,
) -> WebSocket:
    async def send(message: dict) -> None:
        sent.append(message)

    ws = WebSocket.from_asgi(
        {"type": "websocket", "path": "/ws", "headers": []},
        receive,
        send,
        idle_timeout=idle_timeout,
    )
    ws._accepted = True
    return ws


# ── Construction / validation ────────────────────────────────────────


def test_idle_timeout_defaults_to_none_in_init():
    ws = WebSocket(_FakeTransport(), {"sec-websocket-key": "k"})
    assert ws._idle_timeout is None


def test_idle_timeout_defaults_to_none_in_from_asgi():
    ws = WebSocket.from_asgi({"type": "websocket", "path": "/ws"}, None, None)
    assert ws._idle_timeout is None


@pytest.mark.parametrize("bad", [0, -1.0, float("nan"), float("inf"), -float("inf")])
def test_init_rejects_non_positive_or_non_finite_idle_timeout(bad):
    with pytest.raises(ValueError, match="idle_timeout"):
        WebSocket(_FakeTransport(), {}, idle_timeout=bad)


@pytest.mark.parametrize("bad", [0, -0.5, float("nan"), float("inf")])
def test_from_asgi_rejects_bad_idle_timeout(bad):
    with pytest.raises(ValueError, match="idle_timeout"):
        WebSocket.from_asgi({"type": "websocket", "path": "/ws"}, None, None, idle_timeout=bad)


def test_set_idle_timeout_validates_and_updates():
    ws = WebSocket.from_asgi({"type": "websocket", "path": "/ws"}, None, None)
    ws.set_idle_timeout(0.05)
    assert ws._idle_timeout == 0.05
    ws.set_idle_timeout(None)
    assert ws._idle_timeout is None
    with pytest.raises(ValueError, match="idle_timeout"):
        ws.set_idle_timeout(-1)


# ── Timeout fires on a silent peer (ASGI mode) ───────────────────────


@pytest.mark.asyncio
async def test_idle_timeout_closes_1001_and_raises_disconnect():
    async def never() -> dict:
        # A silent peer: never delivers a frame.
        await asyncio.sleep(3600)
        raise AssertionError("unreachable")

    sent: list[dict] = []
    ws = _asgi_ws(never, sent, idle_timeout=0.02)

    with pytest.raises(WebSocketDisconnect) as exc_info:
        await ws.receive_text()

    # The raised disconnect carries the idle close code so handlers can
    # tell an idle timeout apart from a normal 1000 close.
    assert exc_info.value.code == WS_1001_GOING_AWAY
    # A clean RFC 6455 close was sent with 1001 - never a fabricated 1006.
    assert sent == [{"type": "websocket.close", "code": WS_1001_GOING_AWAY, "reason": ""}]
    assert ws._closed is True


@pytest.mark.asyncio
async def test_idle_timeout_applies_to_receive_bytes():
    async def never() -> dict:
        await asyncio.sleep(3600)
        raise AssertionError("unreachable")

    sent: list[dict] = []
    ws = _asgi_ws(never, sent, idle_timeout=0.02)

    with pytest.raises(WebSocketDisconnect):
        await ws.receive_bytes()

    assert sent[-1]["code"] == WS_1001_GOING_AWAY


@pytest.mark.asyncio
async def test_idle_timeout_applies_to_raw_receive():
    async def never() -> dict:
        await asyncio.sleep(3600)
        raise AssertionError("unreachable")

    sent: list[dict] = []
    ws = _asgi_ws(never, sent, idle_timeout=0.02)

    with pytest.raises(WebSocketDisconnect):
        await ws.receive()

    assert sent[-1]["code"] == WS_1001_GOING_AWAY


@pytest.mark.asyncio
async def test_iter_text_loop_unwinds_cleanly_on_idle_timeout():
    async def never() -> dict:
        await asyncio.sleep(3600)
        raise AssertionError("unreachable")

    sent: list[dict] = []
    ws = _asgi_ws(never, sent, idle_timeout=0.02)

    received: list[str] = []
    async for msg in ws.iter_text():
        received.append(msg)

    assert received == []
    assert sent[-1]["code"] == WS_1001_GOING_AWAY


# ── Activity resets the window ───────────────────────────────────────


@pytest.mark.asyncio
async def test_activity_resets_idle_window():
    # Three frames arrive each well within the idle window; the timeout
    # must not fire while the peer keeps sending.
    frames = ["a", "b", "c"]

    async def receive() -> dict:
        await asyncio.sleep(0.005)
        if frames:
            return {"type": "websocket.receive", "text": frames.pop(0)}
        await asyncio.sleep(3600)
        raise AssertionError("unreachable")

    sent: list[dict] = []
    ws = _asgi_ws(receive, sent, idle_timeout=0.1)

    assert await ws.receive_text() == "a"
    assert await ws.receive_text() == "b"
    assert await ws.receive_text() == "c"
    # No close emitted while frames kept arriving.
    assert sent == []


# ── idle_timeout=None preserves current behavior ─────────────────────


@pytest.mark.asyncio
async def test_none_idle_timeout_does_not_close_on_slow_peer():
    async def receive() -> dict:
        await asyncio.sleep(0.03)
        return {"type": "websocket.receive", "text": "late"}

    sent: list[dict] = []
    ws = _asgi_ws(receive, sent, idle_timeout=None)

    # Even though the peer is slow, no idle window is configured, so the
    # frame is delivered and nothing is closed.
    assert await ws.receive_text() == "late"
    assert sent == []


@pytest.mark.asyncio
async def test_none_idle_timeout_explicit_timeout_still_raises_timeouterror():
    async def never() -> dict:
        await asyncio.sleep(3600)
        raise AssertionError("unreachable")

    sent: list[dict] = []
    ws = _asgi_ws(never, sent, idle_timeout=None)

    # Pre-existing per-call timeout behavior is untouched: a TimeoutError,
    # not a WebSocketDisconnect, and no close frame.
    with pytest.raises((TimeoutError, asyncio.TimeoutError)):
        await ws.receive_text(timeout=0.02)
    assert sent == []
    assert ws._closed is False


# ── Interaction between idle_timeout and an explicit per-call timeout ─


@pytest.mark.asyncio
async def test_smaller_per_call_timeout_wins_over_idle_timeout():
    async def never() -> dict:
        await asyncio.sleep(3600)
        raise AssertionError("unreachable")

    sent: list[dict] = []
    # idle_timeout is large; the per-call timeout is the binding deadline.
    ws = _asgi_ws(never, sent, idle_timeout=10.0)

    # The smaller per-call timeout wins: a plain TimeoutError, no idle
    # close, connection stays open for the caller to decide.
    with pytest.raises((TimeoutError, asyncio.TimeoutError)):
        await ws.receive_text(timeout=0.02)
    assert sent == []
    assert ws._closed is False


@pytest.mark.asyncio
async def test_idle_timeout_wins_when_smaller_than_per_call_timeout():
    async def never() -> dict:
        await asyncio.sleep(3600)
        raise AssertionError("unreachable")

    sent: list[dict] = []
    # idle_timeout is the binding (smaller) deadline; it should close 1001.
    ws = _asgi_ws(never, sent, idle_timeout=0.02)

    with pytest.raises(WebSocketDisconnect):
        await ws.receive_text(timeout=10.0)
    assert sent[-1]["code"] == WS_1001_GOING_AWAY
    assert ws._closed is True


# ── Raw-transport mode (queue-backed) ────────────────────────────────


@pytest.mark.asyncio
async def test_raw_mode_idle_timeout_closes_1001():
    transport = _FakeTransport()
    ws = WebSocket(transport, {"sec-websocket-key": "k"}, idle_timeout=0.02)
    ws._accepted = True

    # Nothing is ever put on the receive queue: a silent peer.
    with pytest.raises(WebSocketDisconnect):
        await ws.receive_text()

    # Raw mode sends a close frame via writelines((header, payload)); the
    # fake transport records the two buffers separately.
    assert len(transport.writes) >= 2, "expected a close frame to be written"
    header, payload = transport.writes[-2], transport.writes[-1]
    # FIN + close opcode 0x8.
    assert header[0] & 0x0F == 0x8
    # The 2-byte big-endian close status code.
    code = int.from_bytes(payload[:2], "big")
    assert code == WS_1001_GOING_AWAY
    assert transport.closed is True


@pytest.mark.asyncio
async def test_raw_mode_activity_delivers_before_idle_close():
    transport = _FakeTransport()
    ws = WebSocket(transport, {"sec-websocket-key": "k"}, idle_timeout=0.2)
    ws._accepted = True

    # Feed a message onto the queue well within the window.
    ws._receive_queue.put_nowait(b"hello")
    assert await ws.receive_text() == "hello"
    assert transport.closed is False


@pytest.mark.asyncio
async def test_raw_mode_idle_window_is_per_completed_message():
    """In raw-transport mode the idle window bounds each COMPLETE message: a
    fragmented message that fully assembles within the window is delivered.
    Raw transport is not the production path (ASGI delivers complete messages
    and owns ping/pong), so the window is measured per message, not per frame."""
    transport = _FakeTransport()
    ws = WebSocket(transport, {"sec-websocket-key": "k"}, idle_timeout=0.2)
    ws._accepted = True

    async def feed() -> None:
        # Both fragments assemble well within the 0.2s idle window.
        await asyncio.sleep(0.03)
        ws.feed_data(_client_frame(0x1, b"frag-", fin=False))
        await asyncio.sleep(0.03)
        ws.feed_data(_client_frame(0x0, b"end", fin=True))

    feeder = asyncio.ensure_future(feed())
    try:
        assert await ws.receive_text() == "frag-end"
    finally:
        await feeder
    assert transport.closed is False


@pytest.mark.asyncio
async def test_raw_mode_silent_after_frame_still_idle_closes():
    """Once frames stop arriving the idle window must still fire: a single
    frame followed by silence past the window trips a clean 1001 close."""
    transport = _FakeTransport()
    ws = WebSocket(transport, {"sec-websocket-key": "k"}, idle_timeout=0.04)
    ws._accepted = True

    async def feed() -> None:
        await asyncio.sleep(0.02)
        ws.feed_data(_client_frame(0x9, b"", fin=True))  # one ping, then silence

    feeder = asyncio.ensure_future(feed())
    try:
        with pytest.raises(WebSocketDisconnect):
            await ws.receive_text()
    finally:
        await feeder
    assert transport.closed is True


# ── The configured window reaches both transports ────────────────────
#
# `WEBSOCKET_IDLE_TIMEOUT` was read only where the native transport builds its
# socket, so the one config-driven way to reap a silent peer was inert under an
# ASGI server - the transport most apps deploy. It is applied at
# `Veloce._run_websocket` instead, the single funnel both transports dispatch
# through, so a transport added later inherits it.


def _idle_app(**config):
    from veloce import Veloce

    app = Veloce(openapi_url=None)
    app.config.update(config)

    @app.websocket("/ws")
    async def handler(ws):
        await ws.accept()
        try:
            await ws.receive_text()
        except WebSocketDisconnect:
            return

    return app


def _observed_timeout(**config) -> float | None:
    """The idle timeout the socket actually carries once dispatch has begun."""
    from veloce import Veloce

    seen: dict[str, float | None] = {}
    app = Veloce(openapi_url=None)
    app.config.update(config)

    @app.websocket("/ws")
    async def handler(ws):
        await ws.accept()
        seen["timeout"] = ws._idle_timeout
        await ws.close()

    from veloce.testclient import TestClient

    with TestClient(app).websocket_connect("/ws"):
        pass
    return seen.get("timeout")


def test_the_configured_window_reaches_an_asgi_socket():
    """The defect: only the native transport read this key."""
    assert _observed_timeout(WEBSOCKET_IDLE_TIMEOUT=12.5) == 12.5


def test_no_configured_window_leaves_the_socket_unbounded():
    assert _observed_timeout() is None
    assert _observed_timeout(WEBSOCKET_IDLE_TIMEOUT=None) is None


def test_a_handler_can_still_override_the_configured_window():
    """`set_idle_timeout` is documented as the handler's own control."""
    from veloce import Veloce
    from veloce.testclient import TestClient

    seen: dict[str, float | None] = {}
    app = Veloce(openapi_url=None)
    app.config["WEBSOCKET_IDLE_TIMEOUT"] = 30.0

    @app.websocket("/ws")
    async def handler(ws):
        await ws.accept()
        ws.set_idle_timeout(1.0)
        seen["timeout"] = ws._idle_timeout
        await ws.close()

    with TestClient(app).websocket_connect("/ws"):
        pass
    assert seen["timeout"] == 1.0
