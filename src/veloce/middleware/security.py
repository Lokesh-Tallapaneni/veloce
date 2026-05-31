"""Security-related middleware — trusted hosts, rate limiting, HTTPS redirect.

Covers Host validation (RFC 9110 §7.2), HTTPS upgrade via 308 redirect
(RFC 9110 §15.4.9), rate-limit headers (draft-ietf-httpapi-ratelimit-headers),
WebSocket origin checks against CSWSH (RFC 6455 §4.1, §4.2.2), and common
hardening response headers.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
import uuid
from collections import deque

from veloce import status
from veloce._constants import HEADER_RETRY_AFTER
from veloce._internal import _extract_host
from veloce.http.request import Request
from veloce.http.response import RedirectResponse, Response
from veloce.middleware.base import Middleware

# Stash key used to thread bucket state from process_request → process_response
# so the response path can emit X-RateLimit-* without recomputing.
_RL_STATE_KEY = "rate_limit_state"


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
        # Wildcards stored with leading dot already baked in so the per-request
        # check is a single `endswith` call — no per-request string concat.
        self._wildcard_suffixes_dotted: tuple[str, ...] = tuple(
            "." + h.lower()[2:] for h in allowed_hosts if h.startswith("*.")
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
        return any(host.endswith(suffix) for suffix in self._wildcard_suffixes_dotted)

    async def process_request(self, request: Request) -> Response | None:
        """Reject requests whose Host header is not in the allow-list."""
        # Strip port for matching; shared with the request-side host
        # parser so the IPv6-bracket / bare-IPv6 / IPv4-with-port shapes
        # stay consistent in both directions.
        host = _extract_host(request.headers.get("host", ""))
        if not self.is_host_allowed(host):
            return Response(status_code=status.HTTP_400_BAD_REQUEST, body=b"Invalid host header")
        return None


class RateLimitMiddleware(Middleware):
    """In-process token-bucket rate limiter.

    Counters live in a per-instance dict and are NOT shared across
    worker processes. Multi-worker deployments (`uvicorn --workers N`)
    will see an effective limit of roughly `N × max_requests` per
    window because each worker counts independently. For accurate
    cross-worker limits use a reverse-proxy limiter (nginx
    `limit_req`) or a Redis-backed implementation; the in-process
    limiter is intended for single-worker development and small
    deployments.
    """

    def __init__(self, max_requests: int = 100, window_seconds: int = 60) -> None:
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._buckets: dict[str, deque[float]] = {}
        self._last_sweep = time.monotonic()
        # Lazy-allocated on first sweep so the lock binds to the running
        # event loop, not to whatever loop is current at construction
        # time (matches the same pattern used for `Veloce`'s first-request
        # lock). Guards the timestamp-check + dict-rebuild + timestamp-
        # update sequence — single-threaded asyncio already serialises
        # this block today because it contains no `await`, but any
        # future async cache backend that introduces an `await` inside
        # the sweep block would otherwise open a check-then-act race.
        self._sweep_lock: asyncio.Lock | None = None

    async def process_request(self, request: Request) -> Response | None:
        """Enforce per-client request rate limits."""
        client = self._bucket_key(request)
        now = time.monotonic()
        cutoff = now - self.window_seconds

        # Periodic eviction sweep — bounded memory across unique client IPs.
        if now - self._last_sweep >= self.window_seconds:
            if self._sweep_lock is None:
                self._sweep_lock = asyncio.Lock()
            async with self._sweep_lock:
                # Double-check under the lock so a request that lost the
                # race to acquire the lock does not redo the sweep.
                if now - self._last_sweep >= self.window_seconds:
                    stale = [
                        ip
                        for ip, stamps in self._buckets.items()
                        if not stamps or stamps[-1] <= cutoff
                    ]
                    for ip in stale:
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
            reset = self._reset_after(bucket, now)
            rejected = Response(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                body=b"Too Many Requests",
                headers={HEADER_RETRY_AFTER: str(reset or self.window_seconds)},
            )
            self._apply_headers(rejected, self.max_requests, 0, reset)
            return rejected

        bucket.append(now)
        # Stash the bucket so process_response can read the freshest state
        # without re-resolving the client key or racing with another request.
        request._state[_RL_STATE_KEY] = bucket
        return None

    async def process_response(self, request: Request, response: Response) -> Response:
        """Attach X-RateLimit-* headers to successful responses."""
        bucket = request._state.get(_RL_STATE_KEY)
        if bucket is None:
            return response
        remaining = self.max_requests - len(bucket)
        if remaining < 0:
            remaining = 0
        reset = self._reset_after(bucket, time.monotonic())
        self._apply_headers(response, self.max_requests, remaining, reset)
        return response

    def _bucket_key(self, request: Request) -> str:
        """Pick a bucket key for `request`.

        Falls back through several signals when the transport peer is
        unknown so anonymous traffic does NOT share one global bucket
        (otherwise a single anonymous source could exhaust the limit for
        every other anonymous caller — a trivial DoS).
        """
        client = request.client_host
        if client:
            return f"host:{client}"
        # Right-most X-Forwarded-For hop — the closest proxy is the only
        # trustworthy entry in the chain; left-most entries are
        # attacker-controlled (see development-guardrails.md, Security
        # Parameter Validation).
        xff = request.headers.get("x-forwarded-for", "")
        if xff:
            last = xff.split(",")[-1].strip()
            if last:
                return f"xff:{last}"
        # User-Agent hash — coarse, but partitions anonymous callers by
        # client software when no IP is available.
        ua = request.headers.get("user-agent", "")
        if ua:
            return "ua:" + hashlib.sha256(ua.encode("utf-8", "replace")).hexdigest()[:16]
        # Last resort: per-request UUID stashed on _state so a scope-id
        # reuse after GC can't leak a stale deque into a new request.
        # Effectively disables limiting for fully anonymous traffic —
        # the correct failure mode (fail-open per caller, not fail-shared
        # across all callers).
        anon_id = request._state.get("_rl_anon_id")
        if anon_id is None:
            anon_id = uuid.uuid4().hex
            request._state["_rl_anon_id"] = anon_id
        return f"scope:{anon_id}"

    def _reset_after(self, bucket: deque[float], now: float) -> int:
        """Seconds until the oldest stamp in `bucket` falls out of the window."""
        if not bucket:
            return 0
        remaining = int(bucket[0] + self.window_seconds - now)
        return remaining if remaining > 0 else 0

    def _apply_headers(self, response: Response, limit: int, remaining: int, reset: int) -> None:
        """Attach X-RateLimit-* headers to `response` (draft-ietf-httpapi-ratelimit-headers)."""
        response.headers["X-RateLimit-Limit"] = str(limit)
        response.headers["X-RateLimit-Remaining"] = str(remaining)
        response.headers["X-RateLimit-Reset"] = str(reset)


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
        """Redirect HTTP requests to HTTPS with a 308 status."""
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

        host = request.headers.get("host", "localhost")
        url = f"https://{host}{request.path}"
        if request.query_string:
            url += f"?{request.query_string}"
        return RedirectResponse(url, status_code=status.HTTP_308_PERMANENT_REDIRECT)


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
        hsts_include_subdomains: bool = False,
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
        """Attach security hardening headers to every response."""
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
