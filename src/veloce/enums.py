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


class SameSite(_StrEnum):
    """The `SameSite` cookie attribute values (RFC 6265bis Sec. 4.1.2.7)."""

    STRICT = "Strict"
    LAX = "Lax"
    NONE = "None"
