"""Security-related middleware - trusted hosts, rate limiting, HTTPS redirect.

Covers Host validation (RFC 9110 Sec. 7.2), HTTPS upgrade via 308 redirect
(RFC 9110 Sec. 15.4.9), rate-limit headers (draft-ietf-httpapi-ratelimit-headers),
WebSocket origin checks against CSWSH (RFC 6455 Sec. 4.1, Sec. 4.2.2), and common
hardening response headers.
"""

from __future__ import annotations

import asyncio
import hashlib
import math
import secrets
import time
import uuid
from collections import deque
from collections.abc import Mapping, Sequence

from veloce import status
from veloce._constants import (
    HEADER_CONTENT_SECURITY_POLICY,
    HEADER_CONTENT_SECURITY_POLICY_REPORT_ONLY,
    HEADER_HOST,
    HEADER_PERMISSIONS_POLICY,
    HEADER_REFERRER_POLICY,
    HEADER_RETRY_AFTER,
    HEADER_STRICT_TRANSPORT_SECURITY,
    HEADER_USER_AGENT,
    HEADER_VALUE_DENY,
    HEADER_VALUE_NOSNIFF,
    HEADER_VALUE_STRICT_ORIGIN_WHEN_CROSS_ORIGIN,
    HEADER_X_CONTENT_TYPE_OPTIONS,
    HEADER_X_FORWARDED_FOR,
    HEADER_X_FORWARDED_PROTO,
    HEADER_X_FRAME_OPTIONS,
    HEADER_X_RATELIMIT_LIMIT,
    HEADER_X_RATELIMIT_REMAINING,
    HEADER_X_RATELIMIT_RESET,
)
from veloce._internal import _extract_host
from veloce._protocol_constants import URL_SCHEME_HTTPS, URL_SCHEME_WSS
from veloce.http.request import Request
from veloce.http.response import RedirectResponse, Response, header_present
from veloce.middleware.base import Middleware
from veloce.ratelimit import (
    RATE_LIMIT_ATTR,
    InMemoryRateLimitBackend,
    RateLimitBackend,
    RateLimitResult,
    RateLimitStrategy,
)

# Stash key used to thread bucket state from process_request -> process_response
# so the response path can emit X-RateLimit-* without recomputing.
_RL_STATE_KEY = "rate_limit_state"

# request.state slot holding the per-request CSP nonce (materialized lazily).
CSP_NONCE_STATE_KEY = "csp_nonce"


def csp_nonce(request: Request) -> str | None:
    """Return the per-request CSP nonce, materializing it on first access.

    Templating helpers and handlers embed this on `<script>`/`<style>` tags
    as `nonce="..."`. Returns None when CSPMiddleware did not arm a nonce
    for this request.
    """
    cached = request._state.get(CSP_NONCE_STATE_KEY)
    if cached is None:
        return None
    if isinstance(cached, str):
        return cached
    # A factory closure was stored: materialize, cache, return.
    value = cached()
    request._state[CSP_NONCE_STATE_KEY] = value
    return value


def _normalize_csp(policy: str | Mapping[str, str | Sequence[str]]) -> str:
    """Normalize a CSP policy (str template or directive dict) to a template.

    A dict maps `directive -> source(s)`; the `'nonce'` sentinel source is
    replaced with the runtime `{nonce}` placeholder.
    """
    if isinstance(policy, str):
        return policy
    if isinstance(policy, Mapping):
        segments: list[str] = []
        for directive, sources in policy.items():
            src_list = [sources] if isinstance(sources, str) else list(sources)
            rendered = [("{nonce}" if s == "'nonce'" else s) for s in src_list]
            segments.append(" ".join([directive, *rendered]))
        return "; ".join(segments)
    raise TypeError("CSP policy must be a str template or a directive mapping")


