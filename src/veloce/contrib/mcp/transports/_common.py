"""Admission control and protocol framing shared by the MCP HTTP transports.

Streamable HTTP (`http.py`) and the 2024-11-05 HTTP+SSE pair (`sse.py`) share
one door: reject a browser `Origin` outside the allowlist, verify the bearer
token, and refuse with the RFC 6750 challenge naming the RFC 9728 metadata
route. Protocol documents are framed as protocol, not as application data (see
`encode_envelope`).
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

# Pre-encoded for `_peek_header_key`, which reads one header without the mapping.
_RAW_AUTHORIZATION = b"authorization"

# MCP 2025-11-25 requires "an SSE event with a standard retry field" on close.
_SSE_RETRY_MS = 3000

# Session-id entropy, inside the MCP-mandated visible-ASCII range. It must be
# unguessable: the id alone ties a 2024-11-05 POST to the stream answering it.
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
    parts = [f'Bearer error="{error}"']
    # RFC 9728 Sec. 5.1 makes `resource_metadata` a MAY; bearer-only has no route.
    if not auth.bearer_only:
        parts.append(f'resource_metadata="{PROTECTED_RESOURCE_METADATA_PATH}"')
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
    # RFC 6750 Sec. 2.1 + RFC 7235 permit only SP/HTAB between scheme and token.
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

    The path is fixed by the RFC, so a second mount on one app is a no-op; both
    would serve the same document. A bearer-only mount has none, and names none.
    """
    # The flag `_challenge` reads, so header and routing table cannot disagree.
    if auth is None or auth.bearer_only:
        return
    if app.match("GET", PROTECTED_RESOURCE_METADATA_PATH) is not None:
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
