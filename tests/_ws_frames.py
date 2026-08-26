"""One RFC 6455 client-frame builder for the websocket test modules.

`_client_frame` was written out in six modules. Five carried the extended-length
branches; `test_websocket_heartbeat.py`'s copy had only the 7-bit form, so it
silently produced a **corrupt header** for any payload of 126 bytes or more -
`0x80 | n` overflows the length field into the mask bit. That module happens
never to send one today, so the copy was wrong without being noticed, and would
have misled whoever first added a larger payload there.

Kept as a helper module rather than a `conftest.py` fixture because these are
called at module scope to build constants, not injected per test.

RFC 6455 Sec. 5.1: a client-to-server frame MUST be masked. Sec. 5.2: a payload
under 126 bytes uses the 7-bit length, under 65536 the 16-bit extension, and
above that the 64-bit one.
"""

from __future__ import annotations

import struct

DEFAULT_MASK = b"\x12\x34\x56\x78"


def client_frame(
    opcode: int,
    payload: bytes,
    fin: bool = True,
    mask: bytes = DEFAULT_MASK,
) -> bytes:
    """Build one masked client-to-server frame."""
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    first = (0x80 if fin else 0x00) | opcode
    length = len(payload)
    if length < 126:
        header = bytes([first, 0x80 | length])
    elif length < 65536:
        header = bytes([first, 0x80 | 126]) + struct.pack("!H", length)
    else:
        header = bytes([first, 0x80 | 127]) + struct.pack("!Q", length)
    return header + mask + masked
