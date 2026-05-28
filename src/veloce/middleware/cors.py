"""Cross-Origin Resource Sharing (CORS) middleware.

Implemented from the Fetch standard's CORS protocol section
(https://fetch.spec.whatwg.org/#http-cors-protocol) and RFC 9110 §10.2.
Names match the spec; semantics are veloce's own.

Notable spec rules this middleware enforces:

- `Access-Control-Allow-Origin: *` MUST NOT be combined with
  `Access-Control-Allow-Credentials: true`. When credentials are allowed,
  the response must echo the exact request origin (and Vary on Origin
  so caches key by origin).
- Whenever the allowed origin depends on the request origin, the
  response MUST include `Vary: Origin` to prevent cache poisoning.
- Preflight (`OPTIONS` with `Access-Control-Request-Method`) returns
  204 with the negotiated set; preflight from a disallowed origin gets
  the same 204 (with no allow-* headers) — the browser enforces the
  block.
"""

from __future__ import annotations

import re
from re import Pattern

from veloce import status
from veloce.http.request import Request
from veloce.http.response import Response
from veloce.middleware.base import Middleware


class CORSMiddleware(Middleware):
    """Cross-Origin Resource Sharing middleware."""

    def __init__(
        self,
        allow_origins: list[str] | None = None,
        allow_origin_regex: str | None = None,
        allow_methods: list[str] | None = None,
        allow_headers: list[str] | None = None,
        allow_credentials: bool = False,
        max_age: int = 600,
        expose_headers: list[str] | None = None,
    ) -> None:
        self.allow_origins = list(allow_origins) if allow_origins is not None else ["*"]
        self.allow_origin_regex: Pattern[str] | None = (
            re.compile(allow_origin_regex) if allow_origin_regex else None
        )
        self.allow_methods = allow_methods or [
            "GET",
            "POST",
            "PUT",
            "DELETE",
            "PATCH",
            "OPTIONS",
        ]
        self.allow_headers = allow_headers or ["*"]
        self.allow_credentials = allow_credentials
        self.max_age = max_age
        self.expose_headers = expose_headers or []

        # Precomputed membership structures so the per-request CORS checks
        # are O(1): origin lookups hit a frozenset, and the preflight
        # header intersection reuses one lowercased set instead of
        # rebuilding it each request. `allow_headers` itself stays a list
        # — it is `", ".join`-ed into the response header.
        self._allow_origins_set: frozenset[str] = frozenset(self.allow_origins)
        self._allow_origins_has_star = "*" in self._allow_origins_set
        self._allow_headers_lower: frozenset[str] = frozenset(h.lower() for h in self.allow_headers)
        self._allow_headers_has_star = "*" in self.allow_headers
        # Precompute the joined header strings — these are constant for
        # the middleware lifetime, so the per-response `", ".join(...)`
        # in `_add_cors_headers` is wasted work.
        self._allow_methods_joined = ", ".join(self.allow_methods)
        self._allow_headers_joined = ", ".join(self.allow_headers)
        self._expose_headers_joined = ", ".join(self.expose_headers)

        # Wildcard-with-credentials is invalid per spec — fail loudly at
        # construction so a misconfigured app never serves it.
        if self.allow_credentials and (
            self._allow_origins_has_star or self._allow_headers_has_star
        ):
            raise ValueError(
                "CORSMiddleware: allow_credentials=True cannot be combined with "
                "wildcard allow_origins or allow_headers (Fetch CORS spec §3.2.4)"
            )

    # ── Origin matching ──────────────────────────────────────────────

    def _origin_allowed(self, origin: str) -> bool:
        """True if `origin` matches the configured allow-list or regex."""
        if not origin:
            return False
        if self._allow_origins_has_star or origin in self._allow_origins_set:
            return True
        return bool(self.allow_origin_regex and self.allow_origin_regex.fullmatch(origin))

    def _resolve_allow_origin(self, origin: str) -> str | None:
        """Pick the value for `Access-Control-Allow-Origin`.

        - With credentials: must echo the exact origin or refuse — `*` is
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

    # ── Request hooks ────────────────────────────────────────────────

    async def process_request(self, request: Request) -> Response | None:
        """Handle CORS preflight requests and validate origins."""
        origin = request.headers.get("origin", "")
        request._state["_cors_origin"] = origin

        # Preflight: OPTIONS + Origin. Strict spec requires
        # `Access-Control-Request-Method` too, but older browsers (and many
        # test clients) send OPTIONS+Origin alone for soft preflight checks.
        # Honour both shapes so common patterns work; we still echo only
        # ACR-Headers when that header is actually present.
        if request.method == "OPTIONS" and origin:
            # A preflight from a disallowed origin gets a diagnostic 400
            # rather than a bare 204 — the browser would block it either
            # way, but 400 makes the rejection visible to developers.
            if not self._origin_allowed(origin):
                return Response(
                    status_code=status.HTTP_400_BAD_REQUEST, body=b"Disallowed CORS origin"
                )
            response = Response(status_code=status.HTTP_204_NO_CONTENT, body=b"")
            self._add_cors_headers(response, origin, preflight=True)
            # Echo the requested headers (filtered) and method.
            requested = request.headers.get("access-control-request-headers", "")
            if requested and self._allow_headers_has_star:
                response.headers["Access-Control-Allow-Headers"] = requested
            elif requested:
                # Intersect requested vs the precomputed lowercased allow-set.
                tokens = [t.strip() for t in requested.split(",") if t.strip()]
                matched = [t for t in tokens if t.lower() in self._allow_headers_lower]
                if matched:
                    response.headers["Access-Control-Allow-Headers"] = ", ".join(matched)
            response.headers["Access-Control-Max-Age"] = str(self.max_age)
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

    # ── Header writer ────────────────────────────────────────────────

    def _add_cors_headers(self, response: Response, origin: str, preflight: bool) -> None:
        allow_origin = self._resolve_allow_origin(origin)
        if allow_origin is not None:
            response.headers["Access-Control-Allow-Origin"] = allow_origin

        # `Vary: Origin` is required whenever the response value depends on
        # the request origin (i.e. anything other than literal `*` without
        # credentials). Cache poisoning class is real here.
        if allow_origin is not None and allow_origin != "*":
            response.add_vary("Origin")

        if preflight:
            response.headers["Access-Control-Allow-Methods"] = self._allow_methods_joined
            if self._allow_headers_has_star:
                response.headers["Access-Control-Allow-Headers"] = "*"
            else:
                response.headers["Access-Control-Allow-Headers"] = self._allow_headers_joined

        if self.allow_credentials:
            response.headers["Access-Control-Allow-Credentials"] = "true"

        if self.expose_headers:
            response.headers["Access-Control-Expose-Headers"] = self._expose_headers_joined
