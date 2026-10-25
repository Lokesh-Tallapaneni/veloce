"""Security-related middleware — the six hardening layers and their helper.

`CSPMiddleware` emits a Content Security Policy and, with `csp_nonce()`, a
per-request nonce a template can put on an inline script.
`TrustedHostMiddleware` validates Host (RFC 9110 Sec. 7.2),
`RateLimitMiddleware` emits rate-limit headers
(draft-ietf-httpapi-ratelimit-headers), `HTTPSRedirectMiddleware` upgrades with
a 308 (RFC 9110 Sec. 15.4.9), `SecurityHeadersMiddleware` sets the common
hardening response headers, and `WebSocketOriginMiddleware` checks Origin
against CSWSH (RFC 6455 Sec. 4.1, Sec. 4.2.2).
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import logging
import math
import secrets
import time
import uuid
from collections import deque
from collections.abc import Awaitable, Mapping, Sequence
from typing import TYPE_CHECKING, Any, cast

import veloce.status as status
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
from veloce._internal import _current_request_var, _extract_host
from veloce._protocol_constants import URL_SCHEME_HTTPS
from veloce.audit import Finding
from veloce.exceptions import TooManyRequests
from veloce.http.request import Request
from veloce.http.response import RedirectResponse, Response, header_present
from veloce.middleware.base import Middleware
from veloce.principal import current_principal

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Callable, Iterable

    from veloce.audit import AuditContext

from veloce.ratelimit import (
    RATE_LIMIT_ATTR,
    InMemoryRateLimitBackend,
    RateLimitBackend,
    RateLimitResult,
    RateLimitStrategy,
)

_logger = logging.getLogger(__name__)

# Threads bucket state from process_request to process_response.
_RL_STATE_KEY = "rate_limit_state"

# request.state slot holding the per-request CSP nonce (materialized lazily).
_CSP_NONCE_STATE_KEY = "csp_nonce"


# ── Content Security Policy ───────────────────────────────
def csp_nonce(request: Request | None = None) -> str | None:
    """Return the per-request CSP nonce, materializing it on first access.

    Pass the request explicitly, or omit it to read the one currently being
    handled. Returns None when `CSPMiddleware` armed no nonce for this request,
    or when there is no request in scope.
    """
    if request is None:
        request = _current_request_var.get()
        if request is None:
            return None
    cached = request._state.get(_CSP_NONCE_STATE_KEY)
    if cached is None:
        return None
    if isinstance(cached, str):
        return cached
    value = cached()
    request._state[_CSP_NONCE_STATE_KEY] = value
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
        # `ValueError`, not `assert`: `python -O` strips asserts, and an empty
        # configuration would then emit no header while the stack claims it does.
        if policy is None and report_only_policy is None:
            raise ValueError("CSPMiddleware requires at least one of policy or report_only_policy")
        self._enforce_template = _normalize_csp(policy) if policy is not None else None
        self._report_template = (
            _normalize_csp(report_only_policy) if report_only_policy is not None else None
        )
        references_nonce = (
            self._enforce_template is not None and "{nonce}" in self._enforce_template
        ) or (self._report_template is not None and "{nonce}" in self._report_template)
        # A nonce-referencing template with nonce generation disabled would
        # render `'nonce-None'`, which a browser parses as a real nonce and
        # which silently breaks the policy. Refuse the construction instead.
        if not nonce and references_nonce:
            raise ValueError(
                "CSPMiddleware was constructed with nonce=False but a policy "
                "references a nonce ('{nonce}' placeholder or 'nonce' source); "
                "enable nonce=True or remove the nonce reference"
            )
        self._needs_nonce = nonce and references_nonce

    async def process_request(self, request: Request) -> Response | None:
        if self._needs_nonce:
            # One-shot factory: a request that never reads it pays only the store.
            request._state[_CSP_NONCE_STATE_KEY] = lambda: secrets.token_urlsafe(16)
        return None

    async def process_response(self, request: Request, response: Response) -> Response:
        # `Response.headers` is a plain dict but HTTP field names are
        # case-insensitive (RFC 9110 Sec. 5.1), and browsers INTERSECT multiple
        # CSP headers - a lowercase route override must still suppress the
        # default, or both ship and the route's policy is silently narrowed.
        for header, template in (
            (HEADER_CONTENT_SECURITY_POLICY, self._enforce_template),
            (HEADER_CONTENT_SECURITY_POLICY_REPORT_ONLY, self._report_template),
        ):
            if template is None or header_present(response.headers, header):
                continue
            if "{nonce}" in template:
                nonce = csp_nonce(request)
                if nonce is None:
                    # No nonce was armed because `process_request` never ran - an
                    # earlier middleware answered. A fixed `nonce-None` would let
                    # an injected `<script nonce="None">` run while the page's own
                    # script was blocked, so mint an unguessable one here.
                    nonce = secrets.token_urlsafe(16)
                    request._state[_CSP_NONCE_STATE_KEY] = nonce
                value = template.replace("{nonce}", f"'nonce-{nonce}'")
            else:
                value = template
            response.headers[header] = value
        return response


# ── Host validation ───────────────────────────────────────
class TrustedHostMiddleware(Middleware):
    """Validates Host header against an allow-list.

    Supports literal hostnames, the catch-all `*`, and subdomain wildcards
    of the form `*.example.com` (matches `api.example.com`,
    `a.b.example.com`, etc. - never the bare `example.com`). Matching is
    case-insensitive; the port portion of `Host:` is stripped before
    comparison (RFC 9110 Sec. 7.2).

    A host that fails the check is refused with `400 Bad Request`, because the
    request named a host this server does not answer for - the request itself is
    malformed, not the credential it carried. Pass `status_code=403` to report it
    as a refusal instead, or any other 4xx an edge proxy or log pipeline keys on.
    """

    def __init__(
        self,
        allowed_hosts: list[str],
        *,
        status_code: int = status.HTTP_400_BAD_REQUEST,
        name: str | None = None,
    ) -> None:
        super().__init__(name=name)
        # `ValueError`, not `assert`: `python -O` strips asserts, and a non-4xx
        # status makes the refusal read as a success, a redirect or a fault.
        if not 400 <= status_code < 500:
            raise ValueError("TrustedHostMiddleware status_code must be a 4xx client error")
        self._status_code = status_code
        # Split at construction so the hot path stays O(1) for the common case.
        self._exact: frozenset[str] = frozenset(
            h.lower() for h in allowed_hosts if not h.startswith("*.")
        )
        # Leading dot baked in so the per-request check is one `endswith`.
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
        return any(host.endswith(suffix) for suffix in self._wildcard_suffixes_dotted)

    async def process_request(self, request: Request) -> Response | None:
        """Reject requests whose Host header is not in the allow-list."""
        # A replayed MCP call carries no Host; the transport POST that carried it
        # already passed this check, so judging the replay rejects every
        # route-backed tool in an application that validates Host at all.
        if request.is_mcp:
            return None
        # Shared with the request-side parser so the IPv6 and port shapes agree.
        host = _extract_host(request.headers.get(HEADER_HOST, ""))
        if not self.is_host_allowed(host):
            return Response(status_code=self._status_code, body=b"Invalid host header")
        return None


# ── Rate limiting ─────────────────────────────────────────
class _SlidingLog:
    """The process-local sliding-log limiter behind the `max_requests=` constructor.

    Owns every piece of state that algorithm needs, so `RateLimitMiddleware`
    holds one limiter object rather than a second limiter's attributes spread
    across its own instance.

    Deliberately not one `evaluate()` call: the periodic sweep must be awaited,
    and folding the whole per-request path behind it would put a coroutine on
    every request. The caller awaits `sweep` only when one is due - once per
    window - and reaches the rest through sync calls.
    """

    __slots__ = (
        "_buckets",
        "_max_keys",
        "_sweep_lock",
        "last_sweep",
        "max_requests",
        "window_seconds",
    )

    def __init__(self, max_requests: int, window_seconds: int, max_keys: int) -> None:
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        # Sweeping alone bounds nothing - keys arriving within one window
        # accumulate, and a single machine's IPv6 /64 makes that unbounded.
        self._max_keys = max_keys
        self._buckets: dict[str, deque[float]] = {}
        self.last_sweep = time.monotonic()
        # Lazy so the lock binds to the running loop, not whichever was current
        # at construction. Guards a check-then-act an `await` would open.
        self._sweep_lock: asyncio.Lock | None = None

    async def sweep(self, now: float, cutoff: float) -> None:
        """Drop every bucket whose newest stamp has aged out. Once per window."""
        if self._sweep_lock is None:
            self._sweep_lock = asyncio.Lock()
        async with self._sweep_lock:
            # Double-check so a request that lost the race does not redo it.
            if now - self.last_sweep >= self.window_seconds:
                stale = [
                    ip for ip, stamps in self._buckets.items() if not stamps or stamps[-1] <= cutoff
                ]
                for ip in stale:
                    del self._buckets[ip]
                self.last_sweep = now

    def bucket(self, key: str, cutoff: float) -> deque[float]:
        """Return `key`'s bucket with expired stamps dropped, creating it if needed."""
        bucket = self._buckets.get(key)
        if bucket is None:
            if len(self._buckets) >= self._max_keys:
                self._evict(cutoff)
            bucket = deque()
            self._buckets[key] = bucket
        # Amortized O(1) eviction - popleft until the oldest stamp is fresh.
        while bucket and bucket[0] <= cutoff:
            bucket.popleft()
        return bucket

    def _evict(self, cutoff: float) -> None:
        """Make room in `_buckets`, dropping stale entries before live ones.

        Expired buckets go first; if the dict is still full, the oldest by
        insertion order goes. Never the key being inserted - evicting the newest
        would let a caller cycling source addresses flush an honest client's
        counter, turning a memory bound into a rate-limit bypass.

        `InMemoryRateLimitBackend._evict` applies the same two-phase policy to
        the strategy path; both must change together.
        """
        stale = [k for k, stamps in self._buckets.items() if not stamps or stamps[-1] <= cutoff]
        for k in stale:
            del self._buckets[k]
        while len(self._buckets) >= self._max_keys:
            del self._buckets[next(iter(self._buckets))]

    def reset_after(self, bucket: deque[float], now: float) -> int:
        """Seconds until the oldest stamp in `bucket` falls out of the window."""
        if not bucket:
            return 0
        # Ceil so a sub-second remainder never advertises "retry now".
        remaining = math.ceil(bucket[0] + self.window_seconds - now)
        # A coarse clock (Windows monotonic ticks at ~15ms) can invert the
        # compare and round the remainder past the window, so clamp to it.
        if remaining > self.window_seconds:
            return self.window_seconds
        return remaining if remaining > 0 else 0


