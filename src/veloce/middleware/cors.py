"""CORS middleware — Cross-Origin Resource Sharing per the Fetch standard.

Implemented from the Fetch standard's CORS protocol section
(https://fetch.spec.whatwg.org/#http-cors-protocol) and RFC 9110 Sec. 10.2.
Names match the spec; semantics are veloce's own.

Notable spec rules this middleware enforces:

- `Access-Control-Allow-Origin: *` MUST NOT be combined with
  `Access-Control-Allow-Credentials: true`; with credentials the exact request
  origin is echoed instead.
- `Vary: Origin` MUST be sent whenever the allowed origin depends on the request
  origin, or a shared cache can be poisoned.
- Preflight returns 204 with the negotiated set; a disallowed origin or
  requested method gets a diagnostic 400 so the rejection is visible.
- Private Network Access: the `Access-Control-Allow-Private-Network` grant is
  opt-in via `allow_private_network=True` and never emitted otherwise.
"""

from __future__ import annotations

import re
from re import Pattern

import veloce.status as status
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
from veloce.http.header_set import HeaderSet
from veloce.http.request import Request
from veloce.http.response import Response, header_pop
from veloce.middleware.base import Middleware

# Literals matching every origin, anchors stripped; the probe test is the guard.
_WILDCARD_REGEX_BODIES = frozenset({".*", ".+", ".{0,}", ".{1,}"})

