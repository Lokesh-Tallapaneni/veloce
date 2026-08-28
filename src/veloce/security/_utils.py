"""Security utilities — shared credential extraction helpers for auth schemes."""

from __future__ import annotations

from typing import Any

from veloce._constants import HEADER_AUTHORIZATION, HEADER_WWW_AUTHENTICATE, MSG_NOT_AUTHENTICATED
from veloce._internal import _bearer_token_from
from veloce._internal import _quote_header_value as _quote_header_value
from veloce._protocol_constants import AUTH_SCHEME_BEARER
from veloce.exceptions import HTTPException
from veloce.status import HTTP_401_UNAUTHORIZED

_BEARER_PREFIX = AUTH_SCHEME_BEARER + " "
_BEARER_PREFIX_LOWER = _BEARER_PREFIX.lower()
_BEARER_PREFIX_LEN = len(_BEARER_PREFIX)


def _validate_realm(realm: str) -> None:
    """Reject control characters in a realm at construction (fail fast).

    CR / LF / NUL and other control characters cannot appear in an HTTP
    quoted-string and would corrupt the WWW-Authenticate header, so an
    invalid realm is a configuration error, surfaced when the scheme is
    built rather than on every 401.
    """
    if any(ord(c) < 0x20 or ord(c) == 0x7F for c in realm):
        raise ValueError("realm must not contain control characters")


def _extract_bearer_token(
    request: Any, scheme: str = AUTH_SCHEME_BEARER, auto_error: bool = True
) -> str | None:
    """Extract a bearer token from the Authorization header."""
    # The extraction itself lives in `_internal`, because the MCP HTTP transport
    # needs it too and reaching across a subpackage boundary for an
    # underscore-prefixed name is what that module exists to avoid. What stays
    # here is the part only a security scheme wants: the challenge.
    token = _bearer_token_from(request._peek_header_key(_AUTHORIZATION_KEY) or "", scheme)
    if token is None and auto_error:
        raise HTTPException(
            HTTP_401_UNAUTHORIZED,
            MSG_NOT_AUTHENTICATED,
            headers={HEADER_WWW_AUTHENTICATE: scheme},
        )
    return token


# The lowercase wire key, encoded once. `Request._peek_header_key` compares
# it against the raw header tuples, so re-encoding it per request would give
# back ~200 ns of the ~2.6 us the single-header read saves.
_AUTHORIZATION_KEY = HEADER_AUTHORIZATION.lower().encode("latin-1")


def _extract_api_key(
    source: Any,
    name: str,
    auto_error: bool = True,
    challenge: dict[str, str] | None = None,
) -> str | None:
    """Extract an API key from a dict-like source (headers, query, cookies).

    `challenge` is the precomputed WWW-Authenticate header dict (or `None`
    for no header); passing it straight through to `HTTPException` keeps
    the missing-key path a single branch with no per-request header build.
    """
    key = source.get(name)
    # `isspace()` tests for an all-whitespace key without allocating the
    # stripped copy `.strip()` would build on every (success-path) request;
    # `not key` already covers the empty/None case.
    if not key or key.isspace():
        if auto_error:
            raise HTTPException(HTTP_401_UNAUTHORIZED, MSG_NOT_AUTHENTICATED, headers=challenge)
        return None
    return key