#: Namespace for a `key_func`'s answers. Without a prefix of its own, a namer
#: echoing one of the derivation's keys (`host:`, `xff:`, `ua:`) would share
#: that caller's bucket.
_SUPPLIED_KEY_PREFIX = "key:"


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
    blueprint route includes its `url_prefix` (`/api/login`, not `/login`). An
    explicit `overrides` entry wins over a `rate_limit` tag on the same route.

    Either way, an overridden route gets its own per-client counter, independent
    of the default budget; routes without an override keep the shared default.
    Like `exclude_middleware`, the per-route strategy is resolved against the
    route matched at dispatch entry, so a `before_request` hook that rewrites the
    path does not change which limit applies.

    Counters are keyed by the caller the request came from, derived from the
    transport peer and the proxy chain. Pass `key_func=lambda request: ...` to
    name the caller yourself - an API key, a tenant id, the authenticated
    subject - which is what an application behind a gateway that terminates
    identity, or one serving a door with no client address at all, has to do for
    the limit to mean anything.

    The namer may be `async def`, and may return `None` for a caller it cannot
    name. `None` falls through to the derivation above, so such a caller gets a
    bucket of its own rather than sharing one with every other declined caller.

    Usage::

        from veloce import RateLimitMiddleware, TokenBucket

        app.add_middleware(
            RateLimitMiddleware(
                strategy=TokenBucket(rate=1000, per=60),
                overrides={"/login": TokenBucket(rate=5, per=60)},
            )
        )
    """

    audit_needs_routes = True

    def __init__(
        self,
        max_requests: int = 100,
        window_seconds: int = 60,
        *,
        strategy: RateLimitStrategy | None = None,
        backend: RateLimitBackend | None = None,
        key_func: Callable[[Request], str | None | Awaitable[str | None]] | None = None,
        overrides: dict[str, RateLimitStrategy] | None = None,
        strict_overrides: bool = True,
        max_keys: int = 100_000,
        name: str | None = None,
    ) -> None:
        super().__init__(name=name)
        if max_keys < 1:
            raise ValueError("RateLimitMiddleware max_keys must be >= 1")
        if key_func is not None and not callable(key_func):
            raise TypeError("RateLimitMiddleware key_func must be callable")
        # Settled once so the request path calls `self._key(request)` blind.
        self._key: Callable[[Request], Any] = key_func if key_func is not None else self._bucket_key
        # Decides whether `self._key`'s answer needs namespacing below.
        self._key_supplied: bool = key_func is not None
        # One warning per middleware: the condition is a property of the namer.
        self._warned_key_type: bool = False
        self._max_keys = max_keys
        self._strategy = strategy
        # An override key naming no route is a configuration error by default;
        # `strict_overrides=False` downgrades it for one app that mounts a
        # different set of routers per deployment behind a shared override map.
        self._strict_overrides = strict_overrides
        # Last set reported, so a route-table change logs once per rebuild.
        self._reported_unknown: list[str] = []
        # Shared by both construction modes, since a `@rate_limit` tag is
        # honored either way; built lazily in `max_requests=` mode, so an app
        # with no tagged route never allocates one.
        self._backend: RateLimitBackend | None = None
        self._overrides: dict[str, RateLimitStrategy] | None = None
        # Rebuilt when the route-table generation advances. Empty means the
        # per-request path stays a single client-keyed evaluation.
        self._route_strategies: dict[str, RateLimitStrategy] | None = None
        self._route_strategies_gen: int = -1
        # The process-local sliding log, or `None` when a strategy limits.
        self._log: _SlidingLog | None = None
        if strategy is None:
            # Legacy process-local sliding-log path.
            if backend is not None:
                raise ValueError("backend requires a strategy; pass strategy= as well")
            if overrides is not None:
                raise ValueError("overrides requires a strategy; pass strategy= as well")
            self._log = _SlidingLog(max_requests, window_seconds, max_keys)
        else:
            # The backend runs the pure strategy under its own atomic
            # read-modify-write.
            if backend is None:
                self._backend = InMemoryRateLimitBackend(max_keys=max_keys)
            else:
                # A backend carries its own `max_keys`; accepting both would
                # leave the cap depending on which one the request reached.
                if max_keys != 100_000:
                    raise ValueError(
                        "max_keys applies to the default backend; "
                        "set it on the backend you passed instead"
                    )
                self._backend = backend
            if overrides:
                for route, override in overrides.items():
                    if not isinstance(override, RateLimitStrategy):
                        raise TypeError(f"overrides[{route!r}] must be a RateLimitStrategy")
                self._overrides = dict(overrides)

    async def process_request(self, request: Request) -> Response | None:
        """Enforce per-client request rate limits."""
        strategy = self._strategy
        # The constructor assigns exactly one limiter, so no strategy means log.
        log = self._log if strategy is None else None
        if strategy is None and log is None:
            return None
        caller = self._key(request)
        # Awaited on what the call returned, not on how the namer inspects:
        # `key_func=lambda r: tenant(r)` looks sync and returns a coroutine all
        # the same, and an un-awaited one keys every request to a fresh bucket,
        # so the limiter admits everything. The common path pays a type compare.
        if type(caller) is not str and inspect.isawaitable(caller):
            caller = await caller
        if caller is None or (isinstance(caller, (str, bytes)) and not caller):
            # A namer that cannot name this caller says so with `None` or with
            # an empty `str`/`bytes` - the shapes `headers.get(...) or ""` and
            # `getattr(..., "", "")` produce. Keeping the empty name would put
            # every caller the namer declined into one bucket, which the first
            # of them could then exhaust for all the others.
            caller = self._bucket_key(request)
        elif self._key_supplied:
            # A supplied namer returns caller-influenced text, so it gets a
            # namespace of its own: an `x-api-key` of `host:1.2.3.4` would
            # otherwise land in the derivation's bucket for that peer address,
            # and one caller could spend another's budget.
            if type(caller) is not str:
                # Interpolated, not concatenated, so a tenant id handed over as
                # an `int` costs a wrong bucket and not a 500 per request. An
                # object with no stable `__str__` renders its address, keying
                # every request afresh while the limiter reports itself armed.
                self._warn_unstable_key(caller)
            caller = f"{_SUPPLIED_KEY_PREFIX}{caller}"
        caller = cast("str", caller)
        if strategy is not None:
            return await self._process_strategy(request, strategy, caller)
        assert log is not None
        return await self._process_legacy(request, log, caller)

    def _route_override(self, request: Request) -> RateLimitStrategy | None:
        """Return the strategy this route declares for itself, or `None`.

        Rebuilds the per-route map when the app's route-table generation
        advances, so a route added after the first request is honored, at the
        cost of one int compare per request.
        """
        app = request.app
        gen = app._gen if app is not None else None
        per_route = self._route_strategies
        if per_route is None or gen != self._route_strategies_gen:
            per_route = self._build_route_strategies(request, gen)
        if not per_route:
            return None
        route = request.url_rule
        return per_route.get(route) if route is not None else None

    def _warn_unstable_key(self, caller: Any) -> None:
        """Report a `key_func` answer that is not the `str` the contract names."""
        if self._warned_key_type:
            return
        self._warned_key_type = True
        _logger.warning(
            "RateLimitMiddleware key_func returned %s, not str. The value is used "
            "as text; if it has no stable __str__ every request keys to a new "
            "bucket and nothing is limited. Return a str (or None for a caller "
            "it cannot name).",
            type(caller).__name__,
        )

    async def _process_strategy(
        self, request: Request, strategy: RateLimitStrategy, caller: str
    ) -> Response | None:
        override = self._route_override(request)
        if override is not None:
            strategy = override
            # Scoped so the route keeps a counter separate from the default.
            key = f"{request.url_rule}\x00{caller}"
        else:
            key = caller
        # Wall-clock so the same key on a shared backend agrees across workers.
        assert self._backend is not None
        result = await self._backend.evaluate(key, strategy, time.time())
        if not result.allowed:
            return await self._refuse(request, result.retry_after, result.limit, result.reset)
        request._state[_RL_STATE_KEY] = result
        return None

    def _collect_route_strategies(self, app: Any) -> tuple[dict[str, RateLimitStrategy], set[str]]:
        """Scan `app`'s routes for `@rate_limit` tags, returning them and every template.

        Shared by the startup validation and the per-request build so the two
        cannot drift. `include_hidden=True` so a tag on an
        `include_in_schema=False` route - a login form POST, an internal
        endpoint, the routes most worth throttling - is still discovered.
        """
        tagged_strategies: dict[str, RateLimitStrategy] = {}
        known: set[str] = set()
        for _method, _path, info in app.iter_routes(include_hidden=True):
            known.add(info.path_template)
            tagged = getattr(info.handler, RATE_LIMIT_ATTR, None)
            if isinstance(tagged, RateLimitStrategy):
                tagged_strategies[info.path_template] = tagged
        return tagged_strategies, known

    def _unknown_overrides(self, known: set[str]) -> list[str]:
        """Override keys matching no route template in `known`."""
        if self._overrides is None:
            return []
        return sorted(key for key in self._overrides if key not in known)

    def _report_unknown_overrides(self, known: set[str]) -> None:
        """Log once when an override key matches no route in the current table.

        Whether an unmatched key is fatal is decided at startup by `audit`;
        this is the same question after a route-table change, which only
        happens once the app is already serving - so it reports rather than
        decides. Raising here would turn silencing
        `ratelimit-overrides-unknown` into an outage on every request, which an
        inactive override does not warrant.

        Logged once per set of keys, not per request.
        """
        unknown = self._unknown_overrides(known)
        if not unknown or unknown == self._reported_unknown:
            return
        self._reported_unknown = unknown
        _logger.warning(
            "RateLimitMiddleware overrides reference route template(s) %s that match no "
            "registered route; those overrides are inactive. An override key is the full "
            "route template as registered, including any blueprint url_prefix "
            "(for example '/api/login', not '/login')",
            unknown,
        )

    def audit(self, ctx: AuditContext) -> Iterable[Finding]:
        """Report `overrides` keys that match no registered route.

        A key matching nothing means a route the operator believes is throttled
        but is not. Under `strict_overrides` that is an `error`, so the boot
        fails naming the keys rather than the app serving unthrottled; otherwise
        it is a warning and the overrides are simply inactive.

        `audit_needs_routes` keeps this from running against an app that was
        imported but never started, whose startup routes would read as missing.
        """
        if self._strategy is None or self._overrides is None:
            return ()
        _tagged, known = self._collect_route_strategies(ctx.app)
        unknown = self._unknown_overrides(known)
        if not unknown:
            return ()
        return (
            Finding(
                f"RateLimitMiddleware overrides name route template(s) {unknown} that match "
                "no registered route.",
                severity="error" if self._strict_overrides else "warning",
                fix="correct the override keys, or drop them",
                id="ratelimit-overrides-unknown",
            ),
        )

    def _build_route_strategies(
        self, request: Request, gen: int | None = None
    ) -> dict[str, RateLimitStrategy]:
        # Explicit `overrides` win over a `@rate_limit` decorator tag.
        app = request.app
        if app is None:
            # No app to scan yet; don't cache, so the first real request does.
            return {}
        combined, known = self._collect_route_strategies(app)
        if self._overrides is not None:
            self._report_unknown_overrides(known)
            combined.update(self._overrides)
        self._route_strategies = combined
        self._route_strategies_gen = gen if gen is not None else -1
        return combined

    async def _process_legacy(
        self, request: Request, log: _SlidingLog, caller: str
    ) -> Response | None:
        # A `@rate_limit` tag routes this request through the strategy
        # machinery instead, keyed by route as well as client so the tagged and
        # default budgets stay independent.
        tagged = self._route_override(request)
        if tagged is not None:
            if self._backend is None:
                # The operator's bound, not the constructor default.
                self._backend = InMemoryRateLimitBackend(max_keys=log._max_keys)
            # Delegated, so a tag means one thing in both construction modes.
            return await self._process_strategy(request, tagged, caller)

        now = time.monotonic()
        cutoff = now - log.window_seconds

        # The only await on this path, and reached once per window.
        if now - log.last_sweep >= log.window_seconds:
            await log.sweep(now, cutoff)

        bucket = log.bucket(caller, cutoff)
        if len(bucket) >= log.max_requests:
            reset = log.reset_after(bucket, now)
            return await self._refuse(request, reset or log.window_seconds, log.max_requests, reset)

        bucket.append(now)
        # Stashed so process_response neither re-resolves the key nor races.
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
        # A raw deque is stashed only by the sliding-log path.
        log = self._log
        if log is None:
            return response
        remaining = log.max_requests - len(state)
        if remaining < 0:
            remaining = 0
        reset = log.reset_after(state, time.monotonic())
        self._apply_headers(response, log.max_requests, remaining, reset)
        return response

    def _bucket_key(self, request: Request) -> str:
        """Pick a bucket key for `request`, when no `key_func` named the caller.

        Falls back through several signals when the transport peer is
        unknown so anonymous traffic does NOT share one global bucket
        (otherwise a single anonymous source could exhaust the limit for
        every other anonymous caller - a trivial DoS).
        """
        client = request.client_host
        if client:
            return f"host:{client}"
        # Right-most X-Forwarded-For hop: the closest proxy is the only
        # trustworthy entry; left-most entries are attacker-controlled.
        xff = request.headers.get(HEADER_X_FORWARDED_FOR, "")
        if xff:
            last = xff.split(",")[-1].strip()
            if last:
                return f"xff:{last}"
        # This digest is the ONLY isolation between anonymous callers, so it
        # must resist chosen collisions: a CRC32 would let an attacker craft a
        # User-Agent landing in another caller's bucket and drain their quota.
        ua = request.headers.get(HEADER_USER_AGENT, "")
        if ua:
            return "ua:" + hashlib.sha256(ua.encode("utf-8", "replace")).hexdigest()[:16]
        # An MCP call over stdio has no transport peer, no proxy chain and no
        # User-Agent, so without the principal the UUID below would give every
        # call its own bucket and limit nothing. An anonymous principal is
        # skipped deliberately: one bucket for every unauthenticated agent is
        # the fail-shared mode this chain exists to avoid.
        if request.is_mcp:
            principal = current_principal()
            if principal is not None and principal.subject is not None:
                return f"sub:{principal.subject}"
        # Last resort: a per-request UUID, so a scope id reused after GC cannot
        # leak a stale deque into a new request. Fail-open per caller, which is
        # the right failure mode here, rather than fail-shared across all.
        anon_id = request._state.get("_rl_anon_id")
        if anon_id is None:
            anon_id = uuid.uuid4().hex
            request._state["_rl_anon_id"] = anon_id
        return f"scope:{anon_id}"

    def _apply_headers(self, response: Response, limit: int, remaining: int, reset: int) -> None:
        """Attach X-RateLimit-* headers to `response` (draft-ietf-httpapi-ratelimit-headers)."""
        response.headers[HEADER_X_RATELIMIT_LIMIT] = str(limit)
        response.headers[HEADER_X_RATELIMIT_REMAINING] = str(remaining)
        response.headers[HEADER_X_RATELIMIT_RESET] = str(reset)

    async def _refuse(self, request: Request, retry_after: int, limit: int, reset: int) -> Response:
        """Build the 429 for a caller over its budget, in the app's error format.

        Offered to the app's error handlers first, so one
        `@app.exception_handler(HTTPException)` covers a refused request the way
        it already covers a 404; the plain-text body below is the default when
        nothing is registered.

        `Retry-After` and the `X-RateLimit-*` family are applied after the
        handler, so a handler can restate the body but cannot drop the headers
        that tell the caller when to come back.
        """
        app = request.app
        # The window rides on the exception so a handler can read it.
        exc = TooManyRequests(headers={HEADER_RETRY_AFTER: str(retry_after)})
        handler = app._find_error_handler(request, exc) if app is not None else None
        if app is not None and handler is not None:
            rejected: Response = await app._dispatch_exc_handler(handler, request, exc)
        else:
            rejected = Response(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                body=b"Too Many Requests",
            )
        # Not onto a redirect: RFC 9110 Sec. 10.2.3 gives `Retry-After` a
        # different meaning on a 3xx - the wait before issuing the *redirected*
        # request - so a conforming client would sit out the whole window
        # before following a handler's `RedirectResponse`. Every other status
        # keeps it, the 500 a raising handler degrades to included.
        if not 300 <= rejected.status_code < 400:
            rejected.headers[HEADER_RETRY_AFTER] = str(retry_after)
        self._apply_headers(rejected, limit, 0, reset)
        return rejected


# ── Transport upgrade ─────────────────────────────────────
class HTTPSRedirectMiddleware(Middleware):
    """Redirect HTTP requests to HTTPS.

    Resolves the request scheme in this order:
      1. ASGI scope `"scheme"` if set to `"https"`/`"wss"` (the
         server already terminated TLS).
      2. `X-Forwarded-Proto` header (when a `ProxyFix`-style middleware
         ran upstream this is already the trusted value).
      3. Default `http`.

    Uses 308 Permanent Redirect (RFC 9110 Sec. 15.4.9) so non-GET methods
    preserve their method and body; a `301` turns a `POST` into a `GET`.

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
        # One `str.startswith` per request - no regex, no ReDoS surface.
        self._exempt_paths: tuple[str, ...] = tuple(paths)

    async def process_request(self, request: Request) -> Response | None:
        """Redirect HTTP requests to HTTPS with a 308 status."""
        # A replayed MCP call is not on the wire, so there is no cleartext
        # connection to upgrade and nowhere to send a redirect.
        if request.is_mcp:
            return None
        # `is_secure` also probes the raw transport's TLS object, which
        # re-deriving the scheme here does not - natively-served TLS would then
        # be redirected back to the URL it just answered, an infinite loop.
        if request.is_secure:
            return None
        # `is_secure` refuses an untrusted `X-Forwarded-Proto` because that is a
        # claim about the hop in front, but a redirect guard must accept one or
        # a TLS-terminating proxy that leaves the scope `http` bounces every
        # request. Only where nothing has judged the header: once `ProxyFix` has
        # run it writes the scheme it trusted into the scope, so reading the raw
        # header here would accept a hop it refused - and a TLS-stripping
        # attacker could suppress this redirect by adding one header.
        if "proxy_fix_applied" not in request._state:
            fwd_proto = request.headers.get(HEADER_X_FORWARDED_PROTO, "").lower()
            if fwd_proto == URL_SCHEME_HTTPS:
                return None

        # After the scheme short-circuits, so HTTPS traffic is never affected.
        if self._exempt_paths and request.path.startswith(self._exempt_paths):
            return None

        host = request.headers.get(HEADER_HOST, "localhost")
        url = f"{URL_SCHEME_HTTPS}://{host}{request.path}"
        if request.query_string:
            url += f"?{request.query_string}"
        return RedirectResponse(url, status_code=status.HTTP_308_PERMANENT_REDIRECT)


