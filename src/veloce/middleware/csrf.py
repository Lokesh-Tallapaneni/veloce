"""CSRF protection — double-submit-cookie pattern.

Spec anchors:
- OWASP CSRF Cheat Sheet (Double Submit Cookie pattern, 2024 revision)
- RFC 6265 (cookie semantics)

The middleware keeps a random token in a `csrf_token` cookie and requires a
state-changing request to echo it in the `X-CSRF-Token` header or a
`csrf_token` form field; a request whose two copies disagree is refused with
403. Safe methods bypass the check entirely, because RFC 9110 Sec. 9.2.1 says
they MUST NOT have side effects.

The defence works because a cross-site request cannot read the `csrf_token`
cookie (Same-Origin Policy) and so cannot forge the matching header or field.
`SameSite=Lax`/`Strict` is additional belt-and-braces.

`secret` additionally HMAC-signs the cookie token, so an attacker who can plant
a cookie but cannot obtain a server-issued one (network/HTTP cookie injection, a
cookie-writing sibling subdomain) is refused. It does **not** stop an attacker
who can obtain their own valid token - bind the token to the authenticated
session for that. `max_age` bounds how long a leaked signed token stays
replayable.

`trusted_origins` runs an Origin-first stage **before** double-submit on
state-changing requests, closing the cookie-injection / related-domain class
pure double-submit cannot defend. The request's own origin, read from the ASGI
scope rather than spoofable headers, is always trusted; entries are full origins
(`"https://app.example.com"`), and a leading-dot host (`"https://.example.com"`)
trusts that host and any subdomain. A present-but-mismatched `Origin` is a hard
403; with no `Origin` the stage falls back to `Referer` only on https, where
browsers always send one. Double-submit always still runs, so a single Origin
bypass is not sufficient.
"""

from __future__ import annotations

import secrets
from collections.abc import Callable
from urllib.parse import urlsplit

import veloce.status as status
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
    HTTP_METHOD_QUERY,
    HTTP_METHOD_TRACE,
    URL_SCHEME_HTTPS,
)
from veloce.http.request import Request
from veloce.http.response import JSONResponse, Response
from veloce.middleware.base import Middleware
from veloce.safe import constant_time_compare
from veloce.signing import BadSignature, Signer