class CSPMiddleware(Middleware):
    """Emit Content-Security-Policy with optional per-request nonce.

    `policy` and `report_only_policy` each accept a str template containing
    the literal `{nonce}` placeholder, or a directive mapping where the
    `'nonce'` source is substituted with a fresh per-request nonce.

    Usage::

        app.add_middleware(
            CSPMiddleware(
                policy={"default-src": "'self'", "script-src": ["'self'", "'nonce'"]},
                report_only_policy="default-src 'self'",
            )
        )

    Static (no-nonce) policies can stay on SecurityHeadersMiddleware; use
    this when a per-request nonce or a report-only policy is needed.
    """

    def __init__(
        self,
        policy: str | Mapping[str, str | Sequence[str]] | None = None,
        *,
        report_only_policy: str | Mapping[str, str | Sequence[str]] | None = None,
        nonce: bool = True,
        name: str | None = None,
    ) -> None:
        super().__init__(name=name)
        assert policy is not None or report_only_policy is not None, (
            "CSPMiddleware requires at least one of policy or report_only_policy"
        )
        self._enforce_template = _normalize_csp(policy) if policy is not None else None
        self._report_template = (
            _normalize_csp(report_only_policy) if report_only_policy is not None else None
        )
        references_nonce = (
            self._enforce_template is not None and "{nonce}" in self._enforce_template
        ) or (self._report_template is not None and "{nonce}" in self._report_template)
        # Fail fast on a contradictory config: a template that references a
        # nonce (a `{nonce}` placeholder, or a `'nonce'` source already
        # normalized to `{nonce}`) while nonce generation is disabled would
        # render `'nonce-None'` at response time, which browsers parse as a
        # real - and wrong - nonce, silently breaking the policy. Refuse the
        # construction rather than emit a misleading header.
        if not nonce and references_nonce:
            raise ValueError(
                "CSPMiddleware was constructed with nonce=False but a policy "
                "references a nonce ('{nonce}' placeholder or 'nonce' source); "
                "enable nonce=True or remove the nonce reference"
            )
        self._needs_nonce = nonce and references_nonce

    async def process_request(self, request: Request) -> Response | None:
        if self._needs_nonce:
            # Store a one-shot factory; csp_nonce() materializes on first read
            # so a request that never embeds a nonce pays only the store.
            request._state[CSP_NONCE_STATE_KEY] = lambda: secrets.token_urlsafe(16)
        return None

    async def process_response(self, request: Request, response: Response) -> Response:
        # `Response.headers` is a plain dict, so membership is case-sensitive.
        # HTTP field names are case-insensitive (RFC 9110 Sec. 5.1), and
        # browsers INTERSECT multiple CSP headers, so a route-level override
        # spelled `content-security-policy` (lowercase) must still suppress
        # the default - otherwise both headers ship and the route's intended
        # policy is silently narrowed. Probe case-insensitively.
        for header, template in (
            (HEADER_CONTENT_SECURITY_POLICY, self._enforce_template),
            (HEADER_CONTENT_SECURITY_POLICY_REPORT_ONLY, self._report_template),
        ):
            if template is None or header_present(response.headers, header):
                continue
            if "{nonce}" in template:
                value = template.replace("{nonce}", f"'nonce-{csp_nonce(request)}'")
            else:
                value = template
            response.headers[header] = value
        return response


class TrustedHostMiddleware(Middleware):
    """Validates Host header against an allow-list.

    Supports literal hostnames, the catch-all `*`, and subdomain wildcards
    of the form `*.example.com` (matches `api.example.com`,
    `a.b.example.com`, etc. - never the bare `example.com`). Matching is
    case-insensitive; the port portion of `Host:` is stripped before
    comparison (RFC 9110 Sec. 7.2).
    """

    def __init__(self, allowed_hosts: list[str], *, name: str | None = None) -> None:
        super().__init__(name=name)
        # Split exact matches from wildcards at construction so the request
        # hot path stays O(1) for the common case.
        self._exact: frozenset[str] = frozenset(
            h.lower() for h in allowed_hosts if not h.startswith("*.")
        )
        # Wildcards stored with leading dot already baked in so the per-request
        # check is a single `endswith` call - no per-request string concat.
        self._wildcard_suffixes_dotted: tuple[str, ...] = tuple(
            "." + h.lower()[2:] for h in allowed_hosts if h.startswith("*.")
        )
        self._allow_all = "*" in self._exact

    def is_host_allowed(self, host: str) -> bool:
        """Whether `host` (bare hostname, no port) passes the allow-list.

        Public so the WebSocket dispatch path can apply the same check -
        a WebSocket handshake never reaches an HTTP middleware's
        `process_request`.
        """
        if self._allow_all or host in self._exact:
            return True
        # `*.example.com` matches `a.example.com`, `a.b.example.com`, but
        # NOT the bare `example.com` - the wildcard segment must exist
        # (per common nginx/Caddy conventions).
        return any(host.endswith(suffix) for suffix in self._wildcard_suffixes_dotted)

    async def process_request(self, request: Request) -> Response | None:
        """Reject requests whose Host header is not in the allow-list."""
        # Strip port for matching; shared with the request-side host
        # parser so the IPv6-bracket / bare-IPv6 / IPv4-with-port shapes
        # stay consistent in both directions.
        host = _extract_host(request.headers.get(HEADER_HOST, ""))
        if not self.is_host_allowed(host):
            return Response(status_code=status.HTTP_400_BAD_REQUEST, body=b"Invalid host header")
        return None


