"""Admission control and protocol framing shared by the MCP HTTP transports.

Both remote transports — Streamable HTTP (`http.py`) and the 2024-11-05
HTTP+SSE pair (`sse.py`) — front a JSON-RPC endpoint with the same door: reject
a browser `Origin` outside the allowlist, verify the bearer token, and answer a
refusal with the RFC 6750 challenge that names the RFC 9728 metadata route. They
also frame every protocol document the same way, which is not the same thing as
framing application data (see `encode_envelope`).

`sse.py` used to import all of this from `http.py`. That made a sibling
transport's private surface load-bearing in a way invisible from `http.py`'s
side, so neither module could rename one of these without breaking the other,
and the half `sse.py` did not import it copied instead — a copy that had already
drifted into answering a different JSON-RPC error code. One owner, imported by
both, is what stops that recurring.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any, cast

import veloce.status as status
from veloce._internal import _bearer_token_from
from veloce.contrib.mcp._helpers import encode_envelope
from veloce.contrib.mcp.auth import PROTECTED_RESOURCE_METADATA_PATH, MCPAuth
from veloce.contrib.mcp.errors import OriginNotAllowedError
from veloce.http.response import JSONResponse, Response
from veloce.principal import Principal

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Sequence

    from veloce.http.request import Request

_logger = logging.getLogger(__name__)

# The `Authorization` header, pre-encoded for `_peek_header_key`, which reads one
# header without building the whole mapping.
_RAW_AUTHORIZATION = b"authorization"

# Reconnect hint (milliseconds) emitted as the SSE `retry` field before a stream
# closes, so a disconnected client knows how long to wait before reconnecting
# (MCP 2025-11-25 transport: "send an SSE event with a standard retry field
# before closing").
_SSE_RETRY_MS = 3000

# Bytes of entropy in a session id. `secrets.token_urlsafe` yields URL-safe
# base64 (characters within the MCP-mandated visible-ASCII range 0x21-0x7E), so
# the id is globally unique and cryptographically secure per the transport spec.
# It also appears in the endpoint URL the 2024-11-05 transport tells a client to
# POST to, and is the only thing tying a POST to the stream that will answer it,
# so it must be unguessable rather than sequential.
_SESSION_ID_ENTROPY_BYTES = 24


def _protocol_response(
    payload: Any, *, status_code: int = status.HTTP_200_OK, headers: dict[str, str] | None = None
) -> Response:
    """Build a response carrying an MCP protocol document, encoded as protocol.

    `JSONResponse` resolves the application's JSON provider, which is right for
    application data and wrong for a JSON-RPC envelope - see `encode_envelope`.
    """
    response = JSONResponse._from_encoded(encode_envelope(payload))
    response.status_code = status_code
    if headers:
        response.headers.update(headers)
    return response


def _validate_origin(request: Request, allowed_origins: frozenset[str] | None) -> None:
    """Reject a present `Origin` outside the allowlist (DNS-rebinding defense).

    A missing `Origin` (a non-browser client) is allowed; a browser-set `Origin`
    not in `allowed_origins` raises `OriginNotAllowedError` (HTTP 403). Validation
    is skipped entirely when no allowlist is configured.
    """
    if allowed_origins is None:
        return
    origin = request._peek_header_key(b"origin")
    if origin is not None and origin not in allowed_origins:
        raise OriginNotAllowedError("origin not allowed")


def _challenge(auth: MCPAuth, status_code: int, error: str) -> Response:
    """Build a `401`/`403` response with the RFC 6750 `WWW-Authenticate` challenge."""
    parts = [f'Bearer error="{error}"', f'resource_metadata="{PROTECTED_RESOURCE_METADATA_PATH}"']
    if error == "insufficient_scope" and auth.required_scopes:
        parts.append(f'scope="{" ".join(sorted(auth.required_scopes))}"')
    body = {"error": error}
    return _protocol_response(
        body, status_code=status_code, headers={"WWW-Authenticate": ", ".join(parts)}
    )


async def _authenticate(
    auth: MCPAuth, request: Request
) -> tuple[Principal | None, Response | None]:
    """Validate the request's bearer token; return `(principal, challenge)`.

    A missing or invalid token yields a `401` challenge; a valid token missing the
    endpoint's required scopes yields a `403`. On success the challenge is `None`.
    """
    # The framework's own extractor, not a second parse: RFC 6750 Sec. 2.1 and
    # RFC 7235 permit only SP/HTAB between scheme and token, and a bare `.strip()`
    # also trimmed newlines and NBSP - so a token this door accepted was one the
    # HTTP door rejected. The pure extraction rather than `security/`'s wrapper,
    # which raises where this path owes the caller a challenge response.
    token = _bearer_token_from(request._peek_header_key(_RAW_AUTHORIZATION) or "")
    if not token:
        return None, _challenge(auth, 401, "invalid_token")

    try:
        outcome = auth.verify(token)
        if asyncio.iscoroutine(outcome):
            outcome = await outcome
        principal = cast("Principal | None", outcome)
    except Exception:
        _logger.exception("MCP token verification raised")
        principal = None
    if principal is None:
        return None, _challenge(auth, 401, "invalid_token")

    if auth.required_scopes and not principal.has_scopes(auth.required_scopes):
        return None, _challenge(auth, 403, "insufficient_scope")
    return principal, None


def register_metadata_route(
    app: Any, auth: MCPAuth | None, exclude_middleware: Sequence[str] | None
) -> None:
    """Serve the RFC 9728 protected-resource metadata `_challenge` points at.

    Every `401` either transport emits names this path in its `WWW-Authenticate`
    header, so a client that follows the challenge - which is the whole point of
    the header - must find something there. Registered by the HTTP transport
    alone, an SSE mount answered its own challenge with a 404.

    The path is fixed by the RFC, so two mounts on one app would collide; the
    second is a no-op, since both would serve the same document.
    """
    if auth is None or app.match("GET", PROTECTED_RESOURCE_METADATA_PATH) is not None:
        return

    async def mcp_metadata(request: Request) -> Response:
        return _protocol_response(auth.metadata())

    app.add_route(
        PROTECTED_RESOURCE_METADATA_PATH,
        mcp_metadata,
        methods=["GET"],
        include_in_schema=False,
        exclude_middleware=exclude_middleware,
    )
