"""CORS middleware — Cross-Origin Resource Sharing per the Fetch standard.

Implemented from the Fetch standard's CORS protocol section
(https://fetch.spec.whatwg.org/#http-cors-protocol) and RFC 9110 Sec. 10.2.
Names match the spec; semantics are veloce's own.

Notable spec rules this middleware enforces:

- `Access-Control-Allow-Origin: *` MUST NOT be combined with
  `Access-Control-Allow-Credentials: true`. When credentials are allowed,
  the response must echo the exact request origin (and Vary on Origin
  so caches key by origin).
- Whenever the allowed origin depends on the request origin, the
  response MUST include `Vary: Origin` to prevent cache poisoning.
- Preflight (`OPTIONS` with `Access-Control-Request-Method`) returns
  204 with the negotiated set; a preflight whose origin or requested
  method is disallowed gets a diagnostic 400 instead so the rejection
  is visible to developers (the browser would block it either way).
- Private Network Access: when `allow_private_network=True` and a
  preflight carries `Access-Control-Request-Private-Network: true`, the
  response echoes `Access-Control-Allow-Private-Network: true`. The grant
  is opt-in and never emitted otherwise.
"""

from __future__ import annotations

import re
from re import Pattern

from veloce import status
from veloce._constants import (
    HEADER_ACCESS_CONTROL_ALLOW_CREDENTIALS,
    HEADER_ACCESS_CONTROL_ALLOW_HEADERS,
    HEADER_ACCESS_CONTROL_ALLOW_METHODS,
    HEADER_ACCESS_CONTROL_ALLOW_ORIGIN,
    HEADER_ACCESS_CONTROL_ALLOW_PRIVATE_NETWORK,
    HEADER_ACCESS_CONTROL_EXPOSE_HEADERS,
    HEADER_ACCESS_CONTROL_MAX_AGE,
    HEADER_ACCESS_CONTROL_REQUEST_HEADERS,
    HEADER_ACCESS_CONTROL_REQUEST_METHOD,
    HEADER_ACCESS_CONTROL_REQUEST_PRIVATE_NETWORK,
    HEADER_ORIGIN,
)
from veloce._protocol_constants import (
    HTTP_METHOD_DELETE,
    HTTP_METHOD_GET,
    HTTP_METHOD_OPTIONS,
    HTTP_METHOD_PATCH,
    HTTP_METHOD_POST,
    HTTP_METHOD_PUT,
)
from veloce.http.request import Request
from veloce.http.response import Response
from veloce.middleware.base import Middleware

# Fast-path literals that match every possible origin. Anchors and the
# equivalent `^$`-bracketed forms collapse to the same set, as do
# `.*`/`.+`/`.{0,}` quantifiers. Stripping anchors first lets a single
# tuple cover them all. The probe-test below is the real guard - this is
# just a cheap pre-check that avoids compiling the regex for the obvious
# cases.
_WILDCARD_REGEX_BODIES = frozenset({".*", ".+", ".{0,}", ".{1,}"})

# Impossible-origin probes. A regex that fullmatches every one of these
# is treated as a wildcard equivalent. The set covers the common bypass
# shapes the literal denylist misses (`[\s\S]*`, `(?s).*`, `(?:.|\n)*`,
# `.{1,1000}`, etc.) without us having to enumerate every regex dialect.
_WILDCARD_PROBES = (
    "http://x.invalid",
    "null",
    "__not_an_origin__",
    "file:///etc/passwd",
)


def _is_wildcard_regex_literal(pattern: str) -> bool:
    body = pattern
    if body.startswith("^"):
        body = body[1:]
    if body.endswith("$"):
        body = body[:-1]
    return body in _WILDCARD_REGEX_BODIES


def _is_wildcard_regex(pattern: str, compiled: Pattern[str]) -> bool:
    if _is_wildcard_regex_literal(pattern):
        return True
    return all(compiled.fullmatch(probe) is not None for probe in _WILDCARD_PROBES)


