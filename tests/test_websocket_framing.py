"""Incremental WebSocket frame parsing across transport reads.

The raw-transport reader buffers bytes and parses whole frames off the
front, so a frame split across two transport reads is reassembled and
several frames in one read all parse (RFC 6455 §5).
"""

from __future__ import annotations

import struct

import pytest

from veloce.exceptions import WebSocketDisconnect
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
    return WebSocket(transport, {"sec-websocket-key": "dGhlIHNhbXBsZSBub25jZQ=="}), transport


def _client_frame(opcode: int, payload: bytes, fin: bool = True) -> bytes:
    """Build one masked client→server WebSocket frame (RFC 6455 §5)."""
    mask = b"\x12\x34\x56\x78"
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    b0 = (0x80 if fin else 0x00) | opcode
    n = len(payload)
    if n < 126:
        header = bytes([b0, 0x80 | n])
    elif n < 65536:
        header = bytes([b0, 0x80 | 126]) + struct.pack("!H", n)
    else:
        header = bytes([b0, 0x80 | 127]) + struct.pack("!Q", n)
    return header + mask + masked


async def test_frame_split_across_two_feed_data_chunks():
    """A single frame whose header lands in the first chunk and whose
    payload is split across both chunks parses as one message."""
    ws, _ = _make_ws()
    frame = _client_frame(0x1, b"split-across-reads", fin=True)
    # Header (2) + mask (4) + the first few payload bytes in chunk one;
    # the rest of the payload in chunk two.
    cut = 9
    ws.feed_data(frame[:cut])
    assert ws._receive_queue.empty()  # incomplete — nothing delivered yet
    ws.feed_data(frame[cut:])
    assert ws._receive_queue.get_nowait() == b"split-across-reads"
    assert ws._receive_queue.empty()


async def test_two_frames_in_one_feed_data_call():
    """Two whole frames delivered in a single feed_data both parse."""
    ws, _ = _make_ws()
    payload = _client_frame(0x1, b"first") + _client_frame(0x2, b"second")
    ws.feed_data(payload)
    assert ws._receive_queue.get_nowait() == b"first"
    assert ws._receive_queue.get_nowait() == b"second"
    assert ws._receive_queue.empty()


async def test_fragmented_message_reassembled_across_chunks():
    """A fragmented text message (start + continuation frames) delivered
    one frame per feed_data reassembles into one message."""
    ws, _ = _make_ws()
    ws.feed_data(_client_frame(0x1, b"frag-", fin=False))
    ws.feed_data(_client_frame(0x0, b"men", fin=False))
    ws.feed_data(_client_frame(0x0, b"ted", fin=True))
    assert ws._receive_queue.get_nowait() == b"frag-mented"
    assert ws._receive_queue.empty()


async def test_ping_interleaved_between_fragments_in_one_chunk():
    """A ping frame sitting between two fragments of a data message — all
    in one feed_data — is answered with a pong and does not corrupt the
    reassembled data message (RFC 6455 §5.4)."""
    ws, transport = _make_ws()
    blob = (
        _client_frame(0x2, b"AAAA", fin=False)  # opening data fragment
        + _client_frame(0x9, b"hi", fin=True)  # interleaved ping
        + _client_frame(0x0, b"BBBB", fin=True)  # final data fragment
    )
    ws.feed_data(blob)
    # Ping answered with a pong (FIN + opcode 0xA → first byte 0x8A).
    assert any(w[0] == 0x8A for w in transport.writes)
    # The fragmented message reassembled cleanly around the ping.
    assert ws._receive_queue.get_nowait() == b"AAAABBBB"
    assert ws._receive_queue.empty()


async def test_close_frame_split_across_chunks_still_disconnects():
    """A close frame split across two reads raises WebSocketDisconnect
    only once the whole frame is buffered, not on the partial."""
    ws, _ = _make_ws()
    frame = _client_frame(0x8, struct.pack("!H", 1000), fin=True)
    ws.feed_data(frame[:3])  # partial — must not disconnect yet
    with pytest.raises(WebSocketDisconnect):
        ws.feed_data(frame[3:])
    assert ws._closed is True


async def test_oversized_declared_frame_closes_with_1009():
    """A header declaring a payload past MAX_FRAME_SIZE closes the
    connection with 1009 rather than parking unbounded bytes."""
    ws, transport = _make_ws()
    # 64-bit length far beyond the cap, masked, with no payload supplied.
    huge = ws.MAX_FRAME_SIZE + 1
    header = bytes([0x82, 0x80 | 127]) + struct.pack("!Q", huge) + b"\x00\x00\x00\x00"
    ws.feed_data(header)
    assert ws._closed is True
    assert transport.closed is True
    # A close frame (opcode 0x8) carrying code 1009 went out.
    close_frames = [w for w in transport.writes if w[0] & 0x0F == 0x8]
    assert close_frames
    assert struct.unpack("!H", close_frames[-1][2:4])[0] == 1009


async def test_fragmented_message_past_cap_closes_with_1009():
    """A fragmented message whose continuation frames push the reassembly
    buffer past MAX_MESSAGE_SIZE closes with 1009 instead of growing the
    buffer without limit (each frame is individually under the cap)."""
    ws, transport = _make_ws()
    ws.MAX_MESSAGE_SIZE = 4096  # shrink the cap so the test stays small
    chunk = b"x" * 1024
    ws.feed_data(_client_frame(0x1, chunk, fin=False))  # opening fragment
    # Stream continuation frames until the cumulative size crosses the cap.
    for _ in range(8):
        if ws._closed:
            break
        ws.feed_data(_client_frame(0x0, chunk, fin=False))
    assert ws._closed is True
    assert transport.closed is True
    close_frames = [w for w in transport.writes if w[0] & 0x0F == 0x8]
    assert close_frames
    assert struct.unpack("!H", close_frames[-1][2:4])[0] == 1009


async def test_oversized_control_frame_closes_with_1002():
    """A ping carrying more than 125 bytes is a protocol error (RFC 6455
    §5.5) — the parser closes with 1002 rather than echoing a giant pong."""
    ws, transport = _make_ws()
    ws.feed_data(_client_frame(0x9, b"p" * 126, fin=True))
    assert ws._closed is True
    assert transport.closed is True
    close_frames = [w for w in transport.writes if w[0] & 0x0F == 0x8]
    assert close_frames
    assert struct.unpack("!H", close_frames[-1][2:4])[0] == 1002
    # No pong was emitted for the malformed ping.
    assert not any(w[0] == 0x8A for w in transport.writes)


async def test_fragmented_control_frame_closes_with_1002():
    """A control frame with FIN=0 is forbidden (RFC 6455 §5.5) — the
    parser closes with 1002 rather than treating it as valid."""
    ws, transport = _make_ws()
    ws.feed_data(_client_frame(0x9, b"hi", fin=False))  # fragmented ping
    assert ws._closed is True
    assert transport.closed is True
    close_frames = [w for w in transport.writes if w[0] & 0x0F == 0x8]
    assert close_frames
    assert struct.unpack("!H", close_frames[-1][2:4])[0] == 1002
    assert not any(w[0] == 0x8A for w in transport.writes)