class RateLimitMiddleware(Middleware):
    """Per-client rate limiter with a selectable algorithm and backend.

    Two ways to configure it:

    - The default `max_requests` per `window_seconds` runs a process-local
      sliding-log limiter - simple, zero-dependency, intended for a single
      worker. Counters are NOT shared across workers, so `uvicorn --workers N`
      sees roughly `N x max_requests` per window.
    - Pass a `strategy` - `FixedWindow`, `SlidingWindow`, or `TokenBucket` - to
      choose the algorithm, and a `backend` to choose where state lives:
      `InMemoryRateLimitBackend` (default) or
      `veloce.contrib.redis.RedisRateLimitBackend` for one limit shared across
      every worker and host.

    Give a route its own limit by decorating its handler with `rate_limit` - the
    limit lives on the handler, so there is no route string to mistype::

        from veloce import rate_limit

        @app.post("/login")
        @rate_limit(TokenBucket(rate=5, per=60))
        async def login(request): ...

    The `overrides` map is the central alternative for handlers you cannot
    decorate: it maps a route's *full* path template to a strategy. The key is
    the template as matched at runtime - the value of `request.url_rule` - so a
    blueprint route includes its `url_prefix` (`/api/login`, not `/login`); an
    override key that matches no route raises on the first request. An explicit
    `overrides` entry wins over a `rate_limit` tag on the same route.

    Either way, an overridden route gets its own per-client counter, independent
    of the default budget; routes without an override keep the shared default.
    Like `exclude_middleware`, the per-route strategy is resolved against the
    route matched at dispatch entry, so a `before_request` hook that rewrites the
    path does not change which limit applies.

    Usage::

        from veloce import RateLimitMiddleware, TokenBucket

        app.add_middleware(
            RateLimitMiddleware(
                strategy=TokenBucket(rate=1000, per=60),
                overrides={"/login": TokenBucket(rate=5, per=60)},
            )
        )
    """

    def __init__(
        self,
        max_requests: int = 100,
        window_seconds: int = 60,
        *,
        strategy: RateLimitStrategy | None = None,
        backend: RateLimitBackend | None = None,
        overrides: dict[str, RateLimitStrategy] | None = None,
        name: str | None = None,
    ) -> None:
        super().__init__(name=name)
        self._strategy = strategy
        if strategy is None:
            if backend is not None:
                raise ValueError("backend requires a strategy; pass strategy= as well")
            if overrides is not None:
                raise ValueError("overrides requires a strategy; pass strategy= as well")
            # Legacy process-local sliding-log path.
            self.max_requests = max_requests
            self.window_seconds = window_seconds
            self._buckets: dict[str, deque[float]] = {}
            self._last_sweep = time.monotonic()
            # Lazy-allocated on first sweep so the lock binds to the running
            # event loop, not to whatever loop is current at construction
            # time (matches the same pattern used for `Veloce`'s first-request
            # lock). Guards the timestamp-check + dict-rebuild + timestamp-
            # update sequence - single-threaded asyncio already serialises
            # this block today because it contains no `await`, but any
            # future async cache backend that introduces an `await` inside
            # the sweep block would otherwise open a check-then-act race.
            self._sweep_lock: asyncio.Lock | None = None
        else:
            # Pluggable algorithm + backend path. The backend runs the pure
            # strategy under its own atomic read-modify-write.
            self._backend = backend if backend is not None else InMemoryRateLimitBackend()
            self._overrides: dict[str, RateLimitStrategy] | None = None
            if overrides:
                for route, override in overrides.items():
                    if not isinstance(override, RateLimitStrategy):
                        raise TypeError(f"overrides[{route!r}] must be a RateLimitStrategy")
                self._overrides = dict(overrides)
            # Per-route strategies, keyed by route template, combining
            # `@rate_limit`-tagged handlers with the explicit `overrides` map.
            # Built once on the first request (when the route table is final);
            # `None` until then. An empty result means no per-route limits, so
            # the per-request path stays a single client-keyed evaluation.
            self._route_strategies: dict[str, RateLimitStrategy] | None = None

    async def process_request(self, request: Request) -> Response | None:
        """Enforce per-client request rate limits."""
        strategy = self._strategy
        if strategy is not None:
            return await self._process_strategy(request, strategy)
        return await self._process_legacy(request)

    async def _process_strategy(
        self, request: Request, strategy: RateLimitStrategy
    ) -> Response | None:
        per_route = self._route_strategies
        if per_route is None:
            per_route = self._build_route_strategies(request)
        # No per-route limits: the common path stays a single client-keyed
        # evaluation with no extra lookup.
        if per_route:
            route = request.url_rule
            override = per_route.get(route) if route is not None else None
            if override is not None:
                strategy = override
                # Scope the key to the route so an overridden route keeps its own
                # per-client counter, separate from the default budget.
                key = f"{route}\x00{self._bucket_key(request)}"
            else:
                key = self._bucket_key(request)
        else:
            key = self._bucket_key(request)
        # Wall-clock time so the same key on a shared backend agrees across
        # workers and hosts; the strategy refills/counts against it.
        result = await self._backend.evaluate(key, strategy, time.time())
        if not result.allowed:
            rejected = Response(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                body=b"Too Many Requests",
                headers={HEADER_RETRY_AFTER: str(result.retry_after)},
            )
            self._apply_headers(rejected, result.limit, 0, result.reset)
            return rejected
        request._state[_RL_STATE_KEY] = result
        return None

    def _build_route_strategies(self, request: Request) -> dict[str, RateLimitStrategy]:
        # Resolve per-route strategies once, on the first request, when the route
        # table is final. Combines `@rate_limit`-tagged handlers (discovered by
        # scanning the routes) with the explicit `overrides` map, keyed by the
        # route template - the value of `request.url_rule`, so a blueprint route
        # includes its url_prefix. Explicit `overrides` win over a decorator tag.
        app = request.app
        if app is None:
            # No app to scan yet (e.g. a bare unit-test request); don't cache, so
            # the real first request resolves the table.
            return {}
        combined: dict[str, RateLimitStrategy] = {}
        known: set[str] = set()
        for _method, _path, info in app._collect_all_routes():
            known.add(info.path_template)
            tagged = getattr(info.handler, RATE_LIMIT_ATTR, None)
            if isinstance(tagged, RateLimitStrategy):
                combined[info.path_template] = tagged
        if self._overrides is not None:
            # Fail fast on an override key that matches no route - the silent
            # alternative is a route the operator believes is throttled but is not.
            unknown = sorted(key for key in self._overrides if key not in known)
            if unknown:
                raise ValueError(
                    f"RateLimitMiddleware overrides reference route template(s) {unknown} "
                    "that match no registered route; an override key is the full route "
                    "template as registered, including any blueprint url_prefix "
                    "(for example '/api/login', not '/login')"
                )
            combined.update(self._overrides)
        self._route_strategies = combined
        return combined

    async def _process_legacy(self, request: Request) -> Response | None:
        client = self._bucket_key(request)
        now = time.monotonic()
        cutoff = now - self.window_seconds

        # Periodic eviction sweep - bounded memory across unique client IPs.
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

        # Amortized O(1) eviction - popleft until the oldest stamp is fresh.
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
        state = request._state.get(_RL_STATE_KEY)
        if state is None:
            return response
        if isinstance(state, RateLimitResult):
            self._apply_headers(response, state.limit, state.remaining, state.reset)
            return response
        remaining = self.max_requests - len(state)
        if remaining < 0:
            remaining = 0
        reset = self._reset_after(state, time.monotonic())
        self._apply_headers(response, self.max_requests, remaining, reset)
        return response

    def _bucket_key(self, request: Request) -> str:
        """Pick a bucket key for `request`.

        Falls back through several signals when the transport peer is
        unknown so anonymous traffic does NOT share one global bucket
        (otherwise a single anonymous source could exhaust the limit for
        every other anonymous caller - a trivial DoS).
        """
        client = request.client_host
        if client:
            return f"host:{client}"
        # Right-most X-Forwarded-For hop - the closest proxy is the only
        # trustworthy entry in the chain; left-most entries are
        # attacker-controlled (see development-guardrails.md, Security
        # Parameter Validation).
        xff = request.headers.get(HEADER_X_FORWARDED_FOR, "")
        if xff:
            last = xff.split(",")[-1].strip()
            if last:
                return f"xff:{last}"
        # User-Agent hash - coarse, but partitions anonymous callers by
        # client software when no IP is available. This digest is the ONLY
        # isolation between anonymous callers here, so it must resist chosen
        # collisions: a CRC32 checksum would let an attacker craft a
        # User-Agent that lands in another anonymous caller's bucket and
        # drain their quota. Use a collision-resistant truncated SHA-256.
        ua = request.headers.get(HEADER_USER_AGENT, "")
        if ua:
            return "ua:" + hashlib.sha256(ua.encode("utf-8", "replace")).hexdigest()[:16]
        # Last resort: per-request UUID stashed on _state so a scope-id
        # reuse after GC can't leak a stale deque into a new request.
        # Effectively disables limiting for fully anonymous traffic -
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
        # Ceil so a sub-second remainder reports >=1, never 0 while a client
        # still has to wait (a floored 0.6s would advertise "retry now").
        remaining = math.ceil(bucket[0] + self.window_seconds - now)
        return remaining if remaining > 0 else 0

    def _apply_headers(self, response: Response, limit: int, remaining: int, reset: int) -> None:
        """Attach X-RateLimit-* headers to `response` (draft-ietf-httpapi-ratelimit-headers)."""
        response.headers[HEADER_X_RATELIMIT_LIMIT] = str(limit)
        response.headers[HEADER_X_RATELIMIT_REMAINING] = str(remaining)
        response.headers[HEADER_X_RATELIMIT_RESET] = str(reset)