# A regex fullmatching every impossible origin here counts as a wildcard, which
# catches bypass shapes (`[\s\S]*`, `(?s).*`) the literal set above misses.
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
                allow_headers=["Authorization", "Content-Type"],
                allow_credentials=True,
            )
        )

    `allow_headers` defaults to `["*"]`, and the Fetch standard forbids a
    wildcard alongside credentials, so it must be listed explicitly whenever
    `allow_credentials=True` - omitting it raises at construction.
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
        # A regex-only config must not fall back to `*`: `_origin_allowed` would
        # short-circuit on the star, echo it to every origin, and leave the regex dead.
        if allow_origins is not None:
            self.allow_origins = list(allow_origins)
        else:
            self.allow_origins = [] if allow_origin_regex else ["*"]
        self.allow_origin_regex: Pattern[str] | None
        if allow_origin_regex:
            try:
                compiled = re.compile(allow_origin_regex)
            except re.error as exc:
                raise ValueError(
                    f"CORSMiddleware: invalid allow_origin_regex {allow_origin_regex!r}: {exc}"
                ) from exc
            # Mirrors the `allow_origins=["*"]` guard, so the regex cannot bypass it.
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

        # Precomputed so the per-request checks are O(1).
        self._allow_origins_set: frozenset[str] = frozenset(self.allow_origins)
        self._allow_origins_has_star = "*" in self._allow_origins_set
        # Exactly when `Vary: Origin` is required. Settled from the configuration
        # because it must hold for a refused origin, which carries no such header.
        self._varies_by_origin = bool(
            self.allow_origin_regex or allow_credentials or not self._allow_origins_has_star
        )
        self._allow_headers_lower: frozenset[str] = frozenset(h.lower() for h in self.allow_headers)
        self._allow_headers_has_star = "*" in self.allow_headers
        # Browsers send the canonical uppercase form, so the config is folded up.
        self._allow_methods_set: frozenset[str] = frozenset(m.upper() for m in self.allow_methods)
        # Constant for the middleware lifetime, so joined once rather than per response.
        self._allow_methods_joined = ", ".join(self.allow_methods)
        self._allow_headers_joined = ", ".join(self.allow_headers)
        self._expose_headers_joined = ", ".join(self.expose_headers)

        # Wildcard-with-credentials is invalid per spec; refuse at construction.
        if self.allow_credentials and (
            self._allow_origins_has_star or self._allow_headers_has_star
        ):
            raise ValueError(
                "CORSMiddleware: allow_credentials=True cannot be combined with "
                "wildcard allow_origins or allow_headers (Fetch CORS spec Sec. 3.2.4)"
            )

    # ── Origin matching ───────────────────────────────────

    def _origin_allowed(self, origin: str) -> bool:
        """True if `origin` matches the configured allow-list or regex."""
        if not origin:
            return False
        if self._allow_origins_has_star or origin in self._allow_origins_set:
            return True
        return bool(self.allow_origin_regex and self.allow_origin_regex.fullmatch(origin))

    def _resolve_allow_origin(self, origin: str) -> str | None:
        """Pick the value for `Access-Control-Allow-Origin`.

        With credentials the exact origin is echoed or refused - `*` is
        forbidden. Otherwise `*` for a `*` allow-list, else the matching origin.
        """
        if not self._origin_allowed(origin):
            return None
        if self.allow_credentials:
            return origin  # never `*` when credentials are involved
        if self._allow_origins_has_star:
            return "*"
        return origin

    # ── Request hooks ─────────────────────────────────────

    async def process_request(self, request: Request) -> Response | None:
        """Handle CORS preflight requests and validate origins."""
        origin = request.headers.get(HEADER_ORIGIN, "")
        request._state["_cors_origin"] = origin

        # The spec requires `Access-Control-Request-Method` too, but older clients
        # send OPTIONS+Origin alone for soft checks, so both shapes are honoured.
        if request.method == HTTP_METHOD_OPTIONS and origin:
            # 400 rather than a bare 204 makes the rejection visible to developers.
            if not self._origin_allowed(origin):
                return Response(
                    status_code=status.HTTP_400_BAD_REQUEST, body=b"CORS origin not allowed"
                )
            # Fetch standard 4.8: the preflight is for the method named in
            # Access-Control-Request-Method, so a disallowed one is a rejection.
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
            requested = request.headers.get(HEADER_ACCESS_CONTROL_REQUEST_HEADERS, "")
            if requested and self._allow_headers_has_star:
                response.headers[HEADER_ACCESS_CONTROL_ALLOW_HEADERS] = requested
            elif requested:
                tokens = [s for t in requested.split(",") if (s := t.strip())]
                matched = [t for t in tokens if t.lower() in self._allow_headers_lower]
                if matched:
                    response.headers[HEADER_ACCESS_CONTROL_ALLOW_HEADERS] = ", ".join(matched)
            # Private Network Access (https://wicg.github.io/private-network-access/):
            # echoed only when configured, else omitted so the browser blocks.
            if self.allow_private_network and (
                request.headers.get(HEADER_ACCESS_CONTROL_REQUEST_PRIVATE_NETWORK) == "true"
            ):
                response.headers[HEADER_ACCESS_CONTROL_ALLOW_PRIVATE_NETWORK] = "true"
            response.headers[HEADER_ACCESS_CONTROL_MAX_AGE] = str(self.max_age)
            return response

        return None

    async def process_response(self, request: Request, response: Response) -> Response:
        """Add CORS response headers."""
        # Missing whenever the response phase runs without the request phase, which
        # is a refusal: a 413 would ship with no `Access-Control-Allow-Origin`.
        cached = request._state.get("_cors_origin")
        origin: str = cached if cached is not None else request.headers.get(HEADER_ORIGIN, "")
        self._add_cors_headers(response, origin, preflight=False)
        return response

    # ── Header writer ─────────────────────────────────────

    def _add_cors_headers(self, response: Response, origin: str, preflight: bool) -> None:
        allow_origin = self._resolve_allow_origin(origin)
        if allow_origin is not None:
            response.headers[HEADER_ACCESS_CONTROL_ALLOW_ORIGIN] = allow_origin

        # Leaving a refused response unmarked lets a shared cache store it unkeyed
        # and later hand it to an allowed origin, whose browser then blocks it.
        if self._varies_by_origin:
            response.add_vary(HEADER_ORIGIN)

        if preflight:
            response.headers[HEADER_ACCESS_CONTROL_ALLOW_METHODS] = self._allow_methods_joined
            if self._allow_headers_has_star:
                response.headers[HEADER_ACCESS_CONTROL_ALLOW_HEADERS] = "*"
            else:
                response.headers[HEADER_ACCESS_CONTROL_ALLOW_HEADERS] = self._allow_headers_joined

        # Both headers qualify an `Access-Control-Allow-Origin`, so emitting them
        # for a refused origin would advertise credentials it cannot use.
        if allow_origin is None:
            return

        if self.allow_credentials:
            response.headers[HEADER_ACCESS_CONTROL_ALLOW_CREDENTIALS] = "true"

        if self.expose_headers:
            # Merge, not assign: other middleware contribute to this list header.
            headers = response.headers
            existing = header_pop(headers, HEADER_ACCESS_CONTROL_EXPOSE_HEADERS)
            if existing:
                merged = HeaderSet(existing)
                merged.update(self.expose_headers)
                response.headers[HEADER_ACCESS_CONTROL_EXPOSE_HEADERS] = merged.to_header()
            else:
                response.headers[HEADER_ACCESS_CONTROL_EXPOSE_HEADERS] = self._expose_headers_joined