class CORSMiddleware(Middleware):
    """Cross-Origin Resource Sharing middleware.

    Usage::

        app.add_middleware(
            CORSMiddleware(
                allow_origins=["https://example.com"],
                allow_methods=["GET", "POST"],
                allow_credentials=True,
            )
        )
    """

    def __init__(
        self,
        allow_origins: list[str] | None = None,
        allow_origin_regex: str | None = None,
        allow_methods: list[str] | None = None,
        allow_headers: list[str] | None = None,
        allow_credentials: bool = False,
        allow_private_network: bool = False,
        max_age: int = 600,
        expose_headers: list[str] | None = None,
        name: str | None = None,
    ) -> None:
        super().__init__(name=name)
        self.allow_origins = list(allow_origins) if allow_origins is not None else ["*"]
        self.allow_origin_regex: Pattern[str] | None
        if allow_origin_regex:
            try:
                compiled = re.compile(allow_origin_regex)
            except re.error as exc:
                raise ValueError(
                    f"CORSMiddleware: invalid allow_origin_regex {allow_origin_regex!r}: {exc}"
                ) from exc
            # Reject trivially wildcard patterns when credentials are on -
            # mirrors the `allow_origins=["*"]` + credentials guard so the
            # regex escape hatch can't be used to bypass it. Probes a set
            # of impossible-origin strings so dialect variants like
            # `[\s\S]*`, `(?s).*`, and `(?:.|\n)*` are caught even though
            # they don't appear in the literal denylist.
            if allow_credentials and _is_wildcard_regex(allow_origin_regex, compiled):
                raise ValueError(
                    "CORSMiddleware: allow_credentials=True cannot be combined with a "
                    f"wildcard allow_origin_regex {allow_origin_regex!r} "
                    "(Fetch CORS spec Sec. 3.2.4)"
                )
            self.allow_origin_regex = compiled
        else:
            self.allow_origin_regex = None
        self.allow_methods = allow_methods or [
            HTTP_METHOD_GET,
            HTTP_METHOD_POST,
            HTTP_METHOD_PUT,
            HTTP_METHOD_DELETE,
            HTTP_METHOD_PATCH,
            HTTP_METHOD_OPTIONS,
        ]
        self.allow_headers = allow_headers or ["*"]
        self.allow_credentials = allow_credentials
        self.allow_private_network = allow_private_network
        self.max_age = max_age
        self.expose_headers = expose_headers or []

        # Precomputed membership structures so the per-request CORS checks
        # are O(1): origin lookups hit a frozenset, and the preflight
        # header intersection reuses one lowercased set instead of
        # rebuilding it each request. `allow_headers` itself stays a list
        # - it is `", ".join`-ed into the response header.
        self._allow_origins_set: frozenset[str] = frozenset(self.allow_origins)
        self._allow_origins_has_star = "*" in self._allow_origins_set
        self._allow_headers_lower: frozenset[str] = frozenset(h.lower() for h in self.allow_headers)
        self._allow_headers_has_star = "*" in self.allow_headers
        # Uppercased method set for the preflight requested-method check.
        # HTTP methods are case-sensitive tokens; browsers send them in the
        # canonical uppercase form, so an exact-set membership test is correct.
        # Uppercased for the preflight check: browsers send the requested method
        # in canonical case (`GET`) in `Access-Control-Request-Method`, so a
        # lower-cased `allow_methods` config must still match.
        self._allow_methods_set: frozenset[str] = frozenset(m.upper() for m in self.allow_methods)
        # Precompute the joined header strings - these are constant for
        # the middleware lifetime, so the per-response `", ".join(...)`
        # in `_add_cors_headers` is wasted work.
        self._allow_methods_joined = ", ".join(self.allow_methods)
        self._allow_headers_joined = ", ".join(self.allow_headers)
        self._expose_headers_joined = ", ".join(self.expose_headers)

        # Wildcard-with-credentials is invalid per spec - fail loudly at
        # construction so a misconfigured app never serves it.
        if self.allow_credentials and (
            self._allow_origins_has_star or self._allow_headers_has_star
        ):
            raise ValueError(
                "CORSMiddleware: allow_credentials=True cannot be combined with "
                "wildcard allow_origins or allow_headers (Fetch CORS spec Sec. 3.2.4)"
            )

    # -- Origin matching ----------------------------------------------

    def _origin_allowed(self, origin: str) -> bool:
        """True if `origin` matches the configured allow-list or regex."""
        if not origin:
            return False
        if self._allow_origins_has_star or origin in self._allow_origins_set:
            return True
        return bool(self.allow_origin_regex and self.allow_origin_regex.fullmatch(origin))

    def _resolve_allow_origin(self, origin: str) -> str | None:
        """Pick the value for `Access-Control-Allow-Origin`.

        - With credentials: must echo the exact origin or refuse - `*` is
          forbidden.
        - Without credentials and a `*` allow-list: emit `*`.
        - Otherwise: echo the origin if it matches the list/regex.
        """
        if not self._origin_allowed(origin):
            return None
        if self.allow_credentials:
            return origin  # never `*` when credentials are involved
        if self._allow_origins_has_star:
            return "*"
        return origin

    # -- Request hooks ------------------------------------------------

    async def process_request(self, request: Request) -> Response | None:
        """Handle CORS preflight requests and validate origins."""
        origin = request.headers.get(HEADER_ORIGIN, "")
        request._state["_cors_origin"] = origin

        # Preflight: OPTIONS + Origin. Strict spec requires
        # `Access-Control-Request-Method` too, but older browsers (and many
        # test clients) send OPTIONS+Origin alone for soft preflight checks.
        # Honour both shapes so common patterns work; we still echo only
        # ACR-Headers when that header is actually present.
        if request.method == HTTP_METHOD_OPTIONS and origin:
            # A preflight from a disallowed origin gets a diagnostic 400
            # rather than a bare 204 - the browser would block it either
            # way, but 400 makes the rejection visible to developers.
            if not self._origin_allowed(origin):
                return Response(
                    status_code=status.HTTP_400_BAD_REQUEST, body=b"CORS origin not allowed"
                )
            # Fetch standard 4.8: the preflight is for the actual request's
            # method, carried in Access-Control-Request-Method. If that
            # method is not in the allow-set the preflight is a rejection;
            # surface it as a diagnostic 400 instead of a 204 the browser
            # would silently block. When the header is absent (soft OPTIONS
            # probes from older clients) the check is skipped.
            requested_method = request.headers.get(HEADER_ACCESS_CONTROL_REQUEST_METHOD, "")
            if (
                requested_method
                and "*" not in self._allow_methods_set
                and requested_method.upper() not in self._allow_methods_set
            ):
                return Response(
                    status_code=status.HTTP_400_BAD_REQUEST, body=b"CORS method not allowed"
                )
            response = Response(status_code=status.HTTP_204_NO_CONTENT, body=b"")
            self._add_cors_headers(response, origin, preflight=True)
            # Echo the requested headers (filtered) and method.
            requested = request.headers.get(HEADER_ACCESS_CONTROL_REQUEST_HEADERS, "")
            if requested and self._allow_headers_has_star:
                response.headers[HEADER_ACCESS_CONTROL_ALLOW_HEADERS] = requested
            elif requested:
                # Intersect requested vs the precomputed lowercased allow-set.
                tokens = [s for t in requested.split(",") if (s := t.strip())]
                matched = [t for t in tokens if t.lower() in self._allow_headers_lower]
                if matched:
                    response.headers[HEADER_ACCESS_CONTROL_ALLOW_HEADERS] = ", ".join(matched)
            # Private Network Access (https://wicg.github.io/private-network-access/):
            # a preflight from a public origin to a private host carries
            # Access-Control-Request-Private-Network: true. Only echo the
            # grant when the server was explicitly configured to allow it;
            # the header is omitted otherwise so the browser blocks access.
            if self.allow_private_network and (
                request.headers.get(HEADER_ACCESS_CONTROL_REQUEST_PRIVATE_NETWORK) == "true"
            ):
                response.headers[HEADER_ACCESS_CONTROL_ALLOW_PRIVATE_NETWORK] = "true"
            response.headers[HEADER_ACCESS_CONTROL_MAX_AGE] = str(self.max_age)
            return response

        return None

    async def process_response(self, request: Request, response: Response) -> Response:
        """Add CORS response headers."""
        origin = request._state.get("_cors_origin", "")
        # Plain (non-preflight) cross-origin responses still need
        # Access-Control-Allow-Origin and Vary: Origin if the value
        # depends on the request origin.
        self._add_cors_headers(response, origin, preflight=False)
        return response

    # -- Header writer ------------------------------------------------

    def _add_cors_headers(self, response: Response, origin: str, preflight: bool) -> None:
        allow_origin = self._resolve_allow_origin(origin)
        if allow_origin is not None:
            response.headers[HEADER_ACCESS_CONTROL_ALLOW_ORIGIN] = allow_origin

        # `Vary: Origin` is required whenever the response value depends on
        # the request origin (i.e. anything other than literal `*` without
        # credentials). Cache poisoning class is real here.
        if allow_origin is not None and allow_origin != "*":
            response.add_vary(HEADER_ORIGIN)

        if preflight:
            response.headers[HEADER_ACCESS_CONTROL_ALLOW_METHODS] = self._allow_methods_joined
            if self._allow_headers_has_star:
                response.headers[HEADER_ACCESS_CONTROL_ALLOW_HEADERS] = "*"
            else:
                response.headers[HEADER_ACCESS_CONTROL_ALLOW_HEADERS] = self._allow_headers_joined

        if self.allow_credentials:
            response.headers[HEADER_ACCESS_CONTROL_ALLOW_CREDENTIALS] = "true"

        if self.expose_headers:
            response.headers[HEADER_ACCESS_CONTROL_EXPOSE_HEADERS] = self._expose_headers_joined
