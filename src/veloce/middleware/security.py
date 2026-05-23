"""Security-related middleware — trusted hosts, rate limiting, HTTPS redirect."""

from __future__ import annotations

import time
from collections import deque

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

    def is_host_allowed(self, host: str) -> bool:
        """Whether `host` (bare hostname, no port) passes the allow-list.

        Public so the WebSocket dispatch path can apply the same check —
        a WebSocket handshake never reaches an HTTP middleware's
        `process_request`.
        """
        if self._allow_all or host in self._exact:
            return True
        # `*.example.com` matches `a.example.com`, `a.b.example.com`, but
        # NOT the bare `example.com` — the wildcard segment must exist
        # (per common nginx/Caddy conventions).
        return any(host.endswith("." + suffix) for suffix in self._wildcard_suffixes)

    async def process_request(self, request: Request) -> Response | None:
        # Strip port for matching; Host header may carry `example.com:8080`.
        host = request.headers.get("host", "").split(":", 1)[0].lower()
        if not self.is_host_allowed(host):
            return Response(status_code=400, body=b"Invalid host header")
        return None


class RateLimitMiddleware(Middleware):
    """Simple in-memory token-bucket rate limiter."""

    def __init__(self, max_requests: int = 100, window_seconds: int = 60) -> None:
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._buckets: dict[str, deque[float]] = {}
        self._last_sweep = time.monotonic()

    async def process_request(self, request: Request) -> Response | None:
        client = request.client_host or "unknown"
        now = time.monotonic()
        cutoff = now - self.window_seconds

        # Periodic eviction sweep — bounded memory across unique client IPs.
        # Mutates in place so an append racing with the sweep is not lost.
        if now - self._last_sweep >= self.window_seconds:
            stale = [
                ip for ip, stamps in self._buckets.items() if not stamps or stamps[-1] <= cutoff
            ]
            for ip in stale:
                b = self._buckets.get(ip)
                if b is not None and (not b or b[-1] <= cutoff):
                    del self._buckets[ip]
            self._last_sweep = now

        bucket = self._buckets.get(client)
        if bucket is None:
            bucket = deque()
            self._buckets[client] = bucket

        # Amortized O(1) eviction — popleft until the oldest stamp is fresh.
        while bucket and bucket[0] <= cutoff:
            bucket.popleft()

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


class SecurityHeadersMiddleware(Middleware):
    """Attach common hardening response headers to every response.

    Set by default:

    - ``X-Content-Type-Options: nosniff`` — stop MIME sniffing.
    - ``X-Frame-Options: DENY`` — block framing (clickjacking).
    - ``Referrer-Policy: strict-origin-when-cross-origin``.

    Off unless configured:

    - ``Strict-Transport-Security`` — pass ``hsts_max_age`` (seconds).
      Browsers honour HSTS only over HTTPS, so it is inert in plain-HTTP
      development, but it is still opt-in because it pins clients to
      HTTPS for the configured lifetime.
    - ``Content-Security-Policy`` — pass ``content_security_policy``.
    - ``Permissions-Policy`` — pass ``permissions_policy``.

    A header a handler already set on the response is left untouched —
    these are defaults, not overrides.
    """

    def __init__(
        self,
        *,
        content_type_options: str | None = "nosniff",
        frame_options: str | None = "DENY",
        referrer_policy: str | None = "strict-origin-when-cross-origin",
        hsts_max_age: int | None = None,
        hsts_include_subdomains: bool = True,
        hsts_preload: bool = False,
        content_security_policy: str | None = None,
        permissions_policy: str | None = None,
    ) -> None:
        # Resolve the full header set once at construction so the
        # per-response cost is just copying a small, fixed dict.
        headers: dict[str, str] = {}
        if content_type_options:
            headers["X-Content-Type-Options"] = content_type_options
        if frame_options:
            headers["X-Frame-Options"] = frame_options
        if referrer_policy:
            headers["Referrer-Policy"] = referrer_policy
        if hsts_max_age is not None:
            value = f"max-age={hsts_max_age}"
            if hsts_include_subdomains:
                value += "; includeSubDomains"
            if hsts_preload:
                value += "; preload"
            headers["Strict-Transport-Security"] = value
        if content_security_policy:
            headers["Content-Security-Policy"] = content_security_policy
        if permissions_policy:
            headers["Permissions-Policy"] = permissions_policy
        self._headers = headers

    async def process_response(self, request: Request, response: Response) -> Response:
        for name, value in self._headers.items():
            # Defaults only — never clobber a value the handler chose.
            if name not in response.headers:
                response.headers[name] = value
        return response


class WebSocketOriginMiddleware(Middleware):
    """Reject cross-site WebSocket handshakes (CSWSH).

    A WebSocket handshake is not subject to the Same-Origin Policy and
    bypasses CORS entirely, so a page on any origin can open a socket to
    your app unless the handshake `Origin` is checked. Register this
    with the origins your own front-end is served from; a handshake whose
    `Origin` is present but unlisted is refused with close code 1008.

    Browsers always send `Origin` on a WebSocket handshake (RFC 6455
    §4.1), so `allow_missing=True` (the default) still blocks every
    browser-driven CSWSH attempt while leaving non-browser clients
    (mobile apps, service-to-service) — which legitimately omit `Origin`
    — able to connect. Set `allow_missing=False` to additionally refuse
    handshakes that carry no `Origin` at all.

    Plain HTTP requests pass straight through — `Origin` enforcement for
    HTTP is `CORSMiddleware`'s job.
    """

    def __init__(self, allowed_origins: list[str], allow_missing: bool = True) -> None:
        self._allowed: frozenset[str] = frozenset(o.rstrip("/").lower() for o in allowed_origins)
        self._allow_all = "*" in self._allowed
        self._allow_missing = allow_missing

    def is_websocket_origin_allowed(self, origin: str) -> bool:
        """Whether a handshake carrying `origin` may proceed.

        Public so the WebSocket dispatch path can apply the check — a
        handshake never reaches an HTTP middleware's `process_request`.
        """
        if self._allow_all:
            return True
        if not origin:
            return self._allow_missing
        return origin.rstrip("/").lower() in self._allowed

    async def process_request(self, request: Request) -> Response | None:
        # HTTP traffic is out of scope — pass through untouched.
        return None