class HTTPSRedirectMiddleware(Middleware):
    """Redirect HTTP requests to HTTPS.

    Resolves the request scheme in this order:
      1. ASGI scope `"scheme"` if set to `"https"`/`"wss"` (the
         server already terminated TLS).
      2. `X-Forwarded-Proto` header (when a `ProxyFix`-style middleware
         ran upstream this is already the trusted value).
      3. Default `http`.

    Uses 308 Permanent Redirect (RFC 9110 Sec. 15.4.9) so non-GET methods
    preserve their method and body. The earlier `301` form was wrong
    for `POST`/`PUT` callers - those would silently become `GET`.

    Pass `exempt_paths=("/health/", ...)` to serve some paths over plain HTTP
    (prefix match - use a trailing slash to scope to a segment). By default
    `/.well-known/acme-challenge/` is exempt (RFC 8555 Sec. 8.3: the HTTP-01
    challenge MUST be reachable over plain HTTP for certificate issuance and
    renewal); pass `exempt_acme_challenge=False` to drop that default.
    """

    def __init__(
        self,
        *,
        exempt_paths: tuple[str, ...] = (),
        exempt_acme_challenge: bool = True,
        name: str | None = None,
    ) -> None:
        super().__init__(name=name)
        paths = list(exempt_paths)
        if exempt_acme_challenge:
            paths.append("/.well-known/acme-challenge/")
        # Precompute the tuple so the per-request check is a single
        # `str.startswith` over it - no regex, no ReDoS surface.
        self._exempt_paths: tuple[str, ...] = tuple(paths)

    async def process_request(self, request: Request) -> Response | None:
        """Redirect HTTP requests to HTTPS with a 308 status."""
        # Trust ASGI scope first - the server set it based on the actual
        # transport, not a header that anyone could spoof.
        scope_scheme = request.scope.get("scheme") if request.scope else None
        if scope_scheme in (URL_SCHEME_HTTPS, URL_SCHEME_WSS):
            return None
        # Fall back to X-Forwarded-Proto for environments behind a
        # TLS-terminating proxy that doesn't set scope correctly.
        fwd_proto = request.headers.get(HEADER_X_FORWARDED_PROTO, "").lower()
        if fwd_proto == URL_SCHEME_HTTPS:
            return None

        # Exempt configured prefixes (e.g. ACME HTTP-01) from the redirect,
        # after the scheme short-circuits so HTTPS traffic is never affected.
        if self._exempt_paths and request.path.startswith(self._exempt_paths):
            return None

        host = request.headers.get(HEADER_HOST, "localhost")
        url = f"{URL_SCHEME_HTTPS}://{host}{request.path}"
        if request.query_string:
            url += f"?{request.query_string}"
        return RedirectResponse(url, status_code=status.HTTP_308_PERMANENT_REDIRECT)


