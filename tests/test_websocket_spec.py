"""WebSocket spec-compliance tests (W2/W5/W6)."""

from __future__ import annotations

import struct

import orjson
import pytest

from tests._native_ws import delivered, mark_accepted, nothing_delivered
from tests._ws_frames import client_frame as _client_frame
from veloce.websocket import WebSocket


class _FakeTransport:
    """Minimal asyncio.Transport stand-in for WebSocket tests."""

    def __init__(self) -> None:
        self.writes: list[bytes] = []
        self.closed = False

    def write(self, data: bytes) -> None:
        self.writes.append(data)

    def close(self) -> None:
        self.closed = True


def _make_ws() -> tuple[WebSocket, _FakeTransport]:
    transport = _FakeTransport()
    # The handshake reads Sec-WebSocket-Key; a non-empty value is enough.
    ws = WebSocket(transport, {"sec-websocket-key": "dGhlIHNhbXBsZSBub25jZQ=="})
    return ws, transport


# ── W2: accept(subprotocol=, headers=) ─────────────────────────────────


async def test_accept_writes_basic_handshake():
    ws, transport = _make_ws()
    await ws.accept()
    assert ws._accepted is True
    response = transport.writes[0].decode()
    assert "101 Switching Protocols" in response
    assert "Upgrade: websocket" in response
    assert "Sec-WebSocket-Accept:" in response


async def test_accept_echoes_subprotocol_when_set():
    ws, transport = _make_ws()
    await ws.accept(subprotocol="graphql-ws")
    response = transport.writes[0].decode()
    assert "Sec-WebSocket-Protocol: graphql-ws" in response


async def test_accept_emits_extra_response_headers():
    ws, transport = _make_ws()
    await ws.accept(headers={"X-Custom": "v", "X-Other": "w"})
    response = transport.writes[0].decode()
    assert "X-Custom: v" in response
    assert "X-Other: w" in response


async def test_accept_omits_subprotocol_when_not_provided():
    """No `subprotocol=` arg → no `Sec-WebSocket-Protocol` line."""
    ws, transport = _make_ws()
    await ws.accept()
    assert "Sec-WebSocket-Protocol" not in transport.writes[0].decode()


# ── W5: send_json(mode="text"|"binary") ──────────────────────────────


async def test_send_json_default_mode_is_text():
    ws, transport = _make_ws()
    await ws.accept()
    transport.writes.clear()
    await ws.send_json({"a": 1})
    frame = transport.writes[0]
    # First byte: 0x81 = FIN + opcode 0x1 (text).
    assert frame[0] == 0x81


async def test_send_json_binary_mode_uses_binary_frame():
    ws, transport = _make_ws()
    await ws.accept()
    transport.writes.clear()
    await ws.send_json({"a": 1}, mode="binary")
    frame = transport.writes[0]
    # First byte: 0x82 = FIN + opcode 0x2 (binary).
    assert frame[0] == 0x82


async def test_send_json_invalid_mode_rejected():
    ws, _ = _make_ws()
    await ws.accept()
    with pytest.raises(ValueError):
        await ws.send_json({"a": 1}, mode="bogus")


async def test_send_json_roundtrip_payload():
    ws, transport = _make_ws()
    await ws.accept()
    transport.writes.clear()
    await ws.send_json({"k": "v"})
    frame = transport.writes[0]
    payload = frame[2 : 2 + frame[1]]
    assert orjson.loads(payload) == {"k": "v"}


# ── W6: close(code, reason) ────────────────────────────────────────────


async def test_close_without_reason_emits_2byte_payload():
    ws, transport = _make_ws()
    await ws.accept()
    transport.writes.clear()
    await ws.close(code=1000)
    frame = transport.writes[0]
    # Frame: FIN+0x8 close opcode, length=2, then BE uint16 code.
    assert frame[0] == 0x88
    assert frame[1] == 2
    assert struct.unpack("!H", frame[2:4])[0] == 1000
    assert transport.closed is True


async def test_close_with_short_reason():
    ws, transport = _make_ws()
    await ws.accept()
    transport.writes.clear()
    await ws.close(code=1008, reason="policy")
    frame = transport.writes[0]
    assert frame[0] == 0x88
    # Length = 2 (code) + 6 (reason).
    assert frame[1] == 8
    assert struct.unpack("!H", frame[2:4])[0] == 1008
    assert frame[4:10] == b"policy"


