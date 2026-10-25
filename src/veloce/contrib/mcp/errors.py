"""MCP error hierarchy — failures that render as JSON-RPC 2.0 error objects.

A handler author raises a concrete subclass to surface a typed JSON-RPC error;
an `_InBandError` subclass surfaces instead as an `isError` tool result, and
`ToolError` is that branch's one public member.

Codes follow JSON-RPC 2.0 Sec. 5.1 plus the MCP server-defined codes for
"resource not found" and "forbidden".
"""

from __future__ import annotations

from typing import Any

# JSON-RPC 2.0 error codes (Sec. 5.1) plus the MCP "method not found" reuse.
_JSONRPC_PARSE_ERROR = -32700
_JSONRPC_INVALID_REQUEST = -32600
_JSONRPC_METHOD_NOT_FOUND = -32601
_JSONRPC_INVALID_PARAMS = -32602
_JSONRPC_INTERNAL_ERROR = -32603

# MCP "Resource not found" - server-defined, outside the JSON-RPC reserved range.
_JSONRPC_RESOURCE_NOT_FOUND = -32002

# Server-defined "forbidden" - the JSON-RPC analogue of HTTP 403
# insufficient_scope, for a principal missing a required scope.
_JSONRPC_FORBIDDEN = -32003

# The 2026-07-28 revision reserves -32020 to -32099 for the specification and
# allocates -32022 to `UnsupportedProtocolVersion`.
_JSONRPC_UNSUPPORTED_PROTOCOL_VERSION = -32022

# Allocated in the same reserved range, for a missing, disagreeing or
# unencodable standard request header.
_JSONRPC_HEADER_MISMATCH = -32020


def _error(msg_id: Any, code: int, message: str, data: Any = None) -> dict[str, Any]:
    """Build a JSON-RPC 2.0 error response object."""
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": msg_id, "error": error}


def parse_error() -> dict[str, Any]:
    """Build the JSON-RPC error for a body that is not readable as JSON at all.

    JSON-RPC keeps two failures apart: -32700 says the text could not be read,
    -32600 says what was read is not a Request object.
    """
    return _error(None, _JSONRPC_PARSE_ERROR, "Parse error")


def invalid_request_error() -> dict[str, Any]:
    """Build the JSON-RPC error for a readable body that is not a Request.

    A JSON array lands here too: the revisions this server speaks carry no
    batches. See `parse_error` for why the two are distinct.
    """
    return _error(None, _JSONRPC_INVALID_REQUEST, "Invalid Request")


def internal_error(msg_id: Any, message: str) -> dict[str, Any]:
    """Build the JSON-RPC error for a server-owned failure, carrying the id.

    A transport that cannot put its reply on the wire still answers with this,
    rather than leaving the client waiting on a dropped reply.
    """
    return _error(msg_id, _JSONRPC_INTERNAL_ERROR, message)


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


class _InBandError(MCPError):
    """A failure surfaced in-band as an `isError` tool result, not a JSON-RPC error.

    The tool paths report these as a tool result rather than through `to_error`.
    Only `ToolError` is public.
    """


class ToolError(_InBandError):
    """A tool failed at the work it was asked to do — reported as an `isError` result.

    Only a *tool execution* error is fed back to the language model; a protocol
    error travels as a JSON-RPC error the model never sees. Raise this, not
    another `MCPError`, when the agent should understand why the work failed.

    The message is surfaced verbatim, so write it for the model: name what
    failed and what would make a retry succeed.

    Only the tool path has an in-band channel; from a resource read or a prompt
    the same failure travels as the `-32603` its base carries.
    """


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

    `data.supported` makes the rejection recoverable, and the code itself
    identifies the server as modern: an older server answers a probe with
    method-not-found, which sends the client to the `initialize` handshake.
    """

    code = _JSONRPC_UNSUPPORTED_PROTOCOL_VERSION
    http_status = 400

    def __init__(self, requested: str, supported: tuple[str, ...]) -> None:
        super().__init__(
            "Unsupported protocol version",
            data={"supported": list(supported), "requested": requested},
        )


class HeaderMismatchError(MCPError):
    """A standard request header disagrees with the body it labels.

    An intermediary routes on `Mcp-Method` / `Mcp-Name` while the server executes
    the body, so a divergence is a request-smuggling primitive. MCP 2026-07-28
    Streamable HTTP "Server Validation" rejects it before dispatch, along with an
    absent required header and an unencodable value.
    """

    code = _JSONRPC_HEADER_MISMATCH
    http_status = 400


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


class MCPCapabilityError(InvalidRequestError, _InBandError):
    """A server->client request needs a client capability the client did not advertise.

    Raised by `MCPContext.sample` / `elicit` / `roots` when ``initialize`` did
    not advertise the matching capability. In-band on the tool path, so the
    model can carry on; resources and prompts have no in-band channel and get
    the `-32600` its base carries.
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


class _InvalidArgumentsError(_InBandError):
    """Tool arguments failed validation — reported in-band so the model can retry.

    The spec counts input validation as a *tool execution* error, and only those
    reach the language model. The code stays `-32602` for the resource and prompt
    paths, which have no in-band channel; the tool path catches this type before
    `to_error`.
    """

    code = _JSONRPC_INVALID_PARAMS


class _StreamTooLargeError(_InBandError):
    """A streamed tool result exceeded the buffer limit — reported in-band."""


class _StreamTimeoutError(_InBandError):
    """A streamed tool result outran the drain deadline — reported in-band."""
