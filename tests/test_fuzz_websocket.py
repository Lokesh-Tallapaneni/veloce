"""Property-based fuzz tests for the WebSocket frame parser — `websocket.py`.

Drives `WebSocket.feed_data` with arbitrary byte runs, split frames, and
corrupted-but-valid frames. The parser must buffer, consume, or trigger a
controlled close — never raise an unhandled error or park unbounded bytes in
its receive buffer (an over-allocation / DoS bug).
"""

from __future__ import annotations

import contextlib
import itertools
import struct

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from tests._native_ws import buffered_bytes, delivered
from veloce import WebSocket, WebSocketState
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


def _feed_in_chunks(
    ws: WebSocket, data: bytes, steps: list[int], *, stop_when_closed: bool = False
) -> None:
    """Feed `data` in `steps`-sized chunks, repeating 1 once `steps` runs out."""
    pos = 0
    for step in itertools.chain(steps, itertools.repeat(1)):
        if pos >= len(data) or (stop_when_closed and _is_closed(ws)):
            return
        _feed(ws, data[pos : pos + step])
        pos += step


def _is_closed(ws: WebSocket) -> bool:
    """Closedness through the public property rather than the private flag."""
    return ws.application_state is WebSocketState.DISCONNECTED


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
    _feed_in_chunks(ws, data, steps, stop_when_closed=True)
    assert buffered_bytes(ws) <= len(data)


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
    _feed_in_chunks(ws, frame, steps)
    assert not _is_closed(ws)
    assert buffered_bytes(ws) == 0
    assert delivered(ws)[0] == payload


@settings(max_examples=300, deadline=None)
@given(
    edits=st.lists(
        st.tuples(st.integers(min_value=0), st.integers(min_value=0, max_value=255)),
        min_size=1,
        max_size=5,
    ),
)
def test_corrupted_valid_frame_never_over_allocates(edits: list[tuple[int, int]]) -> None:
    """Flipping random bytes in a valid frame never parks more than it was fed.

    The buffer bound is what this reaches. The docstring used to promise that a
    declared 64-bit length past `MAX_FRAME_SIZE` trips the controlled close,
    which these edits essentially cannot produce - the base frame declares a
    19-byte payload, so an edit would have to land on the length byte *and*
    leave a huge value behind it. `test_inflated_length_closes_not_allocates`
    below builds that frame directly and asserts the close.

    The conditional assertion is kept for the case the strategy does reach it.
    """
    base = _client_data_frame(b"the quick brown fox", b"\x01\x02\x03\x04", 0x1)
    corrupted = bytearray(base)
    # One draw of pairs; see the note in `test_fuzz_signing.py`.
    for pos, rep in edits:
        corrupted[pos % len(corrupted)] = rep
    ws = _raw_websocket()
    _feed(ws, bytes(corrupted))
    assert buffered_bytes(ws) <= len(corrupted)
    # The docstring's actual promise. A buffer bound alone is satisfied by a
    # frame that parked the bytes and stayed open, which is the outcome this
    # exists to rule out: an over-long declared length must have *closed*.
    declared = _declared_payload_length(bytes(corrupted))
    if declared is not None and declared > WebSocket.MAX_FRAME_SIZE:
        assert _is_closed(ws), (
            f"a declared length of {declared} exceeds MAX_FRAME_SIZE and the "
            "connection is still open"
        )


def _declared_payload_length(frame: bytes) -> int | None:
    """The payload length a frame header declares, or `None` if it is truncated.

    Read from the frame rather than assumed, because the fuzzer's edits may
    land on the length bytes - which is the case worth checking.
    """
    if len(frame) < 2:
        return None
    short = frame[1] & 0x7F
    if short < 126:
        return short
    if short == 126:
        return struct.unpack("!H", frame[2:4])[0] if len(frame) >= 4 else None
    return struct.unpack("!Q", frame[2:10])[0] if len(frame) >= 10 else None


def test_inflated_length_closes_not_allocates() -> None:
    """A 64-bit length beyond `MAX_FRAME_SIZE` closes rather than buffering."""
    header = bytes([0x82, 127]) + struct.pack("!Q", WebSocket.MAX_FRAME_SIZE + 1)
    ws = _raw_websocket()
    _feed(ws, header)
    assert _is_closed(ws)
    assert buffered_bytes(ws) <= len(header)
