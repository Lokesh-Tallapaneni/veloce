"""RFC 6455 handshake: `Sec-WebSocket-Accept` uses the correct magic GUID.

Validates against the worked example in RFC 6455 Sec. 1.3 - the ground truth
every conformant client (e.g. the `websockets` library) checks against.
"""

from __future__ import annotations

from veloce.websocket import WebSocket

# RFC 6455 Sec. 1.3 worked example: this key must yield this accept value.
_RFC_KEY = "dGhlIHNhbXBsZSBub25jZQ=="
_RFC_ACCEPT = "s3pPLMBiTxaQ9kYGzzhZRbK+xOo="


class _RecordingTransport:
    def __init__(self) -> None:
        self.data = bytearray()

    def write(self, b: bytes) -> None:
        self.data += b

    def pause_reading(self) -> None:
        pass

    def resume_reading(self) -> None:
        pass

    def is_closing(self) -> bool:
        return False

    def close(self) -> None:
        pass


def test_guid_is_the_rfc6455_value():
    assert WebSocket.GUID == "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


async def test_raw_accept_computes_rfc6455_accept_key():
    transport = _RecordingTransport()
    ws = WebSocket(transport, {"sec-websocket-key": _RFC_KEY})
    await ws.accept()
    head = bytes(transport.data).decode("latin-1")
    assert "101 Switching Protocols" in head
    # The accept value a conformant client recomputes and compares.
    assert f"Sec-WebSocket-Accept: {_RFC_ACCEPT}" in head
