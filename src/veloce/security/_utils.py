"""Security utilities - shared credential extraction helpers for auth schemes."""

from __future__ import annotations

from typing import Any

from veloce._constants import HEADER_AUTHORIZATION, HEADER_WWW_AUTHENTICATE, MSG_NOT_AUTHENTICATED
from veloce._protocol_constants import AUTH_SCHEME_BEARER
from veloce.exceptions import HTTPException
from veloce.status import HTTP_401_UNAUTHORIZED


def _extract_bearer_token(
    request: Any, scheme: str = AUTH_SCHEME_BEARER, auto_error: bool = True
) -> str | None:
    """Extract a bearer token from the Authorization header."""
    auth = request.headers.get(HEADER_AUTHORIZATION, "")
    prefix = f"{scheme} "
    if auth[: len(prefix)].lower() != prefix.lower():
        if auto_error:
            raise HTTPException(
                HTTP_401_UNAUTHORIZED,
                MSG_NOT_AUTHENTICATED,
                headers={HEADER_WWW_AUTHENTICATE: scheme},
            )
        return None
    # RFC 6750 section 2.1 + RFC 7235: only SP/HTAB are permitted between
    # scheme and token. Do not trim other Unicode whitespace (NBSP, \n, \r, ...).
    token = auth[len(prefix) :].strip(" \t")
    if not token:
        if auto_error:
            raise HTTPException(
                HTTP_401_UNAUTHORIZED,
                MSG_NOT_AUTHENTICATED,
                headers={HEADER_WWW_AUTHENTICATE: scheme},
            )
        return None
    return token


def _extract_api_key(source: Any, name: str, auto_error: bool = True) -> str | None:
    """Extract an API key from a dict-like source (headers, query, cookies)."""
    key = source.get(name)
    if not key or not key.strip():
        if auto_error:
            raise HTTPException(HTTP_401_UNAUTHORIZED, MSG_NOT_AUTHENTICATED)
        return None
    return key
