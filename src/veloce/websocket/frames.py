"""WebSocket frame primitives — opcodes, unmasking and the receive sentinel."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover
    pass


# RFC 6455 Sec. 5.2 frame opcodes, by name on the cold paths only: the
# per-frame comparisons fold the literal and carry a `# _OP_*` tag instead.
_OP_CONT = 0x0
_OP_TEXT = 0x1
_OP_BINARY = 0x2
_OP_CLOSE = 0x8
_OP_PING = 0x9
_OP_PONG = 0xA

#: Bytes unmasked per bignum XOR. Python's big-integer XOR is superlinear in
#: operand size, so one XOR per frame costs more than fixed blocks (min-of-7):
#:
#:   frame   whole-frame   16 KiB blocks
#:    16 KiB    28.4 us       28.4 us   (identical - below the threshold)
#:    32 KiB    74.3 us       61.0 us   -17.9%
#:    64 KiB   199.7 us      117.1 us   -41.4%
#:     4 MiB    12.1 ms        5.2 ms   -56.7%, peak 4.20x -> 2.00x frame
#:    16 MiB    50.3 ms       22.7 ms   -55.0%, peak 70.5 MB -> 33.6 MB
#:
#: The memory bound is the point: at `MAX_FRAME_SIZE` one masked frame could
#: put ~70 MB of transient bignums on the heap. 8-128 KiB measure within ~1%.
_UNMASK_BLOCK = 16 * 1024


def _unmask(payload: bytes, mask: bytes | bytearray, length: int) -> bytes:
    """XOR `payload` with the repeating 4-byte `mask` (RFC 6455 Sec. 5.3)."""
    mask = bytes(mask)
    if length <= _UNMASK_BLOCK:
        tiled = (mask * ((length + 3) // 4))[:length]
        return (int.from_bytes(payload, "big") ^ int.from_bytes(tiled, "big")).to_bytes(
            length, "big"
        )
    # One tiled mask integer per full block; only the ragged tail needs its own.
    block_mask = int.from_bytes(mask * (_UNMASK_BLOCK // 4), "big")
    out = bytearray(length)
    view = memoryview(payload)
    pos = 0
    while True:
        end = pos + _UNMASK_BLOCK
        if end >= length:
            tail = length - pos
            tiled = (mask * ((tail + 3) // 4))[:tail]
            out[pos:length] = (
                int.from_bytes(view[pos:length], "big") ^ int.from_bytes(tiled, "big")
            ).to_bytes(tail, "big")
            return bytes(out)
        out[pos:end] = (int.from_bytes(view[pos:end], "big") ^ block_mask).to_bytes(
            _UNMASK_BLOCK, "big"
        )
        pos = end


# Terminal sentinel, enqueued to wake a handler parked in `receive_*()`.
_RAW_DISCONNECT: Any = object()
