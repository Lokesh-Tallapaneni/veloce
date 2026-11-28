"""WebSocket support — one `WebSocket` over two transports.

The same object serves an ASGI `websocket` scope, where the server owns the
framing, and the built-in server's raw transport, where this does the RFC 6455
work itself: the accept key, frame parsing and masking, fragmentation
reassembly, incremental UTF-8 validation of text payloads (RFC 6455 Sec. 5.6),
the close handshake, and an opt-in heartbeat.

The package splits that along its seams: `handshake` (accept key, option
validation), `close` (close codes), `frames` (opcodes, unmasking), `utf8` (the
streaming validator), `raw` (framing, heartbeat and writes on a raw transport)
and `connection` (the `WebSocket` object). Every name the single module
exposed still imports from `veloce.websocket`.
"""

from __future__ import annotations

from veloce.exceptions import WebSocketDisconnect
from veloce.status import (
    WS_1000_NORMAL_CLOSURE,
    WS_1001_GOING_AWAY,
    WS_1002_PROTOCOL_ERROR,
    WS_1005_NO_STATUS_RCVD,
    WS_1006_ABNORMAL_CLOSURE,
    WS_1007_INVALID_FRAME_PAYLOAD_DATA,
    WS_1009_MESSAGE_TOO_BIG,
)
from veloce.websocket.close import _PEER_CLOSE_CODES_OK, _sanitise_close
from veloce.websocket.connection import WebSocket, WebSocketState
from veloce.websocket.frames import (
    _OP_BINARY,
    _OP_CLOSE,
    _OP_CONT,
    _OP_PING,
    _OP_PONG,
    _OP_TEXT,
    _RAW_DISCONNECT,
    _UNMASK_BLOCK,
    _unmask,
)
from veloce.websocket.handshake import (
    _WS_ACCEPT_GUID,
    _validate_heartbeat,
    _validate_idle_timeout,
    _validate_positive_seconds,
    compute_accept,
)
from veloce.websocket.utf8 import _Utf8Validator

__all__ = [
    "WS_1000_NORMAL_CLOSURE",
    "WS_1001_GOING_AWAY",
    "WS_1002_PROTOCOL_ERROR",
    "WS_1005_NO_STATUS_RCVD",
    "WS_1006_ABNORMAL_CLOSURE",
    "WS_1007_INVALID_FRAME_PAYLOAD_DATA",
    "WS_1009_MESSAGE_TOO_BIG",
    "WebSocket",
    "WebSocketDisconnect",
    "WebSocketState",
    "compute_accept",
]