class CSRFMiddleware(Middleware):
    """Double-submit-cookie CSRF middleware.

    Issues a token cookie and requires an unsafe request to echo it back in a
    header or form field; a request whose two copies disagree is refused.

    `cookie_name` / `header_name` / `form_field` rename the slots the token
    travels in, and `safe_methods` overrides which verbs bypass the check.
    `cookie_secure` / `cookie_httponly` / `cookie_samesite` set the cookie
    attributes - `httponly` must stay `False` because client-side JavaScript
    has to read the cookie to echo it, while `secure` defaults to `True`, so
    local HTTP development needs `cookie_secure=False`. Setting `secret`
    additionally HMAC-signs the token, which proves the value was minted by
    this server; the module docstring covers what that does and does not stop.

    Usage::

        from veloce import Veloce
        from veloce.middleware.csrf import CSRFMiddleware

        app = Veloce()
        app.add_middleware(CSRFMiddleware(secret="a-long-random-string"))
    """

    def __init__(
        self,
        cookie_name: str = "csrf_token",
        header_name: str = HEADER_X_CSRF_TOKEN,
        form_field: str = "csrf_token",
        safe_methods: tuple[str, ...] = (
            HTTP_METHOD_GET,
            HTTP_METHOD_HEAD,
            HTTP_METHOD_OPTIONS,
            HTTP_METHOD_QUERY,
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
        # A frozenset for the per-request membership probe.
        self.safe_methods = frozenset(m.upper() for m in safe_methods)
        self.cookie_secure = cookie_secure
        self.cookie_httponly = cookie_httponly
        self.cookie_samesite = cookie_samesite
        self.token_factory = token_factory or (lambda: secrets.token_urlsafe(32))
        # Split once, so the per-request comparison is not a parse.
        self._trusted: tuple[tuple[str, str | None, str | None], ...] = ()
        if trusted_origins:
            parsed: list[tuple[str, str | None, str | None]] = []
            for origin in trusted_origins:
                split = urlsplit(origin)
                scheme = split.scheme.lower()
                # So `:443`/`:80` still matches a browser header that omits it.
                host = strip_default_port(scheme, split.netloc.lower())
                if host.startswith("."):
                    parsed.append((scheme, None, host[1:]))
                else:
                    parsed.append((scheme, host, None))
            self._trusted = tuple(parsed)
        # A cookie token carrying no valid server signature fails verification
        # even when the attacker also echoes it in the header.
        self._max_age = max_age
        self._signer: Signer | None = None
        if secret:
            self._signer = Signer(secret, salt="veloce.csrf")

    async def process_request(self, request: Request) -> Response | None:
        """Validate the CSRF token on state-changing requests."""
        # CSRF defends a browser that attaches a cookie automatically; an MCP
        # call has neither.
        if request.is_mcp:
            return None
        # New tokens are minted in `process_response` when the cookie is missing.
        existing = request.cookies.get(self.cookie_name)
        request._state["_csrf_cookie"] = existing

        # `request.method` is already upper-cased at construction.
        if request.method in self.safe_methods:
            return None

        # Rests on the transport-derived scheme/host, which an attacker who can
        # only plant a cookie cannot forge.
        if self._trusted:
            denied = self._verify_origin(request)
            if denied is not None:
                return denied

        cookie_val = existing
        if not cookie_val:
            return self._forbidden("CSRF cookie missing")
        # A valid signature is required before double-submit is trusted.
        if self._signer is not None:
            try:
                self._signer.loads(cookie_val, max_age=self._max_age)
            except BadSignature:
                # `process_response` skips a cookie merely present, so a planted
                # value would otherwise refuse every write from this browser.
                request._state["_csrf_rotate"] = True
                return self._forbidden("CSRF cookie signature invalid or expired")
        # Case-insensitive header lookup (Headers is CIMultiDict).
        header_val = request.headers.get(self.header_name)
        if self._matches(header_val, cookie_val):
            return None
        # Form-shaped bodies only, so a JSON path cannot fail on a parse error.
        ct = request.headers.get(HEADER_CONTENT_TYPE, "")
        if MIME_FORM_URLENCODED in ct or MIME_MULTIPART_FORM_DATA in ct:
            try:
                form = await request.form()
            except Exception:
                # No form-field token available: the mismatch verdict, not a 500.
                form = None
            # Multipart parts can resolve to UploadFile; only a string echoes the cookie.
            if form is not None and self._matches(form.get(self.form_field), cookie_val):
                return None

        return self._forbidden("CSRF token mismatch")

    async def process_response(self, request: Request, response: Response) -> Response:
        """Set or rotate the CSRF cookie."""
        existing = request._state.get("_csrf_cookie")
        # Without this, an anonymous session's CSRF cookie would persist across
        # login - a session-fixation pathway.
        force_rotate = bool(request._state.get("_csrf_rotate"))
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
        # The Origin/Referer netloc carries the port, so compare the authority.
        host = request.host.lower()
        own_scheme = scheme
        own_host = host

        origin = request.headers.get(HEADER_ORIGIN)
        if origin is not None:
            # The browser sets `Origin` on every cross-origin and every unsafe
            # same-origin request, so a mismatch here is a forgery.
            if self._origin_allowed(origin, own_scheme, own_host):
                return None
            return self._forbidden("CSRF origin mismatch")

        # On https a browser still sends `Referer`, so a missing or foreign one is
        # a rejection; on plain http it is an API client, and double-submit runs.
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
        # Gated on a string, so an `UploadFile` part never reaches the compare.
        if not isinstance(candidate, str):
            return False
        try:
            return secrets.compare_digest(candidate, cookie_val)
        except TypeError:
            # `compare_digest` raises on a `str` holding non-ASCII, and both the
            # header and the form field are caller-controlled, so a forged token
            # with one non-ASCII byte would answer 500 rather than 403. Kept in
            # the `except` so the ASCII path pays nothing for the guard.
            return constant_time_compare(candidate, cookie_val)

    def _forbidden(self, detail: str) -> Response:
        return JSONResponse(
            {"detail": detail},
            status_code=status.HTTP_403_FORBIDDEN,
        )


def rotate_csrf_token(request: Request) -> None:
    """Force the active `CSRFMiddleware` to mint a fresh token on response.

    Call this at the end of an authentication handler (login, logout,
    permission elevation). Without rotation an attacker who plants a known CSRF
    cookie on an anonymous victim can submit forged requests after the victim
    logs in - a session-fixation pathway.

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
