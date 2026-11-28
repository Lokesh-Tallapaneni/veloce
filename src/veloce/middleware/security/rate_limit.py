"""Rate-limit middleware — per-caller budgets with draft-ietf-httpapi-ratelimit-headers headers."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import logging
import math
import time
import uuid
from collections import deque
from collections.abc import Awaitable
from typing import TYPE_CHECKING, Any, cast

import veloce.status as status
from veloce._constants import (
    HEADER_RETRY_AFTER,
    HEADER_USER_AGENT,
    HEADER_X_FORWARDED_FOR,
    HEADER_X_RATELIMIT_LIMIT,
    HEADER_X_RATELIMIT_REMAINING,
    HEADER_X_RATELIMIT_RESET,
)
from veloce.audit import Finding
from veloce.exceptions import TooManyRequests
from veloce.http.request import Request
from veloce.http.response import Response
from veloce.middleware.base import Middleware
from veloce.principal import current_principal
from veloce.protocols import RateLimitStoreBackend, _require_backend

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Callable, Iterable

    from veloce.audit import AuditContext

from veloce.ratelimit import (
    RATE_LIMIT_ATTR,
    InMemoryRateLimitBackend,
    RateLimitResult,
    RateLimitStrategy,
)

_logger = logging.getLogger(__name__)

# Threads bucket state from process_request to process_response.
_RL_STATE_KEY = "rate_limit_state"


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
        # Sweeping alone bounds nothing: one machine's IPv6 /64 is unbounded.
        self._max_keys = max_keys
        self._buckets: dict[str, deque[float]] = {}
        self.last_sweep = time.monotonic()
        # Lazy so the lock binds to the running loop, not the construction-time one.
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
        # A coarse clock (Windows ticks at ~15ms) can round past the window.
        if remaining > self.window_seconds:
            return self.window_seconds
        return remaining if remaining > 0 else 0


#: Namespace for a `key_func`'s answers: without one, a namer echoing `host:`,
#: `xff:` or `ua:` would share that caller's bucket.
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
        backend: RateLimitStoreBackend | None = None,
        key_func: Callable[[Request], str | None | Awaitable[str | None]] | None = None,
        overrides: dict[str, RateLimitStrategy] | None = None,
        strict_overrides: bool = True,
        max_keys: int = 100_000,
        name: str | None = None,
    ) -> None:
        """Choose the limiter.

        Args:
            max_requests: Requests allowed per window when no `strategy` is given.
            window_seconds: Window length in seconds when no `strategy` is given.
            strategy: The default algorithm; replaces `max_requests`/`window_seconds`.
            backend: Where limiter state lives; process-local by default. Needs `strategy`.
            key_func: Names the caller a request is counted against; may be async and
                may return `None` to fall back to the derived caller.
            overrides: Full route template to strategy, for handlers that cannot be
                decorated with `rate_limit`. Needs `strategy`.
            strict_overrides: Make the startup audit fail on an override naming no route; a
                warning if False.
            max_keys: Most caller keys kept by the default backend.
            name: Name for `exclude_middleware`.

        Raises:
            ValueError: If `max_keys` is below 1, `backend` or `overrides` is given
                without `strategy`, or `max_keys` is set alongside a custom `backend`.
            TypeError: If `key_func` is not callable, an override is not a
                `RateLimitStrategy`, or `backend` lacks an async `evaluate`.

        """
        super().__init__(name=name)
        if max_keys < 1:
            raise ValueError("RateLimitMiddleware max_keys must be >= 1")
        if key_func is not None and not callable(key_func):
            raise TypeError("RateLimitMiddleware key_func must be callable")
        # Settled once so the request path calls `self._key(request)` blind.
        self._key: Callable[[Request], Any] = key_func if key_func is not None else self._bucket_key
        self._key_supplied: bool = key_func is not None
        # One warning per middleware: the condition is a property of the namer.
        self._warned_key_type: bool = False
        self._max_keys = max_keys
        self._strategy = strategy
        # An override key naming no route is a configuration error by default;
        # `strict_overrides=False` downgrades it for a per-deployment router set.
        self._strict_overrides = strict_overrides
        # Last set reported, so a route-table change logs once per rebuild.
        self._reported_unknown: list[str] = []
        # Shared by both construction modes, since a `@rate_limit` tag is honored
        # either way; built lazily so an untagged app never allocates one.
        self._backend: RateLimitStoreBackend | None = None
        self._overrides: dict[str, RateLimitStrategy] | None = None
        # Rebuilt when the route-table generation advances; empty is the fast path.
        self._route_strategies: dict[str, RateLimitStrategy] | None = None
        self._route_strategies_gen: int = -1
        self._log: _SlidingLog | None = None
        if strategy is None:
            if backend is not None:
                raise ValueError("backend requires a strategy; pass strategy= as well")
            if overrides is not None:
                raise ValueError("overrides requires a strategy; pass strategy= as well")
            self._log = _SlidingLog(max_requests, window_seconds, max_keys)
        else:
            # The backend runs the pure strategy under its own atomic RMW.
            if backend is None:
                self._backend = InMemoryRateLimitBackend(max_keys=max_keys)
            else:
                _require_backend(backend, RateLimitStoreBackend, role="rate-limit backend")
                # A backend carries its own `max_keys`; two caps would disagree.
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
        # Awaited on what the call returned, not on how the namer inspects: an
        # un-awaited coroutine keys every request afresh and admits everything.
        if type(caller) is not str and inspect.isawaitable(caller):
            caller = await caller
        if caller is None or (isinstance(caller, (str, bytes)) and not caller):
            # A namer declines with `None` or an empty `str`/`bytes`; keeping that
            # name would pool every declined caller into one exhaustible bucket.
            caller = self._bucket_key(request)
        elif self._key_supplied:
            # A namer returns caller-influenced text, so it is namespaced: an
            # `x-api-key` of `host:1.2.3.4` would else spend that peer's budget.
            if type(caller) is not str:
                # Interpolated, not concatenated, so an `int` tenant id costs a
                # wrong bucket rather than a 500 on every request.
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
        # A `@rate_limit` tag is keyed by route as well as client, so the tagged
        # and default budgets stay independent.
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
        # Right-most X-Forwarded-For hop: the left-most are attacker-controlled.
        xff = request.headers.get(HEADER_X_FORWARDED_FOR, "")
        if xff:
            last = xff.split(",")[-1].strip()
            if last:
                return f"xff:{last}"
        # The ONLY isolation between anonymous callers, so it must resist chosen
        # collisions: a crafted User-Agent could else drain another's quota.
        ua = request.headers.get(HEADER_USER_AGENT, "")
        if ua:
            return "ua:" + hashlib.sha256(ua.encode("utf-8", "replace")).hexdigest()[:16]
        # An MCP call over stdio has no peer, chain or User-Agent. An anonymous
        # principal is skipped: one shared bucket is the mode this chain avoids.
        if request.is_mcp:
            principal = current_principal()
            if principal is not None and principal.subject is not None:
                return f"sub:{principal.subject}"
        # Last resort: a per-request UUID, so a GC-reused scope id cannot leak a
        # stale deque. Fail-open per caller rather than fail-shared across all.
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
        # Not onto a redirect: RFC 9110 Sec. 10.2.3 gives `Retry-After` a different
        # meaning on a 3xx, so a client would sit out the window before following it.
        if not 300 <= rejected.status_code < 400:
            rejected.headers[HEADER_RETRY_AFTER] = str(retry_after)
        self._apply_headers(rejected, limit, 0, reset)
        return rejected