async def test_close_truncates_long_reason_to_123_bytes():
    """Reason longer than 123 bytes must be truncated (per RFC 6455 §5.5.1)."""
    ws, transport = _make_ws()
    await ws.accept()
    transport.writes.clear()
    long_reason = "x" * 200
    await ws.close(code=1000, reason=long_reason)
    frame = transport.writes[0]
    # Payload = 2 (code) + 123 (reason cap) = 125 bytes.
    assert frame[1] == 125


async def test_close_truncates_at_utf8_boundary():
    """A truncated reason must not break in the middle of a codepoint."""
    ws, transport = _make_ws()
    await ws.accept()
    transport.writes.clear()
    # "あ" is 3 UTF-8 bytes. Make a string that lands the 123-byte cut
    # mid-codepoint and verify we walk back to a clean boundary.
    reason = "あ" * 50  # 150 bytes
    await ws.close(code=1000, reason=reason)
    frame = transport.writes[0]
    # The reason segment (bytes after the 2-byte code) must decode cleanly.
    reason_bytes = bytes(frame[4 : 2 + frame[1]])
    # Should not raise.
    reason_decoded = reason_bytes.decode("utf-8")
    assert all(c == "あ" for c in reason_decoded)


async def test_close_idempotent():
    """Calling close twice does not write a second close frame."""
    ws, transport = _make_ws()
    await ws.accept()
    transport.writes.clear()
    await ws.close(code=1000)
    n_writes_after_first = len(transport.writes)
    await ws.close(code=1001)
    assert len(transport.writes) == n_writes_after_first


async def test_websocket_send_before_accept_raises():

    ws = WebSocket(transport=None, headers={})
    with pytest.raises(RuntimeError, match="accept"):
        await ws.send_text("hello")


async def test_websocket_double_accept_raises():

    ws = mark_accepted(WebSocket(transport=None, headers={}))
    with pytest.raises(RuntimeError, match="already accepted"):
        await ws.accept()


async def test_websocket_accept_rejects_crlf_in_custom_header():
    ws = WebSocket(transport=None, headers={})
    with pytest.raises(ValueError):
        await ws.accept(headers={"X-Evil": "a\r\nInjected: 1"})


async def test_websocket_raw_send_before_accept_raises():
    """The raw `send()` escape hatch enforces accept-before-send too —
    not just the typed send_text / send_bytes helpers."""
    ws = WebSocket(transport=None, headers={})
    with pytest.raises(RuntimeError, match="accept"):
        await ws.send({"type": "websocket.send", "text": "hi"})


async def test_websocket_raw_send_after_close_raises():
    """`send()` after the connection is closed raises WebSocketDisconnect,
    matching send_text / send_bytes."""
    from veloce.exceptions import WebSocketDisconnect

    ws, _ = _make_ws()
    await ws.accept()
    await ws.close(code=1000)
    with pytest.raises(WebSocketDisconnect):
        await ws.send({"type": "websocket.send", "text": "late"})


# ── R4: fragmented-message reassembly ──────────────────────────────────


async def test_websocket_unfragmented_frame_still_delivered():
    """A single FIN data frame is delivered as before."""
    ws, _ = _make_ws()
    ws.feed_data(_client_frame(0x1, b"single", fin=True))
    assert delivered(ws)[0] == b"single"


async def test_websocket_reassembles_fragmented_message():
    """A message split across a start frame + continuation frames is
    reassembled into one delivered message."""
    ws, _ = _make_ws()
    ws.feed_data(_client_frame(0x1, b"hello ", fin=False))  # start (text)
    ws.feed_data(_client_frame(0x0, b"wonder", fin=False))  # continuation
    ws.feed_data(_client_frame(0x0, b"ful", fin=True))  # final fragment
    assert delivered(ws)[0] == b"hello wonderful"
    assert nothing_delivered(ws)  # only one message delivered


async def test_websocket_control_frame_interleaved_in_fragmented_message():
    """A ping between fragments is answered with a pong without disturbing
    the in-progress reassembly buffer (RFC 6455 §5.4)."""
    ws, transport = _make_ws()
    ws.feed_data(_client_frame(0x2, b"AAAA", fin=False))  # start (binary)
    ws.feed_data(_client_frame(0x9, b"pp", fin=True))  # ping mid-stream
    ws.feed_data(_client_frame(0x0, b"BBBB", fin=True))  # final fragment

    # The ping was answered — a pong frame (FIN + opcode 0xA = 0x8A).
    assert any(w[0] == 0x8A for w in transport.writes)
    # The fragmented message reassembled across the interleaved ping.
    assert delivered(ws)[0] == b"AAAABBBB"


