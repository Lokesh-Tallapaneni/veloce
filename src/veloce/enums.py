"""Closed sets of string values — enums that stay interchangeable with plain strings.

Each member is a `str`, so every parameter that takes one keeps accepting the
plain spelling (`transport="http"` and `transport=MCPTransport.HTTP` are the same
call) and `member == "value"` holds. The enum is there so a type checker can
reject a typo and an editor can complete the choices.
"""

from __future__ import annotations

from enum import Enum


class _StrEnum(str, Enum):
    """A `str` enum whose `str()` is its value, on every supported Python."""

    # `enum.StrEnum` is 3.11+; the package supports 3.10. Without this a
    # formatted member would read `MCPTransport.HTTP` rather than `http`.
    def __str__(self) -> str:
        return str(self.value)


class MCPTransport(_StrEnum):
    """The wires `Veloce.mount_mcp` can serve MCP over."""

    STDIO = "stdio"
    HTTP = "http"
    #: The deprecated split-endpoint wire; prefer `HTTP` for anything new.
    SSE = "sse"


class CookiePrefix(_StrEnum):
    """The cookie-name prefixes of RFC 6265bis Sec. 4.1.3."""

    #: `__Host-`: Secure, `Path=/`, and no `Domain`, so the cookie is host-bound.
    HOST = "host"
    #: `__Secure-`: the cookie is only accepted over HTTPS.
    SECURE = "secure"


class DuplicatePolicy(_StrEnum):
    """What a router does when a second handler claims the same path and method."""

    #: Refuse the second registration with `DuplicateRouteError`.
    ERROR = "error"
    #: Keep the second handler and log that it replaced the first.
    WARN = "warn"
    #: Keep the second handler silently.
    OVERRIDE = "override"


class WebSocketMode(_StrEnum):
    """How `WebSocket.send_json` frames its payload."""

    #: A text frame (opcode 0x1).
    TEXT = "text"
    #: A binary frame (opcode 0x2) carrying the raw JSON bytes.
    BINARY = "binary"


class ContentCoding(_StrEnum):
    """The content codings `CompressionMiddleware` can offer."""

    ZSTD = "zstd"
    BR = "br"
    GZIP = "gzip"


class SameSite(_StrEnum):
    """The `SameSite` cookie attribute values (RFC 6265bis Sec. 4.1.2.7)."""

    STRICT = "Strict"
    LAX = "Lax"
    NONE = "None"
