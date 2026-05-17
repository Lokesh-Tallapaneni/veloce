"""Cookie-based session middleware — signed + timestamped payload.

The cookie body is produced by `veloce.signing.Signer`, which embeds a
server-side timestamp so `max_age` can be enforced on the server (the
cookie's own `Max-Age` directive is only a client hint — an attacker
can replay an old cookie past its expiry, so we never trust it as the
sole expiry signal).

Secret rotation: pass a list of secrets — the first signs new cookies,
the rest are accepted on read for the rotation window. Old cookies
keep validating until they age out.
"""

from __future__ import annotations

import json
from typing import Any

from veloce.http.request import Request
from veloce.http.response import Response
from veloce.middleware.base import Middleware
from veloce.sessions import Session
from veloce.signing import BadSignature, Signer


class SessionMiddleware(Middleware):
    """Server-side session stored in a signed, timestamped cookie."""

    def __init__(
        self,
        secret_key: str | list[str],
        cookie_name: str = "session",
        max_age: int = 86400 * 14,
        path: str = "/",
        httponly: bool = True,
        secure: bool = False,
        samesite: str = "lax",
        permanent_lifetime: int = 86400 * 31,
    ) -> None:
        keys = [secret_key] if isinstance(secret_key, str) else list(secret_key)
        if not keys:
            raise ValueError("secret_key must be a non-empty string or list of strings")
        self._signer = Signer(keys[0], salt="veloce.session")
        for fallback in keys[1:]:
            self._signer.add_fallback_secret(fallback, salt="veloce.session")
        self.cookie_name = cookie_name
        self.max_age = max_age
        self.path = path
        self.httponly = httponly
        self.secure = secure
        self.samesite = samesite
        # `PERMANENT_SESSION_LIFETIME` analog — used for the cookie
        # `Max-Age` when `session.permanent` is set. Defaults to 31 days.
        self.permanent_lifetime = permanent_lifetime

    async def process_request(self, request: Request) -> Response | None:
        session_data: dict[str, Any] = {}
        is_new = True
        cookie_val = request.cookies.get(self.cookie_name)
        if cookie_val:
            try:
                # Read with the longer window so a permanent cookie is
                # not rejected; a non-permanent cookie's shorter
                # `Max-Age` already evicts it client-side.
                decoded = self._signer.loads(
                    cookie_val, max_age=max(self.max_age, self.permanent_lifetime)
                )
            except BadSignature:
                decoded = None
            if isinstance(decoded, dict):
                session_data = decoded
                is_new = False
        session = Session(session_data)
        # `new` is True when the request carried no valid session cookie.
        session.new = is_new
        request._state["session"] = session
        # Snapshot canonical form so process_response can detect mutation
        # without re-signing on every response (signing is the expensive bit).
        request._state["_session_original"] = json.dumps(session_data, sort_keys=True)
        return None

    async def process_response(self, request: Request, response: Response) -> Response:
        session = request._state.get("session", {})
        original = request._state.get("_session_original", "{}")
        current = json.dumps(session, sort_keys=True)
        if current == original:
            return response

        cookie_value = self._signer.dumps(session)
        # A `permanent` session uses the longer lifetime for `Max-Age`.
        lifetime = self.permanent_lifetime if getattr(session, "permanent", False) else self.max_age
        cookie = f"{self.cookie_name}={cookie_value}; Path={self.path}; Max-Age={lifetime}"
        if self.httponly:
            cookie += "; HttpOnly"
        if self.secure:
            cookie += "; Secure"
        if self.samesite:
            cookie += f"; SameSite={self.samesite}"
        response.headers["Set-Cookie"] = cookie
        response._encoded = None
        return response
