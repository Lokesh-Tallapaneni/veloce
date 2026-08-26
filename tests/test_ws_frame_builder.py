"""The shared client-frame builder produces frames the server accepts.

Six modules each carried a `_client_frame`, and one had only the 7-bit length
branch - so for a payload of 126 bytes or more it produced a corrupt header
(`0x80 | n` overflows the length field into the mask bit) rather than the 16-bit
extension RFC 6455 Sec. 5.2 requires. It was wrong in a way nothing caught,
because that module never sent a payload that large.

These tests cover the size boundaries the drifted copy got wrong, and check the
frames against the real parser rather than against a hand-computed byte string -
a builder tested only against its own expected bytes can be wrong in exactly the
way the parser cares about.
"""

from __future__ import annotations

import pytest

from tests._native_ws import delivered, mark_accepted
from tests._ws_frames import client_frame
from veloce.websocket import WebSocket

_KEY = {"sec-websocket-key": "dGhlIHNhbXBsZSBub25jZQ=="}


class _Transport:
    def __init__(self) -> None:
        self.writes: list[bytes] = []

    def write(self, data: bytes) -> None:
        self.writes.append(bytes(data))

    def writelines(self, buffers) -> None:
        self.writes.append(b"".join(bytes(b) for b in buffers))

    def close(self) -> None:
        pass

    def is_closing(self) -> bool:
        return False


def _fed(payload: bytes) -> bytes:
    """Feed a binary frame through the real parser and return what it delivered."""
    ws = mark_accepted(WebSocket(_Transport(), dict(_KEY)))
    ws.feed_data(client_frame(0x2, payload))
    queue = ws._receive_queue
    assert queue is not None and not queue.empty(), "the parser delivered no message"
    return queue.get_nowait()


# ── the size boundaries the drifted copy got wrong ───────────────────


@pytest.mark.parametrize("size", [0, 1, 125, 126, 127, 200, 65535, 65536, 70000])
def test_a_payload_of_any_size_round_trips_through_the_parser(size):
    payload = bytes(i % 251 for i in range(size))
    assert _fed(payload) == payload


@pytest.mark.parametrize("size", [126, 200, 65535])
def test_a_medium_payload_uses_the_16_bit_length(size):
    """The branch the short-only copy lacked."""
    frame = client_frame(0x2, b"x" * size)
    assert frame[1] & 0x7F == 126


@pytest.mark.parametrize("size", [65536, 70000])
def test_a_large_payload_uses_the_64_bit_length(size):
    frame = client_frame(0x2, b"x" * size)
    assert frame[1] & 0x7F == 127


@pytest.mark.parametrize("size", [0, 1, 125])
def test_a_short_payload_uses_the_7_bit_length(size):
    frame = client_frame(0x2, b"x" * size)
    assert frame[1] & 0x7F == size


def test_the_short_only_form_would_corrupt_a_126_byte_frame():
    """What the drifted copy produced, stated so the fix is not mistaken for
    cosmetics: `0x80 | 126` is `0xFE`, which the parser reads as the 16-bit
    length marker - so the mask and payload are then misread as the length."""
    short_only = bytes([0x82, 0x80 | 126])
    assert short_only[1] & 0x7F == 126
    correct = client_frame(0x2, b"x" * 126)
    assert correct[:2] == short_only
    # The correct frame carries the two extra length bytes the other omits.
    assert len(correct) == 2 + 2 + 4 + 126


# ── and the frame is a valid client frame ────────────────────────────


def test_every_frame_is_masked():
    """RFC 6455 Sec. 5.1: a client-to-server frame MUST be masked."""
    for size in (0, 125, 126, 65536):
        assert client_frame(0x2, b"x" * size)[1] & 0x80


def test_the_fin_bit_is_settable():
    assert client_frame(0x2, b"x", fin=True)[0] & 0x80
    assert not client_frame(0x2, b"x", fin=False)[0] & 0x80


def test_the_opcode_reaches_the_frame():
    for opcode in (0x1, 0x2, 0x8, 0x9, 0xA):
        assert client_frame(opcode, b"")[0] & 0x0F == opcode


def test_a_custom_mask_is_used():
    frame = client_frame(0x2, b"AAAA", mask=b"\x01\x01\x01\x01")
    assert frame[2:6] == b"\x01\x01\x01\x01"
    assert frame[6:] == bytes(b ^ 1 for b in b"AAAA")


def test_a_text_frame_carries_its_utf8_bytes():
    """The parser queues the raw payload; decoding is `receive_text`'s job. A
    multi-byte character is used so a length computed in characters rather than
    bytes would produce a short frame and fail here."""
    ws = mark_accepted(WebSocket(_Transport(), dict(_KEY)))
    text = "héllo"
    ws.feed_data(client_frame(0x1, text.encode("utf-8")))
    assert delivered(ws)[0] == text.encode("utf-8")
