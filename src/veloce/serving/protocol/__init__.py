"""Serving protocol — high-performance HTTP/1.1 over a raw asyncio transport.

Parses the wire with httptools and dispatches requests through the Veloce app,
bypassing the ASGI layer entirely.

The package splits `HttpProtocol` along its seams: `http` (the protocol and its
request loop), `_websocket` (the RFC 6455 upgrade), `_refusal` (error and
refusal responses), plus `_constants` and `_helpers`. Every name the single
module exposed still imports from `veloce.serving.protocol`.
"""

from __future__ import annotations

from veloce.serving.protocol._constants import (
    MAX_HEADER_SIZE,
    MAX_TOTAL_HEADERS_SIZE,
    MAX_URL_SIZE,
    WRITE_BUFFER_HIGH_WATER,
)
from veloce.serving.protocol._helpers import _enable_tcp_keepalive, _strip_response_body
from veloce.serving.protocol.http import HttpProtocol

__all__ = [
    "MAX_HEADER_SIZE",
    "MAX_TOTAL_HEADERS_SIZE",
    "MAX_URL_SIZE",
    "WRITE_BUFFER_HIGH_WATER",
    "HttpProtocol",
    "_enable_tcp_keepalive",
    "_strip_response_body",
]
