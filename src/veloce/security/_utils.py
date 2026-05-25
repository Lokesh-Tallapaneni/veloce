"""Shared helpers for security schemes."""

from __future__ import annotations

from typing import Any

from veloce.exceptions import HTTPException


def _extract_bearer_token(
    request: Any, scheme: str = "Bearer", auto_error: bool = True
) -> str | None:
    """Extract a bearer token from the Authorization header."""
    auth = request.headers.get("authorization", "")
    prefix = f"{scheme} "
    if auth[: len(prefix)].lower() != prefix.lower():
        if auto_error:
            raise HTTPException(401, "Not authenticated", headers={"WWW-Authenticate": scheme})
        return None
    return auth[len(prefix) :]


def _extract_api_key(source: Any, name: str, auto_error: bool = True) -> str | None:
    """Extract an API key from a dict-like source (headers, query, cookies)."""
    key = source.get(name)
    if key is None:
        if auto_error:
            raise HTTPException(401, "Not authenticated")
        return None
    return key
