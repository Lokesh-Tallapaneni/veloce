"""Property-based fuzz tests for the WebSocket frame parser — `websocket.py`.

Drives `WebSocket.feed_data` with arbitrary byte runs, split frames, and
corrupted-but-valid frames. The parser must buffer, consume, or trigger a
controlled close — never raise an unhandled error or park unbounded bytes in
its receive buffer (an over-allocation / DoS bug).
"""

from __future__ import annotations

import contextlib
import struct

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from veloce import WebSocket
from veloce.exceptions import WebSocketDisconnect

pytestmark = pytest.mark.fuzz


class _NullTransport:
    """Transport that swallows the parser's outgoing frames.

    `_parse_frame` answers pings with a pong and closes on a too-big /
    malformed frame, both of which write to the transport. A live socket is
    irrelevant to parser robustness, so the writes go nowhere.
    """

    def write(self, data: bytes) -> None:
        pass

    def writelines(self, data) -> None:
        pass

    def close(self) -> None:
        pass


def _raw_websocket() -> WebSocket:
    """Build a raw-mode WebSocket whose frame parser can run headlessly."""
    return WebSocket(_NullTransport(), headers={})


def _feed(ws: WebSocket, chunk: bytes) -> None:
    """Drive `feed_data`, absorbing the controlled close a 0x8 frame raises."""
    with contextlib.suppress(WebSocketDisconnect):
        ws.feed_data(chunk)


def _client_data_frame(payload: bytes, mask: bytes, opcode: int) -> bytes:
    """Encode a FIN=1 masked client data frame (RFC 6455 §5.2)."""
    n = len(payload)
    header = bytearray()
    header.append(0x80 | opcode)
    if n < 126:
        header.append(0x80 | n)
    elif n < 65536:
        header.append(0x80 | 126)
        header.extend(struct.pack("!H", n))
    else:
        header.append(0x80 | 127)
        header.extend(struct.pack("!Q", n))
    header.extend(mask)
    masked = bytes(b ^ mask[i & 3] for i, b in enumerate(payload))
    return bytes(header) + masked


@settings(max_examples=300, deadline=None)
@given(
    data=st.binary(max_size=400),
    steps=st.lists(st.integers(min_value=1, max_value=7), min_size=1, max_size=80),
)
def test_arbitrary_bytes_never_crash(data: bytes, steps: list[int]) -> None:
    """Arbitrary byte runs fed in random chunks never raise an unhandled error.

    The receive buffer must never retain more than the bytes fed — a runaway
    buffer is an over-allocation bug.
    """
    ws = _raw_websocket()
    pos = 0
    step_iter = iter(steps)
    while pos < len(data) and not ws._closed:
        try:
            step = next(step_iter)
        except StopIteration:
            step = 1
        _feed(ws, data[pos : pos + step])
        pos += step
    assert len(ws._recv_buffer) <= len(data)


@settings(max_examples=200, deadline=None)
@given(
    payload=st.binary(max_size=130),
    mask=st.binary(min_size=4, max_size=4),
    steps=st.lists(st.integers(min_value=1, max_value=5), min_size=1, max_size=60),
)
def test_split_binary_frame_reassembles(payload: bytes, mask: bytes, steps: list[int]) -> None:
    """A well-formed binary frame fed in random splits reassembles cleanly.

    A binary frame carries arbitrary octets with no UTF-8 constraint
    (RFC 6455 §5.6), so the random payload exercises the framing-level
    partial-read paths without tripping the TEXT UTF-8 check.
    """
    frame = _client_data_frame(payload, mask, 0x2)
    ws = _raw_websocket()
    pos = 0
    step_iter = iter(steps)
    while pos < len(frame):
        try:
            step = next(step_iter)
        except StopIteration:
            step = 1
        _feed(ws, frame[pos : pos + step])
        pos += step
    assert not ws._closed
    assert ws._recv_buffer == bytearray()
    assert ws._receive_queue.get_nowait() == payload


@settings(max_examples=300, deadline=None)
@given(
    positions=st.lists(st.integers(min_value=0), max_size=5),
    replacements=st.lists(st.integers(min_value=0, max_value=255), max_size=5),
)
def test_corrupted_valid_frame_never_over_allocates(
    positions: list[int], replacements: list[int]
) -> None:
    """Flipping random bytes in a valid frame yields only controlled outcomes.

    A declared 64-bit length past `MAX_FRAME_SIZE` must trip the controlled
    close, never park unbounded bytes in the buffer.
    """
    base = _client_data_frame(b"the quick brown fox", b"\x01\x02\x03\x04", 0x1)
    corrupted = bytearray(base)
    for pos, rep in zip(positions, replacements):
        corrupted[pos % len(corrupted)] = rep
    ws = _raw_websocket()
    _feed(ws, bytes(corrupted))
    assert len(ws._recv_buffer) <= len(corrupted)


def test_inflated_length_closes_not_allocates() -> None:
    """A 64-bit length beyond `MAX_FRAME_SIZE` closes rather than buffering."""
    header = bytes([0x82, 127]) + struct.pack("!Q", WebSocket.MAX_FRAME_SIZE + 1)
    ws = _raw_websocket()
    _feed(ws, header)
    assert ws._closed
    assert len(ws._recv_buffer) <= len(header)
