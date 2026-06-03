"""Inbound TEXT UTF-8 validation and Close-frame code/reason handling.

RFC 6455 Sec. 8.1 requires TEXT payloads to be valid UTF-8; a violation is
answered with a 1007 close at the parser boundary rather than surfacing a
raw `UnicodeDecodeError` at `receive_text()` time. Sec. 5.5.1 / Sec. 7.4
define the Close-frame body (2-byte code + UTF-8 reason); an out-of-range
code or a non-UTF-8 reason is a 1002/1007 protocol error, and the peer's
code/reason become observable via `ws.close_code` / `ws.close_reason`.
"""

from __future__ import annotations

import struct

import pytest

from veloce.exceptions import WebSocketDisconnect
from veloce.websocket import WebSocket, _Utf8Validator


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


def _last_close_code(transport: _FakeTransport) -> int:
    close_frames = [w for w in transport.writes if w[0] & 0x0F == 0x8]
    assert close_frames
    return struct.unpack("!H", close_frames[-1][2:4])[0]


# ── Incremental UTF-8 validator unit ───────────────────────────────────


def test_utf8_validator_accepts_ascii_and_multibyte():
    v = _Utf8Validator()
    assert v.feed("héllo アあ".encode())
    assert v.done


def test_utf8_validator_tracks_split_codepoint_across_chunks():
    v = _Utf8Validator()
    text = "あ".encode()  # 3 bytes
    assert v.feed(text[:1])
    assert not v.done  # mid-codepoint
    assert v.feed(text[1:])
    assert v.done


def test_utf8_validator_rejects_lone_continuation_byte():
    assert _Utf8Validator().feed(b"\x80") is False


def test_utf8_validator_rejects_overlong_encoding():
    # 0xC0 0x80 is an overlong encoding of NUL.
    assert _Utf8Validator().feed(b"\xc0\x80") is False


def test_utf8_validator_rejects_surrogate_half():
    # 0xED 0xA0 0x80 = U+D800, a UTF-16 surrogate, illegal in UTF-8.
    assert _Utf8Validator().feed(b"\xed\xa0\x80") is False


def test_utf8_validator_rejects_codepoint_above_max():
    # 0xF4 0x90 ... would encode > U+10FFFF.
    assert _Utf8Validator().feed(b"\xf4\x90\x80\x80") is False


# ── TEXT frame UTF-8 validation at the parser boundary ─────────────────


async def test_invalid_utf8_text_frame_closes_with_1007():
    ws, transport = _make_ws()
    ws.feed_data(_client_frame(0x1, b"\xff\xfe", fin=True))
    assert ws._closed is True
    assert transport.closed is True
    assert _last_close_code(transport) == 1007
    # The bad bytes never reached the receive queue.
    assert ws._receive_queue.empty()


async def test_valid_utf8_text_frame_still_delivered():
    ws, _ = _make_ws()
    ws.feed_data(_client_frame(0x1, "naïve café".encode(), fin=True))
    assert ws._receive_queue.get_nowait().decode("utf-8") == "naïve café"


async def test_text_truncated_codepoint_at_fin_closes_with_1007():
    """A TEXT frame ending mid-codepoint (incomplete UTF-8 sequence) is
    invalid even though every byte so far was a legal prefix."""
    ws, transport = _make_ws()
    truncated = "あ".encode()[:2]  # first 2 of 3 bytes
    ws.feed_data(_client_frame(0x1, truncated, fin=True))
    assert ws._closed is True
    assert _last_close_code(transport) == 1007


async def test_invalid_utf8_split_across_fragments_closes_with_1007():
    """A codepoint straddling two fragments where the continuation byte is
    illegal is caught on the offending fragment, mid-message."""
    ws, transport = _make_ws()
    text = "あ".encode()  # 3 bytes
    ws.feed_data(_client_frame(0x1, text[:1], fin=False))  # opens a TEXT message
    assert not ws._closed
    # A continuation byte 0x00 is not a valid trailing byte for this lead.
    ws.feed_data(_client_frame(0x0, b"\x00", fin=True))
    assert ws._closed is True
    assert _last_close_code(transport) == 1007


async def test_valid_utf8_split_across_fragments_reassembles():
    ws, _ = _make_ws()
    text = "あい".encode()  # 6 bytes
    ws.feed_data(_client_frame(0x1, text[:1], fin=False))
    ws.feed_data(_client_frame(0x0, text[1:4], fin=False))
    ws.feed_data(_client_frame(0x0, text[4:], fin=True))
    assert ws._receive_queue.get_nowait().decode("utf-8") == "あい"


async def test_binary_frame_skips_utf8_validation():
    """Binary frames carry arbitrary bytes — no UTF-8 constraint applies."""
    ws, _ = _make_ws()
    ws.feed_data(_client_frame(0x2, b"\xff\x00\xfe", fin=True))
    assert ws._receive_queue.get_nowait() == b"\xff\x00\xfe"


# ── Close-frame code / reason validation and exposure ──────────────────


async def test_close_frame_exposes_code_and_reason():
    ws, transport = _make_ws()
    body = struct.pack("!H", 1001) + b"bye"
    with pytest.raises(WebSocketDisconnect) as exc:
        ws.feed_data(_client_frame(0x8, body, fin=True))
    assert exc.value.code == 1001
    assert ws.close_code == 1001
    assert ws.close_reason == "bye"
    # The close was echoed and the transport torn down.
    assert ws._closed is True
    assert transport.closed is True


async def test_empty_close_frame_records_no_status():
    ws, _ = _make_ws()
    with pytest.raises(WebSocketDisconnect):
        ws.feed_data(_client_frame(0x8, b"", fin=True))
    # 1005 ("no status received") is recorded but never put on the wire.
    assert ws.close_code == 1005
    assert ws.close_reason == ""


async def test_one_byte_close_frame_is_protocol_error():
    ws, transport = _make_ws()
    with pytest.raises(WebSocketDisconnect):
        ws.feed_data(_client_frame(0x8, b"\x03", fin=True))
    assert _last_close_code(transport) == 1002


async def test_reserved_close_code_is_protocol_error():
    """A peer must not send 1006 (abnormal closure) in a Close body."""
    ws, transport = _make_ws()
    with pytest.raises(WebSocketDisconnect):
        ws.feed_data(_client_frame(0x8, struct.pack("!H", 1006), fin=True))
    assert _last_close_code(transport) == 1002


async def test_unassigned_close_code_below_3000_is_protocol_error():
    ws, transport = _make_ws()
    with pytest.raises(WebSocketDisconnect):
        ws.feed_data(_client_frame(0x8, struct.pack("!H", 2222), fin=True))
    assert _last_close_code(transport) == 1002


async def test_application_close_code_above_3000_accepted():
    ws, _ = _make_ws()
    with pytest.raises(WebSocketDisconnect) as exc:
        ws.feed_data(_client_frame(0x8, struct.pack("!H", 4000), fin=True))
    assert exc.value.code == 4000
    assert ws.close_code == 4000


async def test_close_frame_with_invalid_utf8_reason_closes_with_1007():
    ws, transport = _make_ws()
    body = struct.pack("!H", 1000) + b"\xff\xfe"
    with pytest.raises(WebSocketDisconnect):
        ws.feed_data(_client_frame(0x8, body, fin=True))
    assert _last_close_code(transport) == 1007
