"""Drive a native-transport `WebSocket` in memory.

The native path has no ASGI send callable, so `send_json` frames the payload
itself. Nothing else in the suite exercises that branch of `send_json`.

It also holds the two things twelve modules were reaching into `WebSocket` for.
`accept()` writes a real 101 into the transport, which a test driving frames
does not want, so twenty sites set `ws._accepted = True` by hand; and delivery
was read off `ws._receive_queue` at thirty-five sites, because `receive_text()`
awaits and these tests feed bytes and then assert synchronously what arrived.

Neither is a missing framework API. There should be no public "pretend you
handshook" method, and no public queue - the queue is the buffer behind
`receive_*`, and exposing it would make it part of the contract. What was
missing is a shared *test* seam, which is what these are.
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
        ws = mark_accepted(WebSocket(transport, dict(_KEY)))
        await ws.send_json(payload)
        frame = transport.writes[-1]
        assert frame[0] & 0x0F == 0x1, "send_json(mode='text') must use a text frame"
        return json.loads(_payload_of(frame).decode("utf-8"))

    return asyncio.run(run())


def mark_accepted(websocket: WebSocket) -> WebSocket:
    """Put `websocket` into the state that follows a completed handshake.

    `accept()` does the real thing: it computes the accept key and writes a 101
    into the transport (or sends `websocket.accept` over ASGI). A test about
    frame handling, timeouts or the close handshake does not want that write in
    the recording it is about to assert on, and on the native path `accept()`
    cannot negotiate a subprotocol anyway because the 101 has already gone.

    So twenty sites set the flag by hand. This is the same poke, named once,
    with the reason attached - rather than twenty bare assignments to a private
    attribute that read as if they were working around something.
    """
    websocket._accepted = True
    return websocket


def accepted_websocket(
    headers: dict[str, str] | None = None,
) -> tuple[WebSocket, _RecordingTransport]:
    """A `WebSocket` in the post-handshake state, on a recording transport.

    `accept()` performs the real handshake and writes a 101 into the transport,
    which a test about frame handling has to then skip past. This puts the
    socket straight into the state that follows, and hands back the transport so
    the test can read what was written.
    """
    transport = _RecordingTransport()
    return mark_accepted(WebSocket(transport, dict(headers or _KEY))), transport


def delivered(websocket: WebSocket) -> list[Any]:
    """Everything currently buffered for `receive_*`, drained without waiting.

    `receive_text()` awaits, so a test that feeds bytes and wants to assert what
    arrived cannot use it without arranging a task. This is the same buffer,
    read the way those tests actually need to read it.
    """
    out: list[Any] = []
    while True:
        try:
            out.append(websocket._receive_queue.get_nowait())
        except asyncio.QueueEmpty:
            return out


def buffered_bytes(websocket: WebSocket) -> int:
    """How many raw bytes the frame parser is still holding un-consumed.

    `_recv_buffer` is the frame-assembly buffer, and by the reasoning above it
    should not become public framework API - but a parser fuzz test has to be
    able to say "nothing was parked here", so the poke is named once.
    """
    return len(websocket._recv_buffer)


def nothing_delivered(websocket: WebSocket) -> bool:
    """Whether nothing has been buffered for `receive_*` yet."""
    return websocket._receive_queue.empty()


def deliver(websocket: WebSocket, payload: Any) -> None:
    """Buffer `payload` as if a frame carrying it had arrived.

    The inverse of `delivered`: a test that is about what `receive_*` does with
    a message, rather than about framing, can put one there directly instead of
    building a frame to feed.
    """
    websocket._receive_queue.put_nowait(payload)
