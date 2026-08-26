"""Drive a native-transport `WebSocket` in memory and read one JSON frame back.

The native path has no ASGI send callable, so `send_json` frames the payload
itself. Nothing else in the suite exercises that branch of `send_json`.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from veloce.websocket import WebSocket

_KEY = {"sec-websocket-key": "dGhlIHNhbXBsZSBub25jZQ=="}


class _RecordingTransport:
    """A minimal asyncio transport that records what was written."""

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


def _payload_of(frame: bytes) -> bytes:
    """Extract the payload of a single unmasked server frame (RFC 6455 Sec. 5.2)."""
    length = frame[1] & 0x7F
    offset = 2
    if length == 126:
        length = int.from_bytes(frame[2:4], "big")
        offset = 4
    elif length == 127:
        length = int.from_bytes(frame[2:10], "big")
        offset = 10
    return frame[offset : offset + length]


def native_ws_json(payload: Any) -> Any:
    """Send `payload` with `send_json` over a native transport; return what was framed."""

    async def run() -> Any:
        transport = _RecordingTransport()
        ws = WebSocket(transport, dict(_KEY))
        ws._accepted = True
        await ws.send_json(payload)
        frame = transport.writes[-1]
        assert frame[0] & 0x0F == 0x1, "send_json(mode='text') must use a text frame"
        return json.loads(_payload_of(frame).decode("utf-8"))

    return asyncio.run(run())
