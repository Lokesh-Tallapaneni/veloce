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
  double-submit equality check still applies; the signature additionally
  proves the value was minted by this server, so an attacker who can
  plant a cookie but cannot obtain a server-issued token (network/HTTP
  cookie injection, a cookie-writing sibling subdomain) is refused.
  Signing alone does **not** stop an attacker who can obtain their own
  valid token — bind the token to the authenticated session for that.
- `max_age` — when set together with `secret`, a signed token older
  than this many seconds is rejected, bounding how long a leaked token
  stays replayable.
- `token_factory` — overridable for tests; default
  `secrets.token_urlsafe(32)`.
"""

from __future__ import annotations

import secrets
from typing import Any

from veloce import status
from veloce._constants import MIME_FORM_URLENCODED, MIME_MULTIPART_FORM_DATA
from veloce.http.request import Request
from veloce.http.response import JSONResponse, Response
from veloce.middleware.base import Middleware
from veloce.signing import BadSignature, Signer


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
        max_age: int | None = None,
    ) -> None:
        self.cookie_name = cookie_name
        self.header_name = header_name
        self.form_field = form_field
        self.safe_methods = tuple(m.upper() for m in safe_methods)
        self.cookie_secure = cookie_secure
        self.cookie_httponly = cookie_httponly
        self.cookie_samesite = cookie_samesite
        self.token_factory = token_factory or (lambda: secrets.token_urlsafe(32))
        # When a `secret` is supplied the cookie token is HMAC-signed: a
        # value carrying no valid server signature fails verification
        # even when the attacker also echoes it in the header.
        self._max_age = max_age
        self._signer: Any = None
        if secret:
            self._signer = Signer(secret, salt="veloce.csrf")

    async def process_request(self, request: Request) -> Response | None:
        """Validate the CSRF token on state-changing requests."""
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
        # With signing enabled, the cookie value must additionally carry
        # a valid (and, when `max_age` is set, unexpired) server
        # signature before the double-submit comparison is trusted.
        if self._signer is not None:
            try:
                self._signer.loads(cookie_val, max_age=self._max_age)
            except BadSignature:
                return self._forbidden("CSRF cookie signature invalid or expired")
        # Case-insensitive header lookup (Headers is CIMultiDict).
        header_val = request.headers.get(self.header_name)
        if self._matches(header_val, cookie_val):
            return None
        # Fall back to form-field check. Only consult `request.form` when
        # the body looks form-shaped — JSON / multipart-without-form-field
        # paths shouldn't fail the check on a parse error.
        ct = request.headers.get("content-type", "")
        if MIME_FORM_URLENCODED in ct or MIME_MULTIPART_FORM_DATA in ct:
            try:
                form = await request.form()
            except Exception:
                form = None
            # Multipart parts can resolve to UploadFile; only a string echoes the cookie.
            if form is not None and self._matches(form.get(self.form_field), cookie_val):
                return None

        return self._forbidden("CSRF token mismatch")

    async def process_response(self, request: Request, response: Response) -> Response:
        """Set or rotate the CSRF cookie."""
        existing = request._state.get("_csrf_cookie") if request._state else None
        # `rotate_csrf_token()` sets this sentinel on the request state to
        # force a fresh token regardless of an existing cookie. Without
        # this, an anonymous session's CSRF cookie would persist across
        # login — a session-fixation pathway.
        force_rotate = bool(request._state.get("_csrf_rotate") if request._state else False)
        if existing and not force_rotate:
            return response
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

    @staticmethod
    def _matches(candidate: object, cookie_val: str) -> bool:
        # Constant-time equality, gated on the candidate being a string so
        # multipart UploadFile parts and missing fields don't reach
        # `compare_digest` (which would raise on non-bytes/str).
        return isinstance(candidate, str) and secrets.compare_digest(candidate, cookie_val)

    def _forbidden(self, detail: str) -> Response:
        return JSONResponse(
            {"detail": detail},
            status_code=status.HTTP_403_FORBIDDEN,
        )


def rotate_csrf_token(request: Request) -> None:
    """Force the active `CSRFMiddleware` to mint a fresh token on response.

    Call this at the end of an authentication handler (login, logout,
    permission elevation) so the CSRF cookie issued to the
    pre-authentication session is replaced by a fresh one bound to the
    new authentication state. Without rotation an attacker who plants
    a known CSRF cookie on an anonymous victim can submit forged
    requests after the victim logs in (session-fixation pathway).

    Usage::

        @app.post("/login")
        async def login(request: Request):
            user = authenticate(...)
            request.session["user_id"] = user.id
            rotate_csrf_token(request)
            return RedirectResponse("/")

    No-op when `CSRFMiddleware` is not installed.
    """
    request._state["_csrf_rotate"] = True
