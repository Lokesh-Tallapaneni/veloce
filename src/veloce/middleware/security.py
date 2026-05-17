"""Security-related middleware — trusted hosts, rate limiting, HTTPS redirect."""

from __future__ import annotations

import time

from veloce.http.request import Request
from veloce.http.response import Response
from veloce.middleware.base import Middleware


class TrustedHostMiddleware(Middleware):
    """Validates Host header against an allow-list.

    Supports literal hostnames, the catch-all `*`, and subdomain wildcards
    of the form `*.example.com` (matches `api.example.com`,
    `a.b.example.com`, etc. — never the bare `example.com`). Matching is
    case-insensitive; the port portion of `Host:` is stripped before
    comparison (RFC 9110 §7.2).
    """

    def __init__(self, allowed_hosts: list[str]) -> None:
        # Split exact matches from wildcards at construction so the request
        # hot path stays O(1) for the common case.
        self._exact: frozenset[str] = frozenset(
            h.lower() for h in allowed_hosts if not h.startswith("*.")
        )
        # Wildcards stored as bare suffix strings (no leading dot) — match
        # `host.endswith("." + suffix)`.
        self._wildcard_suffixes: tuple[str, ...] = tuple(
            h.lower()[2:] for h in allowed_hosts if h.startswith("*.")
        )
        self._allow_all = "*" in self._exact

    def _host_allowed(self, host: str) -> bool:
        if self._allow_all or host in self._exact:
            return True
        # `*.example.com` matches `a.example.com`, `a.b.example.com`, but
        # NOT the bare `example.com` — the wildcard segment must exist
        # (per common nginx/Caddy conventions).
        return any(host.endswith("." + suffix) for suffix in self._wildcard_suffixes)

    async def process_request(self, request: Request) -> Response | None:
        # Strip port for matching; Host header may carry `example.com:8080`.
        host = request.headers.get("host", "").split(":", 1)[0].lower()
        if not self._host_allowed(host):
            return Response(status_code=400, body=b"Invalid host header")
        return None


class RateLimitMiddleware(Middleware):
    """Simple in-memory token-bucket rate limiter."""

    def __init__(self, max_requests: int = 100, window_seconds: int = 60) -> None:
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._buckets: dict[str, list[float]] = {}

    async def process_request(self, request: Request) -> Response | None:
        client = request.client_host or "unknown"
        now = time.monotonic()

        if client not in self._buckets:
            self._buckets[client] = []

        cutoff = now - self.window_seconds
        self._buckets[client] = [t for t in self._buckets[client] if t > cutoff]
        bucket = self._buckets[client]

        if len(bucket) >= self.max_requests:
            return Response(
                status_code=429,
                body=b"Too Many Requests",
                headers={"Retry-After": str(self.window_seconds)},
            )

        bucket.append(now)
        return None


class HTTPSRedirectMiddleware(Middleware):
    """Redirect HTTP requests to HTTPS.

    Resolves the request scheme in this order:
      1. ASGI scope `"scheme"` if set to `"https"`/`"wss"` (the
         server already terminated TLS).
      2. `X-Forwarded-Proto` header (when a `ProxyFix`-style middleware
         ran upstream this is already the trusted value).
      3. Default `http`.

    Uses 308 Permanent Redirect (RFC 9110 §15.4.9) so non-GET methods
    preserve their method and body. The earlier `301` form was wrong
    for `POST`/`PUT` callers — those would silently become `GET`.
    """

    async def process_request(self, request: Request) -> Response | None:
        # Trust ASGI scope first — the server set it based on the actual
        # transport, not a header that anyone could spoof.
        scope_scheme = request.scope.get("scheme") if request.scope else None
        if scope_scheme in ("https", "wss"):
            return None
        # Fall back to X-Forwarded-Proto for environments behind a
        # TLS-terminating proxy that doesn't set scope correctly.
        fwd_proto = request.headers.get("x-forwarded-proto", "").lower()
        if fwd_proto == "https":
            return None

        from veloce.http.response import RedirectResponse

        host = request.headers.get("host", "localhost")
        url = f"https://{host}{request.path}"
        if request.query_string:
            url += f"?{request.query_string}"
        return RedirectResponse(url, status_code=308)
