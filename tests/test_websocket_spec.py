"""WebSocket spec-compliance tests (W2/W5/W6)."""

from __future__ import annotations

import struct

import orjson
import pytest

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


@pytest.mark.asyncio
async def test_accept_writes_basic_handshake():
    ws, transport = _make_ws()
    await ws.accept()
    assert ws._accepted is True
    response = transport.writes[0].decode()
    assert "101 Switching Protocols" in response
    assert "Upgrade: websocket" in response
    assert "Sec-WebSocket-Accept:" in response


@pytest.mark.asyncio
async def test_accept_echoes_subprotocol_when_set():
    ws, transport = _make_ws()
    await ws.accept(subprotocol="graphql-ws")
    response = transport.writes[0].decode()
    assert "Sec-WebSocket-Protocol: graphql-ws" in response


@pytest.mark.asyncio
async def test_accept_emits_extra_response_headers():
    ws, transport = _make_ws()
    await ws.accept(headers={"X-Custom": "v", "X-Other": "w"})
    response = transport.writes[0].decode()
    assert "X-Custom: v" in response
    assert "X-Other: w" in response


@pytest.mark.asyncio
async def test_accept_omits_subprotocol_when_not_provided():
    """No `subprotocol=` arg → no `Sec-WebSocket-Protocol` line."""
    ws, transport = _make_ws()
    await ws.accept()
    assert "Sec-WebSocket-Protocol" not in transport.writes[0].decode()


# ── W5: send_json(mode="text"|"binary") ──────────────────────────────


@pytest.mark.asyncio
async def test_send_json_default_mode_is_text():
    ws, transport = _make_ws()
    await ws.accept()
    transport.writes.clear()
    await ws.send_json({"a": 1})
    frame = transport.writes[0]
    # First byte: 0x81 = FIN + opcode 0x1 (text).
    assert frame[0] == 0x81


@pytest.mark.asyncio
async def test_send_json_binary_mode_uses_binary_frame():
    ws, transport = _make_ws()
    await ws.accept()
    transport.writes.clear()
    await ws.send_json({"a": 1}, mode="binary")
    frame = transport.writes[0]
    # First byte: 0x82 = FIN + opcode 0x2 (binary).
    assert frame[0] == 0x82


@pytest.mark.asyncio
async def test_send_json_invalid_mode_rejected():
    ws, _ = _make_ws()
    await ws.accept()
    with pytest.raises(ValueError):
        await ws.send_json({"a": 1}, mode="bogus")


@pytest.mark.asyncio
async def test_send_json_roundtrip_payload():
    ws, transport = _make_ws()
    await ws.accept()
    transport.writes.clear()
    await ws.send_json({"k": "v"})
    frame = transport.writes[0]
    payload = frame[2 : 2 + frame[1]]
    assert orjson.loads(payload) == {"k": "v"}


# ── W6: close(code, reason) ────────────────────────────────────────────


@pytest.mark.asyncio
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


@pytest.mark.asyncio
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


@pytest.mark.asyncio
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


@pytest.mark.asyncio
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


@pytest.mark.asyncio
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
    from veloce.websocket import WebSocket

    ws = WebSocket(transport=None, headers={})
    with pytest.raises(RuntimeError, match="accept"):
        await ws.send_text("hello")


async def test_websocket_double_accept_raises():
    from veloce.websocket import WebSocket

    ws = WebSocket(transport=None, headers={})
    ws._accepted = True
    with pytest.raises(RuntimeError, match="already accepted"):
        await ws.accept()


async def test_websocket_accept_rejects_crlf_in_custom_header():
    ws = WebSocket(transport=None, headers={})
    with pytest.raises(ValueError):
        await ws.accept(headers={"X-Evil": "a\r\nInjected: 1"})
