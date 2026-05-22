"""CSRF protection — double-submit-cookie pattern.

Spec anchors:
- OWASP CSRF Cheat Sheet (Double Submit Cookie pattern, 2024 revision)
- RFC 6265 (cookie semantics)

The double-submit-cookie pattern is one of two OWASP-recommended CSRF
defences (the other being the synchronizer-token pattern, which
requires server-side state). It works like this:

1. On every request, the middleware ensures there's a CSRF token in
   a cookie named `csrf_token` (random per session — generated on
   first request when missing).
2. For state-changing methods (`POST`/`PUT`/`PATCH`/`DELETE`), the
   client must echo the cookie value in a header (`X-CSRF-Token` by
   default) **or** in a form field (`csrf_token`). If neither matches
   the cookie, the request is refused with 403.
3. Safe methods (`GET`/`HEAD`/`OPTIONS`/`TRACE`) bypass the check
   entirely — RFC 9110 §9.2.1 spec semantics: they MUST NOT have
   side effects.

The defence works because a cross-site request can't read the
`csrf_token` cookie (Same-Origin Policy) so it can't forge the
matching header / form field. SameSite=Lax/Strict cookies are
additional belt-and-braces.

Veloce-specific knobs:
- `cookie_name` / `header_name` / `form_field` — rename the slots.
- `safe_methods` — override the bypass set.
- `cookie_secure` / `cookie_httponly` / `cookie_samesite` — cookie
  attribute flags. Default `httponly=False` is required (the
  *client-side* JS must read the cookie to echo it in the header);
  `secure` defaults to `True` so the cookie is never sent over plain
  HTTP — pass `cookie_secure=False` for local HTTP development.
- `secret` — when set, the token in the cookie is HMAC-signed. The
  double-submit equality check still applies, but the signature also
  proves the value was minted by this server, closing the
  cookie-injection gap where an attacker able to write the victim's
  `csrf_token` cookie could otherwise also forge the matching header.
- `token_factory` — overridable for tests; default
  `secrets.token_urlsafe(32)`.
"""

from __future__ import annotations

import secrets
from typing import Any

from veloce.http.request import Request
from veloce.http.response import Response
from veloce.middleware.base import Middleware


class CSRFMiddleware(Middleware):
    """Double-submit-cookie CSRF middleware."""

    def __init__(
        self,
        cookie_name: str = "csrf_token",
        header_name: str = "X-CSRF-Token",
        form_field: str = "csrf_token",
        safe_methods: tuple[str, ...] = ("GET", "HEAD", "OPTIONS", "TRACE"),
        cookie_secure: bool = True,
        cookie_httponly: bool = False,
        cookie_samesite: str = "Lax",
        token_factory: Any = None,
        secret: str | None = None,
    ) -> None:
        self.cookie_name = cookie_name
        self.header_name = header_name
        self.form_field = form_field
        self.safe_methods = tuple(m.upper() for m in safe_methods)
        self.cookie_secure = cookie_secure
        self.cookie_httponly = cookie_httponly
        self.cookie_samesite = cookie_samesite
        self.token_factory = token_factory or (lambda: secrets.token_urlsafe(32))
        # When a `secret` is supplied the cookie token is HMAC-signed, so
        # a planted (cookie-injected) value fails verification even when
        # the attacker also controls the echoed header.
        self._signer: Any = None
        if secret:
            from veloce.signing import Signer

            self._signer = Signer(secret, salt="veloce.csrf")

    async def process_request(self, request: Request) -> Response | None:
        # Stash existing cookie value (or None) on request._state for the
        # response phase. New tokens are minted in process_response when
        # the cookie is missing.
        existing = request.cookies.get(self.cookie_name)
        request._state["_csrf_cookie"] = existing

        if request.method.upper() in self.safe_methods:
            return None

        # Verification: cookie value must match header OR form field.
        cookie_val = existing
        if not cookie_val:
            return self._forbidden("CSRF cookie missing")
        # With signing enabled, the cookie value must additionally carry a
        # valid server signature — an injected cookie can't produce one.
        if self._signer is not None:
            from veloce.signing import BadSignature

            try:
                self._signer.loads(cookie_val)
            except BadSignature:
                return self._forbidden("CSRF cookie signature invalid")
        # Case-insensitive header lookup (Headers is CIMultiDict).
        header_val = request.headers.get(self.header_name)
        if header_val and secrets.compare_digest(header_val, cookie_val):
            return None
        # Fall back to form-field check. Only consult `request.form` when
        # the body looks form-shaped — JSON / multipart-without-form-field
        # paths shouldn't fail the check on a parse error.
        ct = request.headers.get("content-type", "")
        if "application/x-www-form-urlencoded" in ct or "multipart/form-data" in ct:
            try:
                form = await request.form()
            except Exception:
                form = None
            if form is not None:
                form_val = form.get(self.form_field)
                if form_val and secrets.compare_digest(form_val, cookie_val):
                    return None

        return self._forbidden("CSRF token mismatch")

    async def process_response(self, request: Request, response: Response) -> Response:
        existing = request._state.get("_csrf_cookie") if request._state else None
        if existing:
            return response
        # First request — mint a token and set the cookie. When signing
        # is enabled the stored value is the signed token.
        token = self.token_factory()
        if self._signer is not None:
            token = self._signer.dumps(token)
        response.set_cookie(
            self.cookie_name,
            token,
            path="/",
            secure=self.cookie_secure,
            httponly=self.cookie_httponly,
            samesite=self.cookie_samesite,
        )
        return response

    def _forbidden(self, detail: str) -> Response:
        from veloce.http.response import JSONResponse

        return JSONResponse(
            {"detail": detail},
            status_code=403,
        )