class SecurityHeadersMiddleware(Middleware):
    """Attach common hardening response headers to every response.

    Set by default:

    - ``X-Content-Type-Options: nosniff`` - stop MIME sniffing.
    - ``X-Frame-Options: DENY`` - block framing (clickjacking).
    - ``Referrer-Policy: strict-origin-when-cross-origin``.

    Off unless configured:

    - ``Strict-Transport-Security`` - pass ``hsts_max_age`` (seconds).
      Browsers honour HSTS only over HTTPS, so it is inert in plain-HTTP
      development, but it is still opt-in because it pins clients to
      HTTPS for the configured lifetime.
    - ``Content-Security-Policy`` - pass ``content_security_policy``.
    - ``Permissions-Policy`` - pass ``permissions_policy``.

    A header a handler already set on the response is left untouched -
    these are defaults, not overrides.
    """

    def __init__(
        self,
        *,
        content_type_options: str | None = HEADER_VALUE_NOSNIFF,
        frame_options: str | None = HEADER_VALUE_DENY,
        referrer_policy: str | None = HEADER_VALUE_STRICT_ORIGIN_WHEN_CROSS_ORIGIN,
        hsts_max_age: int | None = None,
        hsts_include_subdomains: bool = False,
        hsts_preload: bool = False,
        content_security_policy: str | None = None,
        permissions_policy: str | None = None,
        name: str | None = None,
    ) -> None:
        super().__init__(name=name)
        # Resolve the full header set once at construction so the
        # per-response cost is just copying a small, fixed dict.
        headers: dict[str, str] = {}
        if content_type_options:
            headers[HEADER_X_CONTENT_TYPE_OPTIONS] = content_type_options
        if frame_options:
            headers[HEADER_X_FRAME_OPTIONS] = frame_options
        if referrer_policy:
            headers[HEADER_REFERRER_POLICY] = referrer_policy
        if hsts_max_age is not None:
            value = f"max-age={hsts_max_age}"
            if hsts_include_subdomains:
                value += "; includeSubDomains"
            if hsts_preload:
                value += "; preload"
            headers[HEADER_STRICT_TRANSPORT_SECURITY] = value
        if content_security_policy:
            headers[HEADER_CONTENT_SECURITY_POLICY] = content_security_policy
        if permissions_policy:
            headers[HEADER_PERMISSIONS_POLICY] = permissions_policy
        self._headers = headers

    async def process_response(self, request: Request, response: Response) -> Response:
        """Attach security hardening headers to every response."""
        for name, value in self._headers.items():
            # Defaults only - never clobber a value the handler chose. Match
            # case-insensitively: `Response.headers` is a plain dict, so a
            # handler-set lowercase `x-frame-options` must still count as an
            # override of the `X-Frame-Options` default.
            if not header_present(response.headers, name):
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
    Sec. 4.1), so `allow_missing=True` (the default) still blocks every
    browser-driven CSWSH attempt while leaving non-browser clients
    (mobile apps, service-to-service) - which legitimately omit `Origin`
    - able to connect. Set `allow_missing=False` to additionally refuse
    handshakes that carry no `Origin` at all.

    Plain HTTP requests pass straight through - `Origin` enforcement for
    HTTP is `CORSMiddleware`'s job.
    """

    def __init__(
        self, allowed_origins: list[str], allow_missing: bool = True, *, name: str | None = None
    ) -> None:
        super().__init__(name=name)
        self._allowed: frozenset[str] = frozenset(o.rstrip("/").lower() for o in allowed_origins)
        self._allow_all = "*" in self._allowed
        self._allow_missing = allow_missing

    def is_websocket_origin_allowed(self, origin: str) -> bool:
        """Whether a handshake carrying `origin` may proceed.

        Public so the WebSocket dispatch path can apply the check - a
        handshake never reaches an HTTP middleware's `process_request`.
        """
        if self._allow_all:
            return True
        if not origin:
            return self._allow_missing
        return origin.rstrip("/").lower() in self._allowed

    async def process_request(self, request: Request) -> Response | None:
        # HTTP traffic is out of scope - pass through untouched.
        return None
