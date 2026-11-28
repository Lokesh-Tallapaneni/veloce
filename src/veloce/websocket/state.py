"""`WebSocketState` — the ASGI-shaped connection-state enum."""

from __future__ import annotations

import enum


class WebSocketState(enum.IntEnum):
    """Connection-state enum - ASGI shape.

    `CONNECTING` until `accept()` is sent (client side) or received
    (application side), `CONNECTED` once the handshake completes, and
    `DISCONNECTED` once a close frame crosses that side.
    """

    CONNECTING = 0
    CONNECTED = 1
    DISCONNECTED = 2