async def test_websocket_stray_continuation_frame_is_protocol_error():
    """A continuation frame with no message in progress is a protocol error
    (RFC 6455 §5.4) - the connection closes with 1002."""
    ws, transport = _make_ws()
    ws.feed_data(_client_frame(0x0, b"orphan", fin=True))
    assert ws._closed is True
    close = [w for w in transport.writes if w[0] & 0x0F == 0x8]
    assert close and struct.unpack("!H", close[-1][2:4])[0] == 1002


async def test_websocket_data_frame_mid_fragmentation_is_protocol_error():
    """A data frame arriving while a fragmented message is in progress is a
    protocol error (RFC 6455 §5.4) - only continuation frames may follow the
    opening frame, so the connection fails with 1002 and nothing is delivered."""
    ws, transport = _make_ws()
    from veloce.websocket import _RAW_DISCONNECT

    ws.feed_data(_client_frame(0x1, b"abandoned-", fin=False))  # opens a fragment
    ws.feed_data(_client_frame(0x1, b"interrupt", fin=True))  # new data frame mid-stream
    assert ws._closed is True
    # The interrupting frame is not delivered: the only thing enqueued is the
    # disconnect sentinel that wakes a parked receiver on the protocol close.
    assert delivered(ws)[0] is _RAW_DISCONNECT
    assert nothing_delivered(ws)
    close = [w for w in transport.writes if w[0] & 0x0F == 0x8]
    assert close and struct.unpack("!H", close[-1][2:4])[0] == 1002


# ── C3 — receive-side state-machine guards ─────────────────────────


def test_receive_text_before_accept_raises():
    """Calling `receive_text` before `accept()` is a programming error
    — without the guard the caller hung on the empty queue forever."""
    import asyncio

    async def go() -> None:
        ws = WebSocket(_FakeTransport(), {})
        with pytest.raises(RuntimeError, match="call accept"):
            await ws.receive_text(timeout=0.01)

    asyncio.run(go())


def test_receive_bytes_before_accept_raises():
    import asyncio

    async def go() -> None:
        ws = WebSocket(_FakeTransport(), {})
        with pytest.raises(RuntimeError, match="call accept"):
            await ws.receive_bytes(timeout=0.01)

    asyncio.run(go())


def test_receive_json_before_accept_raises():
    """`receive_json` routes through `receive_text`, so it inherits the
    guard — pin so a future refactor cannot regress it."""
    import asyncio

    async def go() -> None:
        ws = WebSocket(_FakeTransport(), {})
        with pytest.raises(RuntimeError, match="call accept"):
            await ws.receive_json(timeout=0.01)

    asyncio.run(go())


def test_raw_receive_before_accept_raises():
    """The raw ASGI `receive()` escape hatch must enforce the same
    handshake state machine as the typed `receive_*` helpers — otherwise
    it consumes the `websocket.connect` envelope and corrupts the next
    `accept()`. Symmetric with the existing `WebSocket.send()` guard."""
    import asyncio

    async def go() -> None:
        # Build an ASGI-mode WebSocket so `receive()` is in-scope.
        async def fake_recv() -> dict:
            return {"type": "websocket.connect"}

        async def fake_send(message: dict) -> None:
            return None

        scope = {"type": "websocket", "headers": []}
        ws = WebSocket.from_asgi(scope, fake_recv, fake_send)
        with pytest.raises(RuntimeError, match="call accept"):
            await ws.receive()

    asyncio.run(go())


def test_receive_after_close_raises_disconnect():
    """A receive after the application closed the connection is a
    `WebSocketDisconnect`, matching the `send_*` close-state behaviour."""
    import asyncio

    from veloce.exceptions import WebSocketDisconnect

    async def go() -> None:
        ws = mark_accepted(WebSocket(_FakeTransport(), {}))
        ws._closed = True
        with pytest.raises(WebSocketDisconnect):
            await ws.receive_text(timeout=0.01)

    asyncio.run(go())