# ── Hardening headers ─────────────────────────────────────
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

    sets_hardening_headers = True

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
        # Resolved once so the per-response cost is copying a small fixed dict.
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
        # The lowered form is compared against the response's keys per response.
        self._header_items: tuple[tuple[str, str, str], ...] = tuple(
            (name, name.lower(), value) for name, value in headers.items()
        )

    def audit(self, ctx: AuditContext) -> Iterable[Finding]:
        """Note the opt-in headers this instance is not sending.

        `Strict-Transport-Security` and `Content-Security-Policy` stay off
        until asked for, which leaves a registered instance looking hardened
        while sending neither. Informational: an app served over plain HTTP, or
        one whose policy is set at a proxy, is right to leave them off.
        """
        if HEADER_STRICT_TRANSPORT_SECURITY not in self._headers:
            yield Finding(
                "SecurityHeadersMiddleware sends no Strict-Transport-Security, so a browser "
                "can be talked back onto plain HTTP.",
                severity="info",
                fix="pass hsts_max_age=31536000 (one year)",
                id="hsts-not-sent",
            )
        if HEADER_CONTENT_SECURITY_POLICY not in self._headers:
            yield Finding(
                "SecurityHeadersMiddleware sends no Content-Security-Policy, the header that "
                "limits where scripts may load from.",
                severity="info",
                fix="pass content_security_policy=\"default-src 'self'\" and widen from there",
                id="csp-not-sent",
            )

    async def process_response(self, request: Request, response: Response) -> Response:
        """Attach security hardening headers to every response."""
        # Defaults only - never clobber a value the handler chose. Matching is
        # case-insensitive because `Response.headers` is a plain dict, so a
        # handler-set lowercase `x-frame-options` still overrides the default.
        # One lowered-key set rather than `header_present` per default, which
        # scans every response header on the common miss.
        headers = response.headers
        if not headers:
            headers.update(self._headers)
            return response
        present = {key.lower() for key in headers}
        for name, lowered, value in self._header_items:
            if lowered not in present:
                headers[name] = value
        return response


# ── WebSocket origin ──────────────────────────────────────
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
