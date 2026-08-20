"""MCP error hierarchy — failures that render as JSON-RPC 2.0 error objects.

One base (`MCPError`) carries the JSON-RPC error code and optional `data`; the
dispatcher catches the base and serialises polymorphically via `to_error`, so a
new failure mode is a new subclass rather than another `except` clause. A tool /
resource / prompt handler author may `raise InvalidParamsError(...)` (and the
other concrete subclasses) to surface a typed JSON-RPC error.

Stream-drain failures stay a private `_InBandError` subtree: they surface
in-band as an `isError` tool result, not on the JSON-RPC error channel, so they
are not part of the public surface even though they share the base.

Codes follow JSON-RPC 2.0 Sec. 5.1 plus the MCP server-defined codes for
"resource not found" and "forbidden".
"""

from __future__ import annotations

from typing import Any

# JSON-RPC 2.0 error codes (Sec. 5.1) plus the MCP "method not found" reuse.
_JSONRPC_INVALID_REQUEST = -32600
_JSONRPC_METHOD_NOT_FOUND = -32601
_JSONRPC_INVALID_PARAMS = -32602
_JSONRPC_INTERNAL_ERROR = -32603

# MCP "Resource not found" code (server-defined, outside the JSON-RPC reserved
# range), returned when ``resources/read`` names a URI the registry cannot
# resolve or whose route answers 404.
_JSONRPC_RESOURCE_NOT_FOUND = -32002

# Server-defined "forbidden" code, returned when the request principal lacks a
# tool's / resource's required authorization scopes (the JSON-RPC analogue of an
# HTTP 403 insufficient_scope).
_JSONRPC_FORBIDDEN = -32003

# The 2026-07-28 revision partitions the JSON-RPC server-error range: -32000 to
# -32019 stays implementation-defined (existing SDK usage is grandfathered) and
# -32020 to -32099 is reserved for the specification. `UnsupportedProtocolVersion`
# is allocated -32022 there.
_JSONRPC_UNSUPPORTED_PROTOCOL_VERSION = -32022


def _error(msg_id: Any, code: int, message: str, data: Any = None) -> dict[str, Any]:
    """Build a JSON-RPC 2.0 error response object."""
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": msg_id, "error": error}


def _insufficient_scope(required: frozenset[str]) -> str:
    """Build the error text for a missing-scope rejection."""
    return f"insufficient_scope: requires {sorted(required)}"


class MCPError(Exception):
    """Base for any MCP failure that maps to a JSON-RPC error object."""

    # Class-level default; each subclass overrides with its JSON-RPC code.
    code: int = _JSONRPC_INTERNAL_ERROR

    # HTTP status a transport-level violation maps to when raised before
    # dispatch; only the transport pre-dispatch subclasses override it.
    http_status: int = 400

    def __init__(self, message: str, *, data: Any = None) -> None:
        super().__init__(message)
        self.data = data

    def to_error(self, msg_id: Any) -> dict[str, Any]:
        """Render this error as a JSON-RPC 2.0 error response."""
        return _error(msg_id, self.code, str(self), self.data)


class InvalidRequestError(MCPError):
    """A malformed JSON-RPC request object."""

    code = _JSONRPC_INVALID_REQUEST


class ProtocolVersionError(InvalidRequestError):
    """The HTTP `MCP-Protocol-Version` header names a version this server rejects.

    Per the MCP 2025-06-18 Streamable HTTP transport the server MUST answer a
    request carrying an invalid or unsupported protocol version with HTTP 400.
    """

    http_status = 400


class UnsupportedProtocolVersionError(MCPError):
    """A request declares a protocol version this server does not serve.

    Modern (2026-07-28 and later) clients carry their protocol version on every
    request rather than negotiating once, so the rejection has to travel per
    request and has to be recoverable: `data.supported` lets the client pick a
    mutually supported version and retry.

    This is also the error that identifies the server as modern. A client
    probing an older server gets an unrecognised error (method-not-found) and
    falls back to the `initialize` handshake; getting *this* code back tells it
    the server speaks the modern protocol and only the version was wrong.
    """

    code = _JSONRPC_UNSUPPORTED_PROTOCOL_VERSION
    http_status = 400

    def __init__(self, requested: str, supported: tuple[str, ...]) -> None:
        super().__init__(
            "Unsupported protocol version",
            data={"supported": list(supported), "requested": requested},
        )


