"""Proactive WebSocket heartbeat / pong-timeout (raw-transport mode).

An opt-in `heartbeat` arms a timer that sends a tokened PING every interval;
the peer must answer with a PONG (or send any other frame) before the next
tick or the connection is dropped with a 1006 close code. Any inbound byte
defers the next probe so busy connections send no needless pings. ASGI mode
never starts a timer - the server owns ping/pong there.
"""

from __future__ import annotations

import asyncio
import struct

import pytest

from veloce.exceptions import WebSocketDisconnect
from veloce.status import WS_1006_ABNORMAL_CLOSURE
from veloce.websocket import WebSocket


class _FakeTransport:
    """Minimal asyncio.Transport stand-in recording frames and closes."""

    def __init__(self) -> None:
        self.writes: list[bytes] = []
        self.closed = False

    def write(self, data: bytes) -> None:
        self.writes.append(data)

    def writelines(self, parts) -> None:
        self.writes.append(b"".join(parts))

    def close(self) -> None:
        self.closed = True


def _make_ws(heartbeat: float | None) -> tuple[WebSocket, _FakeTransport]:
    transport = _FakeTransport()
    ws = WebSocket(
        transport,  # type: ignore[arg-type]
        {"sec-websocket-key": "dGhlIHNhbXBsZSBub25jZQ=="},
        heartbeat=heartbeat,
    )
    return ws, transport


def _ping_frames(transport: _FakeTransport) -> list[bytes]:
    """Server PING frames (FIN+opcode 0x9) the heartbeat emitted."""
    return [w for w in transport.writes if w and (w[0] & 0x0F) == 0x9]


def _client_frame(opcode: int, payload: bytes, fin: bool = True) -> bytes:
    mask = b"\x11\x22\x33\x44"
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    b0 = (0x80 if fin else 0x00) | opcode
    n = len(payload)
    header = bytes([b0, 0x80 | n])
    return header + mask + masked


def test_heartbeat_rejects_non_positive() -> None:
    with pytest.raises(ValueError):
        WebSocket(None, {}, heartbeat=0)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        WebSocket(None, {}, heartbeat=-1.0)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        WebSocket(None, {}, heartbeat=float("inf"))  # type: ignore[arg-type]


def test_heartbeat_disabled_by_default() -> None:
    async def go() -> None:
        ws, transport = _make_ws(heartbeat=None)
        ws.start_heartbeat()
        assert ws._hb_handle is None
        await asyncio.sleep(0.05)
        assert _ping_frames(transport) == []

    asyncio.run(go())


def test_heartbeat_sends_tokened_ping() -> None:
    async def go() -> None:
        ws, transport = _make_ws(heartbeat=0.02)
        ws.start_heartbeat()
        assert ws._hb_handle is not None
        await asyncio.sleep(0.03)
        pings = _ping_frames(transport)
        assert len(pings) == 1
        # A 4-byte token rides the PING body (header byte 0x89, len 0x04).
        assert pings[0][:2] == b"\x89\x04"
        assert len(pings[0]) == 6
        await ws.close()

    asyncio.run(go())


def test_heartbeat_pong_match_keeps_alive() -> None:
    async def go() -> None:
        ws, transport = _make_ws(heartbeat=0.02)
        # Drive the windows manually (no `await` between ticks, so the real
        # timers never fire) - deterministic, unlike sleeping across windows.
        ws._heartbeat_tick()  # window 1: sends a tokened PING, arms _hb_token
        pings = _ping_frames(transport)
        assert len(pings) == 1
        token = pings[0][2:6]
        # A matching PONG answers the probe; the token clears and the inbound
        # frame marks the peer alive for the next window.
        ws.feed_data(_client_frame(0xA, token))
        assert ws._hb_token is None
        ws._heartbeat_tick()  # window 2: peer was seen alive -> no drop
        assert not ws._closed
        assert not transport.closed
        await ws.close()

    asyncio.run(go())


def test_heartbeat_silent_peer_dropped_with_1006() -> None:
    async def go() -> None:
        ws, transport = _make_ws(heartbeat=0.02)
        ws.start_heartbeat()
        # First window sends a PING; the peer never answers, so the second
        # window faults it.
        await asyncio.sleep(0.07)
        assert ws._closed is True
        assert transport.closed is True
        assert ws.close_code == WS_1006_ABNORMAL_CLOSURE

    asyncio.run(go())


def test_heartbeat_any_inbound_defers_probe() -> None:
    async def go() -> None:
        ws, transport = _make_ws(heartbeat=0.03)
        ws.start_heartbeat()
        # Keep feeding unrelated data frames faster than the interval; no
        # PING should ever be emitted because the socket keeps proving alive.
        for _ in range(6):
            ws.feed_data(_client_frame(0x2, b"x"))
            await asyncio.sleep(0.01)
        assert _ping_frames(transport) == []
        assert not ws._closed
        await ws.close()

    asyncio.run(go())


def test_heartbeat_cancelled_on_close() -> None:
    async def go() -> None:
        ws, _ = _make_ws(heartbeat=0.02)
        ws.start_heartbeat()
        assert ws._hb_handle is not None
        await ws.close()
        assert ws._hb_handle is None

    asyncio.run(go())


def test_heartbeat_inert_in_asgi_mode() -> None:
    async def receive() -> dict:
        return {"type": "websocket.connect"}

    async def send(msg: dict) -> None:
        return None

    async def go() -> None:
        scope = {"type": "websocket", "path": "/ws", "headers": []}
        ws = WebSocket.from_asgi(scope, receive, send, heartbeat=0.01)
        assert ws._heartbeat is None
        ws.start_heartbeat()
        assert ws._hb_handle is None
        await asyncio.sleep(0.03)
        assert not ws._closed

    asyncio.run(go())


def test_start_heartbeat_idempotent() -> None:
    async def go() -> None:
        ws, _ = _make_ws(heartbeat=0.05)
        ws.start_heartbeat()
        first = ws._hb_handle
        ws.start_heartbeat()
        assert ws._hb_handle is first
        await ws.close()

    asyncio.run(go())


def test_pong_with_wrong_token_does_not_clear() -> None:
    async def go() -> None:
        ws, transport = _make_ws(heartbeat=0.02)
        ws.start_heartbeat()
        await asyncio.sleep(0.03)
        assert ws._hb_token is not None
        # A PONG whose body does not echo the outstanding token does not
        # confirm the probe (though any inbound frame still defers death via
        # the saw-inbound flag).
        ws.feed_data(_client_frame(0xA, struct.pack("!I", 0x99999999)))
        assert ws._hb_token is not None
        await ws.close()

    asyncio.run(go())


def test_heartbeat_timeout_unblocks_parked_receive() -> None:
    """A silent peer that trips the heartbeat must wake a handler parked in
    receive_*() with a 1006 disconnect, not leave it hung forever."""

    async def go() -> None:
        ws, _transport = _make_ws(heartbeat=0.02)
        ws._accepted = True
        ws.start_heartbeat()
        with pytest.raises(WebSocketDisconnect) as exc:
            await ws.receive_text()
        assert exc.value.code == WS_1006_ABNORMAL_CLOSURE
        assert ws._closed is True

    asyncio.run(go())
