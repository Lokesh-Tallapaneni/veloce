"""CSRF protection — double-submit-cookie pattern.

Spec anchors:
- OWASP CSRF Cheat Sheet (Double Submit Cookie pattern, 2024 revision)
- RFC 6265 (cookie semantics)

The double-submit-cookie pattern is one of two OWASP-recommended CSRF
defences (the other being the synchronizer-token pattern, which
requires server-side state). It works like this:

1. On every request, the middleware ensures there's a CSRF token in
   a cookie named `csrf_token` (random per session - generated on
   first request when missing).
2. For state-changing methods (`POST`/`PUT`/`PATCH`/`DELETE`), the
   client must echo the cookie value in a header (`X-CSRF-Token` by
   default) **or** in a form field (`csrf_token`). If neither matches
   the cookie, the request is refused with 403.
3. Safe methods (`GET`/`HEAD`/`OPTIONS`/`TRACE`) bypass the check
   entirely - RFC 9110 Sec. 9.2.1 spec semantics: they MUST NOT have
   side effects.

The defence works because a cross-site request can't read the
`csrf_token` cookie (Same-Origin Policy) so it can't forge the
matching header / form field. SameSite=Lax/Strict cookies are
additional belt-and-braces.

Veloce-specific knobs:
- `cookie_name` / `header_name` / `form_field` - rename the slots.
- `safe_methods` - override the bypass set.
- `cookie_secure` / `cookie_httponly` / `cookie_samesite` - cookie
  attribute flags. Default `httponly=False` is required (the
  *client-side* JS must read the cookie to echo it in the header);
  `secure` defaults to `True` so the cookie is never sent over plain
  HTTP - pass `cookie_secure=False` for local HTTP development.
- `secret` - when set, the token in the cookie is HMAC-signed. The
  double-submit equality check still applies; the signature additionally
  proves the value was minted by this server, so an attacker who can
  plant a cookie but cannot obtain a server-issued token (network/HTTP
  cookie injection, a cookie-writing sibling subdomain) is refused.
  Signing alone does **not** stop an attacker who can obtain their own
  valid token - bind the token to the authenticated session for that.
- `max_age` - when set together with `secret`, a signed token older
  than this many seconds is rejected, bounding how long a leaked token
  stays replayable.
- `token_factory` - overridable for tests; default
  `secrets.token_urlsafe(32)`.
- `trusted_origins` - when set, an Origin-first verification stage runs
  **before** the double-submit check on state-changing requests. The
  request's own origin (`scheme://host[:port]`, sourced from the ASGI
  scope rather than spoofable headers) is always trusted; additional
  cross-origin callers are listed here as full origins
  (`"https://app.example.com"`). A leading-dot host wildcard
  (`"https://.example.com"`) trusts that host and any subdomain of it.
  A present-but-mismatched `Origin` header is a hard 403. When `Origin`
  is absent the stage falls back to `Referer` only on https requests
  (where browsers always send one); plain-HTTP requests with no Origin -
  typical of non-browser API clients - skip straight to double-submit.
  Double-submit always still runs, so a single Origin bypass is not
  sufficient. This closes the cookie-injection / related-domain CSRF
  class that pure double-submit cannot defend.
"""

from __future__ import annotations

import secrets
from collections.abc import Callable
from urllib.parse import urlsplit

from veloce import status
from veloce._constants import (
    HEADER_CONTENT_TYPE,
    HEADER_ORIGIN,
    HEADER_REFERER,
    HEADER_X_CSRF_TOKEN,
    MIME_FORM_URLENCODED,
    MIME_MULTIPART_FORM_DATA,
)
from veloce._internal import strip_default_port
from veloce._protocol_constants import (
    HTTP_METHOD_GET,
    HTTP_METHOD_HEAD,
    HTTP_METHOD_OPTIONS,
    HTTP_METHOD_TRACE,
    URL_SCHEME_HTTPS,
)
from veloce.http.request import Request
from veloce.http.response import JSONResponse, Response
from veloce.middleware.base import Middleware
from veloce.signing import BadSignature, Signer


