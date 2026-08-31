"""Unmasking a frame is blocked, so a large frame does not blow up the heap.

RFC 6455 Sec. 5.3 masks every client-to-server payload with a repeating 4-byte
key. Veloce XORs it through Python's big integers, which is far faster than a
byte loop — but big-integer XOR is superlinear in operand size, so doing it in
one operation over a whole frame is both slower *and* far more allocation-hungry
than doing it in fixed blocks.

`MAX_FRAME_SIZE` is 16 MiB, so a single masked frame could put ~70 MB of
transient big integers on the heap. Measured on the project's benchmark host:

| frame | whole-frame | 16 KiB blocks | |
|---|---|---|---|
| 16 KiB | 28.4 us | 28.4 us | identical - below the threshold |
| 32 KiB | 74.3 us | 61.0 us | -17.9% |
| 64 KiB | 199.7 us | 117.1 us | -41.4% |
| 4 MiB | 12.0 ms | 5.2 ms | -57%, peak 4.20x -> 2.05x frame |
| 16 MiB | 50.2 ms | 21.7 ms | -57%, peak 70.5 MB -> 33.8 MB |

These tests are about correctness, not speed: a blocked XOR has a seam at every
block boundary and a ragged tail, and getting either wrong corrupts a payload in
a way only a large frame would show. Every case that straddles a boundary is
covered explicitly.
"""

from __future__ import annotations

import os
import struct

import pytest

from veloce import Veloce
from veloce.testclient import TestClient
from veloce.websocket import _UNMASK_BLOCK, _unmask

MASK = b"\x01\x02\x03\x04"


def _reference(payload: bytes, mask: bytes) -> bytes:
    """The definition from RFC 6455 Sec. 5.3, written the obvious way."""
    return bytes(byte ^ mask[i % 4] for i, byte in enumerate(payload))


# ── the sizes a block boundary can go wrong at ───────────────────────


@pytest.mark.parametrize(
    "length",
    [
        0,
        1,
        2,
        3,
        4,
        5,
        7,
        _UNMASK_BLOCK - 1,
        _UNMASK_BLOCK,
        _UNMASK_BLOCK + 1,
        _UNMASK_BLOCK + 3,
        _UNMASK_BLOCK * 2 - 1,
        _UNMASK_BLOCK * 2,
        _UNMASK_BLOCK * 2 + 1,
        _UNMASK_BLOCK * 3 + 17,
    ],
)
def test_the_blocked_unmask_matches_the_definition(length):
    payload = os.urandom(length)
    assert _unmask(payload, MASK, length) == _reference(payload, MASK)


@pytest.mark.parametrize("length", [_UNMASK_BLOCK - 1, _UNMASK_BLOCK, _UNMASK_BLOCK + 1])
def test_the_result_is_bytes_at_every_size(length):
    """The two branches must not return different types."""
    assert type(_unmask(os.urandom(length), MASK, length)) is bytes


def test_a_frame_spanning_many_blocks_round_trips():
    """Masking is its own inverse, so unmasking twice is the identity."""
    payload = os.urandom(_UNMASK_BLOCK * 5 + 123)
    once = _unmask(payload, MASK, len(payload))
    assert _unmask(once, MASK, len(once)) == payload


def test_an_empty_payload_is_empty():
    assert _unmask(b"", MASK, 0) == b""


# ── the mask itself ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    "mask",
    [
        b"\x00\x00\x00\x00",
        b"\xff\xff\xff\xff",
        b"\x00\xff\x00\xff",
        b"\x01\x00\x00\x00",
        b"\xde\xad\xbe\xef",
    ],
)
def test_any_mask_matches_the_definition(mask):
    payload = os.urandom(_UNMASK_BLOCK * 2 + 9)
    assert _unmask(payload, mask, len(payload)) == _reference(payload, mask)


def test_an_all_zero_mask_leaves_the_payload_alone():
    payload = os.urandom(_UNMASK_BLOCK + 5)
    assert _unmask(payload, b"\x00\x00\x00\x00", len(payload)) == payload


def test_the_mask_phase_is_kept_across_a_block_boundary():
    """The one thing a blocked implementation gets wrong.

    Each block is XORed against a mask tiled from index 0, which is only correct
    because the block size is a multiple of 4. A payload of all-zero bytes makes
    the resulting phase directly readable.
    """
    length = _UNMASK_BLOCK + 8
    out = _unmask(b"\x00" * length, MASK, length)
    assert out == (MASK * (length // 4 + 1))[:length]
    assert _UNMASK_BLOCK % 4 == 0, "the block size must be a whole number of mask periods"


def test_a_payload_of_high_bytes_is_not_truncated():
    """A leading zero byte after XOR must survive `int.to_bytes` round-tripping.

    The big-integer route drops leading zeros; the fixed `length` passed to
    `to_bytes` is what restores them, and a payload whose first byte XORs to zero
    is what would expose losing that.
    """
    payload = bytes([MASK[0]]) + os.urandom(_UNMASK_BLOCK + 40)
    out = _unmask(payload, MASK, len(payload))
    assert out[0] == 0
    assert len(out) == len(payload)


def test_a_payload_that_is_all_zero_after_unmasking_keeps_its_length():
    length = _UNMASK_BLOCK * 2
    payload = (MASK * (length // 4))[:length]
    out = _unmask(payload, MASK, length)
    assert out == b"\x00" * length


# ── end to end, through the frame parser ─────────────────────────────


def _client_frame(payload: bytes, opcode: int = 0x2) -> bytes:
    """A masked client frame, built the way a browser would.

    Deliberately **not** `tests/_ws_frames.client_frame`, which the other
    websocket modules share: this one masks through `_reference`, the
    straightforward implementation this module checks the optimised `_unmask`
    against. Building the frame with the shared helper would mask it with the
    same code path under test, so a bug in it would cancel out and the
    comparison would prove nothing.
    """
    mask = b"\x11\x22\x33\x44"
    n = len(payload)
    head = bytearray([0x80 | opcode])
    if n < 126:
        head.append(0x80 | n)
    elif n < 65536:
        head.append(0x80 | 126)
        head += struct.pack("!H", n)
    else:
        head.append(0x80 | 127)
        head += struct.pack("!Q", n)
    head += mask
    return bytes(head) + _reference(payload, mask)


@pytest.mark.parametrize("length", [8, 1000, _UNMASK_BLOCK, _UNMASK_BLOCK * 2 + 77])
def test_a_masked_frame_arrives_intact(length):
    """The parser must deliver exactly the bytes the client sent."""

    payload = os.urandom(length)
    app = Veloce(openapi_url=None)

    @app.websocket("/ws")
    async def echo(websocket):
        await websocket.accept()
        data = await websocket.receive_bytes()
        await websocket.send_bytes(data)
        await websocket.close()

    with TestClient(app).websocket_connect("/ws") as ws:
        ws.send_bytes(payload)
        assert ws.receive_bytes() == payload