class OriginNotAllowedError(InvalidRequestError):
    """A request carries an `Origin` header outside the configured allowlist.

    Per the MCP transport's DNS-rebinding defense the server MUST reject a
    present, disallowed `Origin` with HTTP 403 (a missing `Origin`, a non-browser
    client, is allowed).
    """

    http_status = 403


class SessionRequiredError(InvalidRequestError):
    """A post-initialization request omitted the required `Mcp-Session-Id` header.

    Per the MCP 2025-06-18 Streamable HTTP transport, once the server has assigned
    a session id the client MUST echo it on every later request; a request missing
    it is answered HTTP 400.
    """

    http_status = 400


class SessionNotFoundError(MCPError):
    """A request named an `Mcp-Session-Id` the server no longer recognizes.

    Per the MCP 2025-06-18 Streamable HTTP transport a terminated (or never
    issued) session is answered HTTP 404, signalling the client to start a new
    session.
    """

    code = _JSONRPC_INVALID_REQUEST
    http_status = 404


class MethodNotFoundError(MCPError):
    """A request named a method this server does not implement."""

    code = _JSONRPC_METHOD_NOT_FOUND


class InvalidParamsError(MCPError):
    """A call's params are missing, mistyped, or fail validation."""

    code = _JSONRPC_INVALID_PARAMS


class InternalError(MCPError):
    """An unexpected server-side failure handling the request."""

    code = _JSONRPC_INTERNAL_ERROR


class ResourceNotFoundError(MCPError):
    """A ``resources/read`` named a URI the server cannot resolve."""

    code = _JSONRPC_RESOURCE_NOT_FOUND


class AuthorizationError(MCPError):
    """The principal lacks a required scope — reported as a forbidden error.

    Carries the required scopes so the HTTP transport can surface them in an
    RFC 6750 `WWW-Authenticate` insufficient-scope challenge.
    """

    code = _JSONRPC_FORBIDDEN

    def __init__(self, scopes: frozenset[str]) -> None:
        super().__init__(_insufficient_scope(scopes), data={"requiredScopes": sorted(scopes)})
        self.scopes = scopes


class MCPCapabilityError(InvalidRequestError):
    """A server->client request needs a client capability the client did not advertise.

    Raised by `MCPContext.sample` / `elicit` / `roots` (and their sub-capabilities)
    when the connected client did not advertise the matching capability in
    ``initialize``, so the request cannot be issued.
    """

    def __init__(self, capability: str) -> None:
        super().__init__(
            f"client did not advertise the {capability!r} capability",
            data={"capability": capability},
        )
        self.capability = capability


class _ForbiddenError(MCPError):
    """A route answered 401/403 during a resource read — a forbidden error.

    Distinct from `AuthorizationError`: it carries the route's own error body
    rather than a scope set, so it is not part of the public surface.
    """

    code = _JSONRPC_FORBIDDEN


class _InBandError(MCPError):
    """A failure surfaced in-band as an `isError` tool result, not a JSON-RPC error.

    Shares `MCPError` so the type relationship is explicit, but the stream-drain
    paths report these as a tool result rather than routing them through
    `to_error`, so the subtree stays private.
    """


class _InvalidArgumentsError(_InBandError):
    """Tool arguments failed validation — reported in-band so the model can retry.

    The spec splits tool failures in two: a *protocol* error (unknown tool, a
    request that does not satisfy the `CallToolRequest` schema, a server fault)
    and a *tool execution* error, which explicitly includes input validation.
    Only the second is fed back to the language model, because only it carries
    something the model can act on — so reporting a bad argument as
    `-32602 Invalid params` denies the model the one signal that would let it
    correct the call and retry.

    The code is still `-32602`, because `isError` is a *tool result* field:
    resources and prompts have no in-band channel, so the same failure has to
    travel as a JSON-RPC error there. The tool path catches this type before it
    reaches `to_error`; the other paths let it through with the right code.
    """

    code = _JSONRPC_INVALID_PARAMS


class _StreamTooLargeError(_InBandError):
    """A streamed tool result exceeded the buffer limit — reported in-band."""


class _StreamTimeoutError(_InBandError):
    """A streamed tool result outran the drain deadline — reported in-band."""