class CSRFMiddleware(Middleware):
    """Double-submit-cookie CSRF middleware."""

    def __init__(
        self,
        cookie_name: str = "csrf_token",
        header_name: str = HEADER_X_CSRF_TOKEN,
        form_field: str = "csrf_token",
        safe_methods: tuple[str, ...] = (
            HTTP_METHOD_GET,
            HTTP_METHOD_HEAD,
            HTTP_METHOD_OPTIONS,
            HTTP_METHOD_TRACE,
        ),
        cookie_secure: bool = True,
        cookie_httponly: bool = False,
        cookie_samesite: str = "Lax",
        token_factory: Callable[[], str] | None = None,
        secret: str | None = None,
        max_age: int | None = None,
        trusted_origins: tuple[str, ...] | None = None,
        name: str | None = None,
    ) -> None:
        super().__init__(name=name)
        self.cookie_name = cookie_name
        self.header_name = header_name
        self.form_field = form_field
        # A frozenset for the per-request membership probe (it is only ever
        # tested with `in`, never iterated), mirroring `TrustedHostMiddleware`.
        self.safe_methods = frozenset(m.upper() for m in safe_methods)
        self.cookie_secure = cookie_secure
        self.cookie_httponly = cookie_httponly
        self.cookie_samesite = cookie_samesite
        self.token_factory = token_factory or (lambda: secrets.token_urlsafe(32))
        # Origin allow-list. An empty list keeps the legacy pure
        # double-submit behaviour; a non-empty list activates the
        # Origin-first stage. Each entry is split once at construction
        # into (scheme, exact-host-or-None, suffix-or-None) so the
        # per-request comparison is a couple of cheap string equalities
        # rather than a parse. A leading-dot host such as
        # ``https://.example.com`` matches the bare host and any
        # subdomain via a suffix test.
        self._trusted: tuple[tuple[str, str | None, str | None], ...] = ()
        if trusted_origins:
            parsed: list[tuple[str, str | None, str | None]] = []
            for origin in trusted_origins:
                split = urlsplit(origin)
                scheme = split.scheme.lower()
                # Drop a default port here too, so a configured wildcard or
                # exact origin written with `:443`/`:80` still matches a
                # browser Origin/Referer that omits it.
                host = strip_default_port(scheme, split.netloc.lower())
                if host.startswith("."):
                    parsed.append((scheme, None, host[1:]))
                else:
                    parsed.append((scheme, host, None))
            self._trusted = tuple(parsed)
        # When a `secret` is supplied the cookie token is HMAC-signed: a
        # value carrying no valid server signature fails verification
        # even when the attacker also echoes it in the header.
        self._max_age = max_age
        self._signer: Signer | None = None
        if secret:
            self._signer = Signer(secret, salt="veloce.csrf")

    async def process_request(self, request: Request) -> Response | None:
        """Validate the CSRF token on state-changing requests."""
        # Stash existing cookie value (or None) on request._state for the
        # response phase. New tokens are minted in process_response when
        # the cookie is missing.
        existing = request.cookies.get(self.cookie_name)
        request._state["_csrf_cookie"] = existing

        # `request.method` is already upper-cased at construction.
        if request.method in self.safe_methods:
            return None

        # Origin-first stage (active only when `trusted_origins` is set):
        # confirm the request was issued from a trusted origin before the
        # double-submit equality is even consulted. This rests on the
        # transport-derived scheme/host, which an attacker who can only
        # plant a cookie cannot forge.
        if self._trusted:
            denied = self._verify_origin(request)
            if denied is not None:
                return denied

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
        # the body looks form-shaped - JSON / multipart-without-form-field
        # paths shouldn't fail the check on a parse error.
        ct = request.headers.get(HEADER_CONTENT_TYPE, "")
        if MIME_FORM_URLENCODED in ct or MIME_MULTIPART_FORM_DATA in ct:
            try:
                form = await request.form()
            except Exception:
                # A malformed or truncated body must not crash the CSRF
                # check: any parse failure simply means no form-field token
                # is available, so fall through to the mismatch verdict
                # below rather than surfacing the parser error.
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
        # login - a session-fixation pathway.
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

    def _verify_origin(self, request: Request) -> Response | None:
        """Return a 403 Response if the request's origin is untrusted, else None."""
        scheme = request.scheme.lower()
        # `request.host` is the Host header verbatim (with port); the
        # Origin/Referer netloc carries the port too, so compare on the
        # full authority rather than the port-stripped host.
        host = request.host.lower()
        own_scheme = scheme
        own_host = host

        origin = request.headers.get(HEADER_ORIGIN)
        if origin is not None:
            # A present Origin is authoritative: it must match. The
            # browser sets it on every cross-origin and every unsafe
            # same-origin request, so a mismatch here is a forgery.
            if self._origin_allowed(origin, own_scheme, own_host):
                return None
            return self._forbidden("CSRF origin mismatch")

        # No Origin header. On https a browser still sends Referer, so a
        # missing/foreign one is suspicious enough to reject. On plain
        # http we cannot distinguish a stripped Referer from a non-browser
        # API client, so we defer to the double-submit factor instead.
        if scheme != URL_SCHEME_HTTPS:
            return None

        referer = request.headers.get(HEADER_REFERER)
        if not referer:
            return self._forbidden("CSRF referer missing")
        split = urlsplit(referer)
        if not split.scheme or not split.netloc:
            return self._forbidden("CSRF referer malformed")
        if split.scheme.lower() != URL_SCHEME_HTTPS:
            return self._forbidden("CSRF referer insecure")
        referer_origin = f"{split.scheme.lower()}://{split.netloc.lower()}"
        if self._origin_allowed(referer_origin, own_scheme, own_host):
            return None
        return self._forbidden("CSRF referer mismatch")

    def _origin_allowed(self, origin: str, own_scheme: str, own_host: str) -> bool:
        """True if `origin` is the request's own origin or a trusted entry."""
        split = urlsplit(origin)
        o_scheme = split.scheme.lower()
        o_host = strip_default_port(o_scheme, split.netloc.lower())
        if not o_scheme or not o_host:
            return False
        if o_scheme == own_scheme and o_host == strip_default_port(own_scheme, own_host):
            return True
        for t_scheme, t_host, t_suffix in self._trusted:
            if o_scheme != t_scheme:
                continue
            if t_host is not None and o_host == strip_default_port(t_scheme, t_host):
                return True
            if t_suffix is not None and (o_host == t_suffix or o_host.endswith("." + t_suffix)):
                return True
        return False

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
