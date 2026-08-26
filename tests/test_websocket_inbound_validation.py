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

from tests._ws_frames import client_frame as _client_frame
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
    # The bad bytes never reached the receive queue as data; the only thing
    # queued is the terminal disconnect sentinel that wakes a parked receiver.
    from veloce.websocket import _RAW_DISCONNECT

    assert ws._receive_queue.get_nowait() is _RAW_DISCONNECT
    assert ws._receive_queue.empty()


async def test_valid_utf8_text_frame_still_delivered():
    ws, _ = _make_ws()
    ws.feed_data(_client_frame(0x1, "naïve café".encode(), fin=True))
    assert ws._receive_queue.get_nowait().decode("utf-8") == "naïve café"


# ── Client frame masking (RFC 6455 Sec. 5.1) ───────────────────────────


async def test_unmasked_client_frame_closes_with_1002():
    # RFC 6455 Sec. 5.1: a client->server frame MUST be masked; an unmasked
    # frame fails the connection with a 1002 protocol error rather than being
    # processed as a valid message.
    ws, transport = _make_ws()
    # TEXT frame, fin=1, payload "hi", mask bit cleared, no mask key, raw payload.
    ws.feed_data(bytes([0x81, 0x02]) + b"hi")
    assert ws._closed is True
    assert transport.closed is True
    assert _last_close_code(transport) == 1002
    # The unmasked payload never reached the receive queue as data.
    from veloce.websocket import _RAW_DISCONNECT

    assert ws._receive_queue.get_nowait() is _RAW_DISCONNECT


async def test_unmasked_control_frame_closes_with_1002():
    # The masking requirement applies to control frames too; an unmasked PING
    # is a protocol error, not a frame to pong.
    ws, transport = _make_ws()
    ws.feed_data(bytes([0x89, 0x00]))  # PING, fin=1, len 0, unmasked
    assert _last_close_code(transport) == 1002


async def test_masked_client_frame_still_accepted():
    # The masking guard must not reject a properly masked client frame.
    ws, _ = _make_ws()
    ws.feed_data(_client_frame(0x1, b"ok", fin=True))
    assert ws._receive_queue.get_nowait().decode("utf-8") == "ok"


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


# ── RSV-bit + stray-continuation framing (RFC 6455 §5.2 / §5.4) ─────────


def _with_rsv(frame: bytes, rsv_mask: int) -> bytes:
    """Set reserved bit(s) on an existing frame's first byte."""
    return bytes([frame[0] | rsv_mask]) + frame[1:]


async def test_rsv1_bit_set_closes_with_1002():
    ws, transport = _make_ws()
    ws.feed_data(_with_rsv(_client_frame(0x1, b"hi", fin=True), 0x40))
    assert ws._closed is True
    assert transport.closed is True
    assert _last_close_code(transport) == 1002
    # Payload never reached the application queue.
    from veloce.websocket import _RAW_DISCONNECT

    assert ws._receive_queue.get_nowait() is _RAW_DISCONNECT


@pytest.mark.parametrize("rsv_mask", [0x20, 0x10])
async def test_rsv2_and_rsv3_bits_rejected(rsv_mask):
    ws, transport = _make_ws()
    ws.feed_data(_with_rsv(_client_frame(0x2, b"\x00\x01", fin=True), rsv_mask))
    assert ws._closed is True
    assert _last_close_code(transport) == 1002


async def test_stray_continuation_without_message_closes_with_1002():
    ws, transport = _make_ws()
    ws.feed_data(_client_frame(0x0, b"orphan", fin=True))
    assert ws._closed is True
    assert _last_close_code(transport) == 1002


async def test_clean_frame_with_zero_rsv_still_delivered():
    """The 0x70 mask must not false-positive on FIN/opcode bits."""
    ws, _ = _make_ws()
    ws.feed_data(_client_frame(0x1, b"hello", fin=True))
    assert ws._receive_queue.get_nowait().decode("utf-8") == "hello"


# ── Close code preserved across the between-receives path ──────────────


async def test_close_code_preserved_on_receive_after_close():
    """A close that landed between receives must surface its recorded code on
    the next receive_*() (which hits _check_can_receive first), not a 1000."""
    ws, _ = _make_ws()
    ws._accepted = True
    # Peer close (going-away) recorded while user code processed a message.
    ws._closed = True
    ws.close_code = 1001
    with pytest.raises(WebSocketDisconnect) as exc:
        await ws.receive_text()
    assert exc.value.code == 1001


async def test_close_frame_with_invalid_utf8_reason_closes_with_1007():
    ws, transport = _make_ws()
    body = struct.pack("!H", 1000) + b"\xff\xfe"
    with pytest.raises(WebSocketDisconnect):
        ws.feed_data(_client_frame(0x8, body, fin=True))
    assert _last_close_code(transport) == 1007


@pytest.mark.parametrize("code", [1012, 1013, 1014])
async def test_registered_close_codes_1012_1014_accepted(code):
    """1012 (Service Restart), 1013 (Try Again Later), 1014 (Bad Gateway) are
    registered peer close codes - surface them, do not answer with 1002."""
    ws, _ = _make_ws()
    with pytest.raises(WebSocketDisconnect) as exc:
        ws.feed_data(_client_frame(0x8, struct.pack("!H", code), fin=True))
    assert exc.value.code == code
    assert ws.close_code == code


async def test_close_code_above_4999_is_protocol_error():
    """RFC 6455 Sec. 7.4.2: only 3000-4999 are valid in the private range;
    a code above 4999 (e.g. 5000) is a protocol error answered with 1002."""
    ws, transport = _make_ws()
    with pytest.raises(WebSocketDisconnect):
        ws.feed_data(_client_frame(0x8, struct.pack("!H", 5000), fin=True))
    assert _last_close_code(transport) == 1002
