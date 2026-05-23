"""API Key authentication schemes — header, query, cookie."""

from __future__ import annotations

from veloce.exceptions import HTTPException
from veloce.http.request import Request


class APIKeyHeader:
    """API Key authentication via HTTP header."""

    def __init__(self, name: str, auto_error: bool = True) -> None:
        # Keep the user's casing for the OpenAPI spec; header lookup goes
        # through the case-insensitive `Headers` (CIMultiDict) so the case
        # doesn't matter at read time.
        self.name = name
        self.auto_error = auto_error

    def __call__(self, request: Request) -> str | None:
        key = request.headers.get(self.name)
        if key is None:
            if self.auto_error:
                raise HTTPException(401, "Not authenticated")
            return None
        return key


class APIKeyQuery:
    """API Key authentication via query parameter."""

    def __init__(self, name: str, auto_error: bool = True) -> None:
        self.name = name
        self.auto_error = auto_error

    def __call__(self, request: Request) -> str | None:
        key = request.query_params.get(self.name)
        if key is None:
            if self.auto_error:
                raise HTTPException(401, "Not authenticated")
            return None
        return key


class APIKeyCookie:
    """API Key authentication via cookie."""

    def __init__(self, name: str, auto_error: bool = True) -> None:
        self.name = name
        self.auto_error = auto_error

    def __call__(self, request: Request) -> str | None:
        key = request.cookies.get(self.name)
        if key is None:
            if self.auto_error:
                raise HTTPException(401, "Not authenticated")
            return None
        return key
