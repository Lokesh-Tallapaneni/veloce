"""Wire-level limits and byte literals for the serving protocol."""

from __future__ import annotations

import logging

from veloce.config import (
    DEFAULT_WRITE_BUFFER_HIGH_WATER,
)

_logger = logging.getLogger(__name__)


# Caps the request line and headers, so neither can pin RAM before MAX_CONTENT_LENGTH engages.
MAX_URL_SIZE = 8192

MAX_HEADER_SIZE = 8192

MAX_TOTAL_HEADERS_SIZE = 65536

WRITE_BUFFER_HIGH_WATER = DEFAULT_WRITE_BUFFER_HIGH_WATER

# A bitmask, so a resume for one reason cannot cancel the other's pause.
_PAUSE_BODY = 1

_PAUSE_DEPTH = 2

# RFC 6455 Sec. 4.2.1: the upgrade triplet, lowercased as `on_header` delivers it.
_WS_HEADER_CONNECTION = b"connection"

_WS_HEADER_UPGRADE = b"upgrade"

_WS_HEADER_KEY = b"sec-websocket-key"

_WS_HEADER_VERSION = b"sec-websocket-version"

_WS_HEADER_HOST = b"host"

_WS_HEADER_ORIGIN = b"origin"

CONTENT_TYPE_JSON_LINE = b"Content-Type: application/json\r\n"

# RFC 6455 Sec. 4.2.2: the only WebSocket protocol version Veloce speaks.
_WS_SUPPORTED_VERSION = b"13"

#: An int in `bytes` is a memchr; a one-byte `bytes` a dearer substring search.
_PERCENT_BYTE = 0x25
