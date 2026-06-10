"""Security utilities — shared credential extraction helpers for auth schemes."""

from __future__ import annotations

from typing import Any

from veloce._constants import HEADER_AUTHORIZATION, HEADER_WWW_AUTHENTICATE, MSG_NOT_AUTHENTICATED
from veloce._protocol_constants import AUTH_SCHEME_BEARER
from veloce.exceptions import HTTPException
from veloce.status import HTTP_401_UNAUTHORIZED

_BEARER_PREFIX = AUTH_SCHEME_BEARER + " "
_BEARER_PREFIX_LOWER = _BEARER_PREFIX.lower()
_BEARER_PREFIX_LEN = len(_BEARER_PREFIX)


def _quote_header_value(value: str) -> str:
    """Escape a string for an HTTP quoted-string (RFC 7230 Sec. 3.2.6).

    Backslash must be escaped before the double-quote, or a literal
    backslash preceding a quote would be mis-escaped. This is the correct
    transform for a `realm` and other WWW-Authenticate quoted params -
    not `urllib.parse.quote`, which percent-encodes and mangles `@`/space.
    """
    return value.replace("\\", "\\\\").replace('"', '\\"')


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
    auth = request.headers.get(HEADER_AUTHORIZATION, "")
    # The default "Bearer" prefix is precomputed; only a custom scheme name
    # pays for per-call prefix construction.
    if scheme == AUTH_SCHEME_BEARER:
        prefix_len = _BEARER_PREFIX_LEN
        prefix_lower = _BEARER_PREFIX_LOWER
    else:
        prefix = scheme + " "
        prefix_len = len(prefix)
        prefix_lower = prefix.lower()
    if auth[:prefix_len].lower() != prefix_lower:
        if auto_error:
            raise HTTPException(
                HTTP_401_UNAUTHORIZED,
                MSG_NOT_AUTHENTICATED,
                headers={HEADER_WWW_AUTHENTICATE: scheme},
            )
        return None
    # RFC 6750 section 2.1 + RFC 7235: only SP/HTAB are permitted between
    # scheme and token. Do not trim other Unicode whitespace (NBSP, \n, \r, ...).
    token = auth[prefix_len:].strip(" \t")
    if not token:
        if auto_error:
            raise HTTPException(
                HTTP_401_UNAUTHORIZED,
                MSG_NOT_AUTHENTICATED,
                headers={HEADER_WWW_AUTHENTICATE: scheme},
            )
        return None
    return token


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
