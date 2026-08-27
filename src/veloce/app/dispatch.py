"""HTTP request dispatch — the per-request pipeline mixed into Veloce.

`handle_request` -> `_dispatch_request` runs the request-phase middleware,
before/after hooks, route resolution, dependency resolution, the handler call,
response shaping, and teardown. A mixin on `Veloce`, so method resolution and the
hot path are unchanged. The dispatch-only module state (`_exc_handler_sig_cache`,
`_MW_RESPONSE_CHAIN_KEY`, and the small request/response helpers) lives here too,
which breaks the core <-> dispatch import cycle.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import time
import traceback
import weakref
from collections.abc import Callable, MutableMapping
from typing import TYPE_CHECKING, Any, get_args, get_origin

import orjson
from pydantic import BaseModel as _PydanticBaseModel

import veloce.status as status
from veloce._constants import (
    HEADER_ACCEPT,
    HEADER_ALLOW,
    HEADER_HOST,
    MIME_TEXT_HTML,
    MIME_TEXT_HTML_UTF8,
    MIME_TEXT_PLAIN,
    MIME_TEXT_PLAIN_UTF8,
    MSG_INTERNAL_SERVER_ERROR,
    MSG_METHOD_NOT_ALLOWED,
    MSG_NOT_FOUND,
    STATE_INJECTED_RESPONSE,
)
from veloce._internal import (
    _UNRESOLVED_JSON_DUMPS,
    MIME_HTML,
    MIME_JSON,
    _close_form_uploads,
    _coerce_bool,
    _current_app_var,
    _current_request_var,
    _extract_host,
    _is_async_callable,
    dumps_for,
    offload,
)
from veloce._model_backend import (
    _HAS_MSGSPEC,
    ModelBackend,
    _msgspec,
    backend_of,
    is_msgspec_struct,
    is_pydantic_model,
    shape_through_model,
)
from veloce._pipeline import (
    CompiledPipeline,
)
from veloce._protocol_constants import (
    HTTP_METHOD_GET,
    HTTP_METHOD_HEAD,
    HTTP_METHOD_OPTIONS,
    TRACE_HEADER_TRACEPARENT,
    TRACE_HEADER_TRACESTATE,
    build_trace_carrier,
)
from veloce.blueprints import _endpoint_blueprint
from veloce.debug import render_traceback_html
from veloce.dependency import DependencyResolver, Depends
from veloce.encoders import orjson_default
from veloce.exceptions import (
    HTTPException,
    http_exception_payload,
)
from veloce.helpers import g
from veloce.http._body import too_large_payload
from veloce.http.request import Request
from veloce.http.response import (
    JSONResponse,
    RedirectResponse,
    Response,
)
from veloce.instrumentation import RequestMetrics
from veloce.middleware import Middleware
from veloce.signals import (
    got_request_exception,
    request_finished,
    request_started,
    request_tearing_down,
)

# ── Dispatch-scoped module state ───────────────────────────


# Cache of `(wants_request, wants_exc)` flags per exception handler - the
# `inspect.signature` walk inside `_call_exc_handler` repeats on every
# raised exception otherwise. WeakKey so handler GC reclaims the entry.
_exc_handler_sig_cache: weakref.WeakKeyDictionary[Callable[..., Any], tuple[bool, bool]] = (
    weakref.WeakKeyDictionary()
)

# Cache of `(wants_request, wants_response)` flags per after-request hook.
# A hook may reasonably be written to take the response alone, both values, or
# neither; passing both unconditionally turned the first shape into a 500.
# WeakKey so hook GC reclaims the entry.
_after_hook_sig_cache: weakref.WeakKeyDictionary[Callable[..., Any], tuple[bool, bool]] = (
    weakref.WeakKeyDictionary()
)

# `request._state` key holding the per-route filtered response-phase
# middleware chain, set by the request phase only when the matched route
# declares `exclude_middleware`. Absent for routes with no exclusions, so
# `_run_response_middleware` keeps walking the full list with zero lookup
# cost beyond a single dict miss.
_MW_RESPONSE_CHAIN_KEY = "_mw_response_chain"


# ── Module helpers ─────────────────────────────────────────


def _prefers_html(request: Request) -> bool:
    """Whether the client prefers an HTML response over plain text.

    Used by the debug traceback page: a browser (`Accept: text/html`) gets the
    rich HTML view, while curl / CLI / programmatic clients (`*/*`, no Accept,
    or an explicit text/plain preference) keep the plain-text traceback. A
    missing Accept header is treated as "no HTML preference" -> plain text,
    preserving the pre-existing debug Content-Type for non-browser clients.
    """
    accept = request.headers.get(HEADER_ACCEPT)
    if not accept:
        return False
    return request.accept_mimetypes.best_match([MIME_TEXT_PLAIN, MIME_TEXT_HTML]) == MIME_TEXT_HTML


def _trace_carrier(request: Request) -> dict[str, str] | None:
    """Inbound W3C trace headers as a carrier dict, or `None` if absent.

    Only `traceparent` / `tracestate` are copied - the dimensions a tracing
    bridge needs to continue a distributed trace - keeping the framework core
    free of any OpenTelemetry dependency. Returns `None` (not an empty dict)
    when no `traceparent` is present so the bridge can cheaply skip extraction.
    """
    traceparent = request.headers.get(TRACE_HEADER_TRACEPARENT)
    if traceparent is None:
        return None
    return build_trace_carrier(traceparent, request.headers.get(TRACE_HEADER_TRACESTATE))


def _is_msgspec_payload(result: Any) -> bool:
    """Whether a handler return value should be encoded by msgspec.

    A `msgspec.Struct`, or a non-empty `list` whose first element is one. A
    `tuple` is deliberately excluded - it is the `(body, status[, headers])`
    response idiom and must not be encoded as a JSON array; `_coerce_response`
    handles the tuple and recurses on its body, at which point a lone struct
    reaches this predicate.
    """
    if is_msgspec_struct(type(result)):
        return True
    if isinstance(result, list) and result:
        return is_msgspec_struct(type(result[0]))
    return False


def _is_struct_list_model(model: Any) -> bool:
    """Whether `model` is `list[SomeStruct]` (a msgspec list response_model)."""
    if get_origin(model) is list:
        args = get_args(model)
        return bool(args) and is_msgspec_struct(args[0])
    return False


def _adapt_hook_kwargs(
    fn: Callable,
    cache: MutableMapping[Any, tuple[bool, bool]],
    second_name: str,
    request: Request,
    second_value: Any,
) -> dict[str, Any]:
    """Select the kwargs `fn` accepts, from `request` and one other value.

    After-request hooks and exception handlers are the same adapter: both take
    `request` and one more parameter (`response` / `exc`), either by name or via
    `**kwargs`, and both cache the answer per callable. Written out twice, the
    copies drifted - only one of them handled `**kwargs`, so an exception handler
    declared `def handler(**kwargs)` was called with an empty dict and could not
    see the exception it was handling.

    A plain function, not a coroutine: it is called from inside an existing
    `await` on the response path, and wrapping it would add a coroutine per hook
    per request for a dict build.
    """
    # The read is guarded as well as the write below. The caches are
    # `WeakKeyDictionary`s, so a callable that cannot be weakly referenced - a
    # method descriptor such as `str.upper`, say - raises `TypeError` on lookup,
    # not only on insert. Only the write was guarded, so registering one as a
    # hook answered `500` on every request. Such a callable is simply not
    # cached: its signature is resolved per call, which is the cost the guard on
    # the write was already accepting.
    try:
        flags = cache.get(fn)
    except TypeError:
        flags = None
    if flags is None:
        params = inspect.signature(fn).parameters
        # A callable taking `**kwargs` accepts whatever is offered.
        if any(p.kind is p.VAR_KEYWORD for p in params.values()):
            flags = (True, True)
        else:
            flags = ("request" in params, second_name in params)
        with contextlib.suppress(TypeError):
            cache[fn] = flags
    wants_request, wants_second = flags
    kwargs: dict[str, Any] = {}
    if wants_request:
        kwargs["request"] = request
    if wants_second:
        kwargs[second_name] = second_value
    return kwargs


class DispatchMixin:
    """The per-request HTTP dispatch pipeline, mixed into Veloce.

    Not slotted: the first-request latch assigns host state (`_setup_locked`,
    `_first_request_fired`) onto the composed `Veloce`, which carries a `__dict__`.
    """

    if TYPE_CHECKING:  # pragma: no cover
        # Attributes / methods the host application (Veloce) provides.
        config: Any
        logger: Any
        debug: bool
        match: Callable[..., Any]
        make_default_options_response: Callable[..., Any]
        _find_scoped_exception_handler: Callable[..., Any]
        _find_scoped_status_handler: Callable[..., Any]
        _instrumentation: Any
        _middlewares: Any
        log_exception: Callable[..., Any]
        _handler_json_dumps: Any
        _resolve_handler_json_dumps: Callable[..., Any]
        _should_propagate_exceptions: Callable[..., Any]
        _setup_locked: bool
        _setup_lock_enabled: bool
        _first_request_fired: bool
        _first_request_lock: Any
        _before_first_request_hooks: Any
        _before_request_hooks: Any
        _after_request_hooks: Any
        _bp_before_hooks: Any
        _bp_after_hooks: Any
        _teardown_request_hooks: Any
        _bp_teardown_hooks: Any
        _teardown_appcontext_hooks: Any
        _url_value_preprocessors: Any
        _bp_url_value_preprocessors: Any
        _ensure_pipeline: Callable[..., Any]
        _setup_openapi: Callable[..., Any]
        _openapi_setup: bool
        spawn: Callable[..., Any]
        _run_teardown_hooks: Callable[..., Any]
        _select_teardown_request_hooks: Callable[..., Any]
        get_allowed_methods: Callable[..., Any]
        _dependency_overrides: Any
        _override_subplans: Any
        _instrumentation_excludes: Any
        _mounted_apps: Any
        _static_handlers: Any
        _mw_version: Any
        redirect_slashes: bool

    # ── Entry point and core dispatch ──────────────────────

    async def _body_too_large_response(
        self, request: Request, cp: CompiledPipeline | None, max_size: int | None
    ) -> Response:
        """Build the 413 for an over-`MAX_CONTENT_LENGTH` body, run the response phase.

        Shared by the eager declared/buffered check and the streamed-body drain so
        both reject with the identical `{detail, status_code, limit}` payload.
        """
        # Encoded against `self` rather than through `JSONResponse`'s own
        # resolution: this runs before the app contextvar is bound, so the
        # dialect was applied only when a previous request on the same task had
        # left it set.
        response: Response = JSONResponse._from_encoded(
            dumps_for(self, too_large_payload(max_size))
        )
        response.status_code = status.HTTP_413_REQUEST_ENTITY_TOO_LARGE
        # No route matched on a reject, so no per-route exclusion chain exists.
        if cp is not None and cp.http_post is not None:
            response = await self._run_response_phase(cp.http_post, request, response, False)
        return response

    async def handle_request(
        self, request: Request, cp: CompiledPipeline | None = None, match: Any = None
    ) -> Response:
        """Handle one request - run the middleware chain, then route dispatch.

        `cp` is the compiled pipeline for this request. `__call__` already
        resolves it (to gate the ASGI wrapper stack) and threads it in so the
        generation check runs once per request, not once here and once there.
        A caller that reaches this method directly (a mounted sub-app, the
        public `dispatch_request` aliases) passes `None` and the pipeline is
        resolved here.
        """
        if cp is None:
            cp = self._ensure_pipeline()
        # Lazy OpenAPI setup (ensures routes exist on first request regardless of entry point)
        if not self._openapi_setup:
            self._setup_openapi()

        # Inject app reference into request
        request.app = self

        # `current_app` / `request` contextvars + per-request g reset.
        # Letting the contextvar fall through naturally when the request
        # task ends is intentional - async dispatch may span tasks that
        # diverge from a `set/reset` token.
        _current_app_var.set(self)
        _current_request_var.set(request)
        g._reset()

        # Inline the no-subscriber guard so a request on an app with no
        # `request_started` receivers pays neither the method call nor the
        # `**kwargs` pack `Signal.send` would otherwise allocate before its own
        # internal short-circuit. A dead-weakref-only `_subs` just defers
        # pruning to the next send, which `send` already handles lazily.
        if request_started._subs:
            try:
                request_started.send(self, request=request)
            except Exception:
                self.logger.exception("request_started signal receiver raised")

        # Drain `before_first_request` hooks exactly once AND decide the setup
        # lock - both keyed off the single `_first_request_fired` latch. The
        # double-check under the lock is the canonical pattern: the unlocked
        # check short-circuits the common (already serving) case without
        # acquiring the lock; the locked check guarantees single-fire when
        # concurrent first requests race. The lock decision runs regardless of
        # whether `before_first_request` hooks exist, so late registration is
        # rejected on every app, not only ones with hooks.
        if not self._first_request_fired:
            if self._first_request_lock is None:
                self._first_request_lock = asyncio.Lock()
            async with self._first_request_lock:
                if not self._first_request_fired:
                    for hook in self._before_first_request_hooks:
                        await self._call_handler(hook, {})
                    # Freeze setup outside DEBUG/TESTING (and when the lock has
                    # been disabled, e.g. by the in-memory TestClient) so
                    # hot-reload and test monkeypatching can keep registering.
                    # Coerce both flags the same way the `debug` property does
                    # so a string `DEBUG=false`/`TESTING=false` from a dotenv
                    # file leaves setup locked instead of staying open on the
                    # truthy raw string.
                    self._setup_locked = self._setup_lock_enabled and not (
                        self.debug or _coerce_bool(self.config.get("TESTING"))
                    )
                    self._first_request_fired = True

        # Enforce MAX_CONTENT_LENGTH. Check both the declared
        # Content-Length (cheap reject) and the actually-buffered body size
        # (defence-in-depth when no Content-Length was sent). Per
        # RFC 9110 Sec. 15.5.14, the status is 413 Content Too Large.
        # Skipped when the transport already applied this app's limit to both
        # the declared and the received length - which both shipped transports
        # do. It still runs for a request that reached dispatch another way: a
        # mounted sub-app (a fresh `Request`, and possibly a smaller limit), or
        # a caller invoking `handle_request` directly.
        max_size = None if request._length_enforced else self.config.get("MAX_CONTENT_LENGTH")
        if max_size is not None:
            declared = request.content_length
            over = declared is not None and declared > max_size
            # For an in-memory request the body is already buffered, so the
            # await resolves immediately and we enforce against the actual
            # bytes (defence-in-depth for bodies that omit Content-Length).
            # For a streamed request (raw HTTP/1.1) the body has NOT arrived
            # yet - draining it here would defeat streaming and force the
            # whole body into memory. The protocol already caps the streamed
            # running total and the body source raises 413 mid-read, so the
            # declared-length check above is the only eager enforcement.
            if not over and request._body_drained:
                buffered = await request.body()
                over = len(buffered) > max_size
            if over:
                return await self._body_too_large_response(request, cp, max_size)

        # Time the dispatch only when instrumentation hooks are registered -
        # an un-instrumented app does not even read the clock. The fused finish
        # slot is `None` exactly when `self._instrumentation` is empty.
        instrument = cp.http_finish is not None
        started = time.perf_counter() if instrument else 0.0

        try:
            # If @app.middleware("http") funcs are registered, wrap dispatch
            # in the call_next chain.
            if cp.http_around is not None:
                response = await self._run_http_middleware_chain(request, cp)
            else:
                response = await self._dispatch_request(request, cp, match)
        except Exception as exc:
            # Dispatch propagated an exception (e.g. PROPAGATE_EXCEPTIONS is
            # set). Record a `500` metric before the exception continues
            # out, so error requests are never dropped from observability.
            if instrument:
                # `_dispatch_request` records the originating exception's class
                # name on request state before re-raising; an exception raised
                # outside it (e.g. in `@app.middleware("http")`) leaves it
                # unset, so fall back to the caught exception here. Either way
                # only the low-cardinality class name reaches the metric.
                request._state.setdefault("_error_type", type(exc).__qualname__)
                with contextlib.suppress(Exception):
                    await self._run_instrumentation(
                        request,
                        status.HTTP_500_INTERNAL_SERVER_ERROR,
                        (time.perf_counter() - started) * 1000.0,
                        end_time_ns=time.time_ns(),
                    )
            raise

        # Capture the wall-clock end the instant dispatch returned - before
        # the request_finished receivers and instrumentation hooks run - so a
        # tracing bridge can anchor an accurate span window regardless of how
        # long a slow earlier hook/receiver takes.
        if instrument:
            end_time_ns = time.time_ns()
            duration_ms = (time.perf_counter() - started) * 1000.0

        # Signal: request finished. Sender is the app, `response=` is the
        # final Response, `request=` lets a receiver correlate with the
        # matching `request_started`. Receivers may peek but not replace.
        if request_finished._subs:
            try:
                request_finished.send(self, response=response, request=request)
            except Exception:
                self.logger.exception("request_finished signal raised an exception")

        if instrument:
            # A HEAD response never iterates its body (the ASGI path sends
            # headers + an empty terminal frame), so its timing/status are
            # already final at this point - it is NOT a live stream even when
            # the underlying response object is a streaming type.
            is_streamed = response.is_streamed and request.method != HTTP_METHOD_HEAD
            await self._run_instrumentation(
                request,
                response.status_code,
                duration_ms,
                streamed=is_streamed,
                end_time_ns=end_time_ns,
            )

        return response

    async def _run_http_middleware_chain(self, request: Request, cp: CompiledPipeline) -> Response:
        """Run @app.middleware('http') functions with call_next pattern.

        `cp.http_around` is the fused tuple of registered functions (the around
        phase has one spec, so the slot holds the tuple directly); it is `None`
        when none are registered, in which case the caller does not reach here -
        so the slot is read directly with no per-request `None` guard.
        """
        funcs: tuple[Callable, ...] = cp.http_around  # type: ignore[assignment]

        def _make_next(level: int) -> Callable:
            _called = False

            async def call_next(req: Request) -> Response:
                nonlocal _called
                if _called:
                    raise RuntimeError("call_next() was called more than once")
                _called = True
                if level + 1 < len(funcs):
                    return await funcs[level + 1](req, _make_next(level + 1))
                return await self._dispatch_request(req, cp)

            return call_next

        return await funcs[0](request, _make_next(0))

    async def _dispatch_request(
        self, request: Request, cp: CompiledPipeline, match: Any = None
    ) -> Response:
        """Core request dispatch - middleware, routing, handler execution.

        The per-request hot path, and **deliberately inline**: extracting a
        phase out of this loop measured about 5% per request, which is why the
        cold branches were moved out of `_asgi_app` and these were not. What is
        here is here on purpose.

        This is not a thin orchestrator delegating to per-phase helpers: the
        method is over three hundred lines and the phases are inline. Reading
        it means reading it, not following calls out.

        The `try/finally` owns the per-request teardown state (`_exc`,
        `_bp_name`, `resolver`) that the `finally` block reads; that is the
        reason the state is bound before the `try` rather than where it is
        first used.
        """
        _exc: Exception | None = None
        # Whether a background task took ownership of releasing this request's
        # upload spool files. Bound before the `try` so the `finally` can read
        # it even when dispatch raises before any task could be scheduled.
        _scheduled_bg = False
        _bp_name: str | None = None
        # Resolver allocation is deferred until a non-trivial route demands
        # it. A trivial-plan route (no injected params, no dependencies)
        # never touches the resolver, so allocating one upfront - plus its
        # internal dict / WeakKeyDictionary / list members - would be pure
        # waste for the static-GET hot path. Per-request fresh allocation
        # is still preserved: a single shared resolver would let one
        # request's `reset()` clobber another's `yield`-teardown stack
        # (matches the per-connection resolver the WebSocket path uses).
        resolver: DependencyResolver | None = None
        # Whether this request stashed a per-route filtered response chain under
        # `_MW_RESPONSE_CHAIN_KEY`. Carried as a local so the response phase skips
        # the per-response `in request._state` membership probe on the common
        # no-exclusion path and dispatches straight to the fused chain.
        excluded = False
        try:
            # Match the route once - before the middleware request phase so a
            # route's `exclude_middleware` opt-out can be honoured. The same
            # match object is reused for dispatch below; `request.endpoint`
            # and `url_rule` are populated here so before_request hooks can
            # gate on the route name. The ASGI transport matches the route
            # before building the request (to decide eager-vs-streaming body
            # handling) and threads the result in here, so the radix tree is
            # walked once per request, not twice.
            _matched_path = request.path
            _matched_method = request.method
            if match is None:
                match = self.match(_matched_method, _matched_path)
            if match is not None:
                request.endpoint = match.route_info.name
                request._state["url_rule"] = match.route_info.path_template

            # Straight-line fast path: with no app-level feature active
            # (`cp.is_bare`) and a fast-eligible matched route, the request-phase
            # middleware, before/after hooks, route re-resolution, and dependency
            # resolver are all no-ops, so invoke the handler directly and skip
            # the orchestration. Coercion (`_build_response`), one-shot
            # `after_this_request` callbacks (`_run_after_hooks`), background-task
            # scheduling, and the surrounding exception ladder / teardown stay
            # shared with the slow path below, so behaviour is identical. The
            # response phase is skipped because `is_bare` guarantees `http_post`
            # is `None` (no response middleware to run).
            if cp.is_bare and match is not None and match.route_info.is_fast_eligible:
                route_info = match.route_info
                # The handler reaches its path params through
                # `request.path_params`; the slow path assigns this in
                # `_resolve_route`, which the fast path skips. `is_bare`
                # guarantees no url-value preprocessors and `is_fast_eligible`
                # no route `defaults`, so the raw match params are final.
                request.path_params = match.path_params
                # One truthiness test in the common case. `is_bare` covers the
                # app-level processors (they apply to every endpoint); a
                # blueprint's apply to its own routes only, so they no longer
                # cost the whole app its fast path - just this lookup.
                if cp.bp_url_procs is not None:
                    bp = _endpoint_blueprint(route_info.name)
                    bp_procs = cp.bp_url_procs.get(bp) if bp is not None else None
                    if bp_procs is not None:
                        for proc in bp_procs:
                            proc(route_info.name, request.path_params)
                if route_info.is_request_only_plan:
                    result = await route_info.handler(**{route_info.request_param_name: request})
                else:
                    result = await route_info.handler()
                # `_build_response` reduced to this one call on this path, and
                # every other line of it was dead work. `is_fast_eligible`
                # (routing/router.py) is set only when `response_model is None`,
                # `response_class is None` and `status_code == HTTP_200_OK`, so
                # its three tests can never fire; and it requires a trivial or
                # request-only plan, which by definition carries no `Response`
                # slot and no dependencies - the only writers of
                # `STATE_INJECTED_RESPONSE` - so the injection merge cannot fire
                # either. `test_fast_path_response_agreement` pins that, so a
                # future loosening of `is_fast_eligible` fails rather than
                # silently skipping work this path still needs.
                response = self._coerce_response(result)
                # `is_bare` guarantees the app/blueprint after_request hooks are
                # empty, so the only work `_run_after_hooks` could do here is
                # drain one-shot `after_this_request` callbacks. Probe for them
                # inline and await the helper only when present, so the common
                # no-callback request pays no extra coroutine on the fast path.
                if request._state and request._state.get("_after_this_request"):
                    response = await self._run_after_hooks(request, response, None)
                # Returning from inside the `try` still runs the `finally`
                # below, so the release decision is recorded here and taken
                # there - one rule for every exit rather than two.
                _scheduled_bg = self._schedule_background_tasks(request, response)
                return response

            # Phase: request-phase middleware. Skipped entirely - no awaited
            # frame - when no middleware applies (the common case). Runs even on a
            # route miss so e.g. a CORS preflight is still answered.
            if cp.http_pre is not None or (
                match is not None and match.route_info.excluded_middleware is not None
            ):
                early_response, excluded = await self._run_request_phase(request, match, cp)
                if early_response is not None:
                    return early_response

            # Run before_request hooks (app-level then matched blueprint).
            # A non-None return short-circuits. `_bp_name` is recorded as the
            # matched blueprint so the `finally`-block teardown hooks fire for
            # the right blueprint even when dispatch short-circuits before the
            # final match is resolved. With no before_request hooks registered the
            # helper cannot short-circuit, so skip the coroutine await - but still
            # derive `bp_name` (the cheap sync work the helper does), because
            # `_resolve_route` can exit early (subdomain/host/slash/404) before the
            # recompute below, and the `finally` blueprint teardown needs it.
            if self._before_request_hooks or self._bp_before_hooks:
                early, _bp_name = await self._run_before_hooks(request)
                if early is not None:
                    return early
            else:
                _bp_name = _endpoint_blueprint(request.endpoint)

            # Resolve the route - handles mounted sub-apps, static files,
            # the re-match-after-hook-rewrite case, subdomain/host
            # constraints, slash redirects, and 404/405. Returns either a
            # terminal Response (already through response middleware) or the
            # match to dispatch.
            resolved = await self._resolve_route(
                request, match, _matched_path, _matched_method, cp, excluded
            )
            if isinstance(resolved, Response):
                return resolved
            match = resolved
            _bp_name = _endpoint_blueprint(request.endpoint)

            # The response-phase chain is NOT refreshed from the final matched
            # route. Per-route middleware exclusion is keyed on the route matched
            # at dispatch entry - the same match the request phase used - so the
            # exact set of middleware that ran `process_request` is the set that
            # runs `process_response`, even when a before_request hook rewrites
            # request.path / method and `_resolve_route` re-matches to a route
            # with a different `exclude_middleware`. Refreshing here would make
            # request and response phases use different chains, leaving a
            # middleware that paired setup in `process_request` without its
            # teardown in `process_response` (or vice versa). The response chain
            # stashed during the request phase above is therefore authoritative.

            # Resolve dependencies first and bind the resolver to this frame
            # *before* calling the handler - if the handler raises, the
            # `finally` block still sees the resolver and runs its
            # yield-dependency teardowns.
            kwargs, resolver = await self._resolve_dependencies(request, match)
            route_info = match.route_info
            result = await self._call_handler(
                route_info.handler,
                kwargs,
                is_coro=(
                    route_info.handler_plan.is_coro if route_info.handler_plan is not None else None
                ),
            )

            # Apply response_model, coerce, and merge any injected response.
            response = self._build_response(request, match, result)

            # Run after_request hooks (app + blueprint) and one-shot
            # `after_this_request` callbacks. With no app/blueprint hooks and no
            # one-shot callback registered the helper does no work, so skip the
            # awaited frame - mirrors the before_hooks guard above and the
            # fast-path probe. The blueprint branch only fires when both
            # `_bp_after_hooks` and a matched `_bp_name` are present.
            if (
                self._after_request_hooks
                or (self._bp_after_hooks and _bp_name is not None)
                or (request._state and request._state.get("_after_this_request"))
            ):
                response = await self._run_after_hooks(request, response, _bp_name)

            # Schedule any background tasks (DI-injected queue + the
            # response-attached task) in fire-and-forget fashion. When one was
            # scheduled it owns releasing the request's upload spool files,
            # because it may still be reading them.
            _scheduled_bg = self._schedule_background_tasks(request, response)

            # Fused response phase. The slot is `None` when no middleware is
            # registered, so the whole block is skipped with no awaited no-op.
            # The common no-exclusion path iterates the compile-time reversed
            # chain inline here (no helper frame, no `_MW_RESPONSE_CHAIN_KEY`
            # membership probe); a route that stashed a filtered chain takes the
            # dynamic walk so its opt-out is honoured.
            http_post = cp.http_post
            if http_post is not None:
                if excluded:
                    response = await self._run_response_middleware(request, response)
                else:
                    for process_response in http_post:
                        response = await process_response(request, response)
            return response

        except HTTPException as exc:
            _exc = exc
            # Status-code handler wins over class handler; class handler walks
            # the MRO so e.g. registering on `HTTPException` catches `NotFound`.
            # Both prefer a handler on the failing request's blueprint chain
            # before the app-level tables, so a blueprint's handler stays scoped
            # to its own routes.
            handler = self._find_scoped_status_handler(
                exc.status_code, request
            ) or self._find_scoped_exception_handler(type(exc), request)
            if handler:
                return await self._run_exc_handler_response(request, exc, handler, cp, excluded)
            return await self._default_http_exception_response(request, exc, cp, excluded)
        except Exception as exc:
            _exc = exc
            handler = self._find_scoped_exception_handler(type(exc), request)
            if handler:
                return await self._run_exc_handler_response(request, exc, handler, cp, excluded)
            # Record the exception's low-cardinality class name (never the
            # message) so the post-dispatch instrumentation hook can surface it
            # as `RequestMetrics.error_type` without the exception object reaching
            # the observability layer.
            request._state["_error_type"] = type(exc).__qualname__
            # PROPAGATE_EXCEPTIONS (or implicit DEBUG+TESTING) lets the exception
            # escape so test suites see real tracebacks; kept inline so the bare
            # `raise` re-raises the active exception with its original traceback.
            if self._should_propagate_exceptions():
                raise
            # Log it here or it is lost. The response is a generic 500 and the
            # exception does not leave the app, so an ASGI server's error
            # logging never sees it and the native server has nothing to catch
            # either - an unhandled failure would reach production with no
            # record anywhere. `got_request_exception` fires below, but only
            # for an app that subscribed to it; this is the default.
            self.log_exception(exc, request)
            return await self._shape_server_error(request, exc, cp, excluded)
        finally:
            # Yield-dependency teardowns first - they conceptually wrap the
            # request (the resource was acquired before the handler ran and
            # must be released regardless of outcome). `run_teardowns`
            # re-raises aggregated teardown failures (PEP 654 group). A
            # failure here happens after the response was built, so by
            # default it must not break the response cycle - but it must
            # not vanish either: the post-yield code is where commits and
            # releases live. It is logged, then surfaced through
            # `got_request_exception` so error trackers see it, and under
            # PROPAGATE_EXCEPTIONS (or implicit DEBUG+TESTING) it re-raises
            # so test suites fail on a broken teardown instead of passing
            # on the already-built response. `run_teardowns` is async; the
            # common no-yield-dep case has an empty stack, so skip the
            # coroutine + await entirely.
            # A yield-teardown failure that PROPAGATE_EXCEPTIONS must re-raise is
            # deferred to the end of this `finally`, so the teardown hooks and
            # signals below still run - the teardown contract holds even when the
            # thing that failed is a yield-dependency teardown.
            _teardown_to_propagate: BaseException | None = None
            if resolver is not None and resolver._teardowns:
                try:
                    await resolver.run_teardowns(_exc)
                except Exception as teardown_exc:
                    self.logger.exception("yield-dependency teardown raised")
                    if got_request_exception._subs:
                        try:
                            got_request_exception.send(self, exception=teardown_exc)
                        except Exception:
                            self.logger.exception("signal receiver raised an exception")
                    if self._should_propagate_exceptions():
                        _teardown_to_propagate = teardown_exc

            # Teardown hooks - always run, even on exceptions. The cheap
            # attribute guard stays inline so a request with no teardown hooks
            # pays zero extra coroutine awaits on the hot path; only once hooks
            # exist is the shared selector consulted. The MCP tool path replays
            # the identical teardown via `_run_request_teardown`.
            if self._teardown_request_hooks or self._bp_teardown_hooks:
                _td_hooks = self._select_teardown_request_hooks(_bp_name)
                if _td_hooks:
                    await self._run_teardown_hooks(_td_hooks, _exc, "teardown_request")

            # Release the spool files a multipart parse opened. An upload past
            # the spool threshold is a real file on disk, and nothing closed it
            # once the response was sent. Skipped when a background task was
            # scheduled: it may still be reading them, and releases them itself.
            if not _scheduled_bg and request._form is not None:
                request._close_uploads()

            # `teardown_appcontext` fires when the app context pops; in
            # veloce that happens at the end of each request (no separate
            # app/request context split). Hooks receive the exception or
            # None. Errors are logged, never re-raised.
            if self._teardown_appcontext_hooks:
                await self._run_teardown_hooks(
                    self._teardown_appcontext_hooks, _exc, "teardown_appcontext"
                )

            # Signals: fire `got_request_exception` first when an exc bubbled
            # up, then always fire `request_tearing_down`. Receivers may
            # raise - log + continue so a buggy listener doesn't poison
            # the dispatch path. Names hoisted to module top. Inline the
            # no-subscriber guards so the common no-receiver request skips the
            # method call + `**kwargs` pack before `Signal.send`'s own internal
            # short-circuit; a dead-weakref-only `_subs` just defers pruning.
            if (_exc is not None and got_request_exception._subs) or request_tearing_down._subs:
                try:
                    if _exc is not None and got_request_exception._subs:
                        got_request_exception.send(self, exception=_exc)
                    if request_tearing_down._subs:
                        request_tearing_down.send(self, exc=_exc)
                except Exception:
                    self.logger.exception("signal receiver raised an exception")

            # Deferred from the yield-teardown block above: re-raise the teardown
            # failure now that the teardown hooks and signals have all run.
            if _teardown_to_propagate is not None:
                raise _teardown_to_propagate

    # ── Request phase and error shaping ────────────────────

    async def _run_request_phase(
        self, request: Request, match: Any, cp: CompiledPipeline
    ) -> tuple[Response | None, bool]:
        """Run the request-phase middleware; return `(early_response, excluded)`.

        A route declaring `exclude_middleware` runs a memoised filtered chain and
        stashes the matching response-phase chain on `request._state` for
        symmetric skip; otherwise the compile-time fused `process_request` chain
        runs. A returned non-`None` response has already been through the response
        phase and should be returned to the client as-is. `excluded` is `True`
        only on the filtered-chain path, so the response phase mirrors the skip.
        """
        if match is not None and match.route_info.excluded_middleware is not None:
            # `_route_middleware_chains` only returns `None` when the route
            # excludes nothing, which the caller's guard already ruled out.
            filtered = self._route_middleware_chains(match.route_info)
            request._state[_MW_RESPONSE_CHAIN_KEY] = filtered[1]  # type: ignore[index]
            for mw in filtered[0]:  # type: ignore[index]
                early = await mw.process_request(request)
                if early is not None:
                    return await self._run_response_middleware(request, early), True
            return None, True
        # Reached only when `cp.http_pre` is set (the caller's guard); `or ()`
        # keeps the loop typed without a redundant None-check.
        for process_request in cp.http_pre or ():
            early = await process_request(request)
            if early is not None:
                return await self._run_response_phase(cp.http_post, request, early, False), False
        return None, False

    async def _run_exc_handler_response(
        self,
        request: Request,
        exc: Exception,
        handler: Callable,
        cp: CompiledPipeline,
        excluded: bool,
    ) -> Response:
        """Run a registered exception handler and apply the response phase."""
        response = await self._dispatch_exc_handler(handler, request, exc)
        response = await self._run_response_phase(cp.http_post, request, response, excluded)
        return response

    async def _default_http_exception_response(
        self, request: Request, exc: HTTPException, cp: CompiledPipeline, excluded: bool
    ) -> Response:
        """Shape an unhandled `HTTPException` into the default JSON error body.

        A `ValidationError` / `RequestValidationError` carries a structured
        `.errors` list - emitted verbatim as `{"detail": [...]}` - rather than
        the stringified repr stored in `exc.detail`.
        """
        response: Response = JSONResponse(
            http_exception_payload(exc),
            status_code=exc.status_code,
            headers=exc.headers,
        )
        response = await self._run_response_phase(cp.http_post, request, response, excluded)
        return response

    async def _shape_server_error(
        self, request: Request, exc: Exception, cp: CompiledPipeline, excluded: bool
    ) -> Response:
        """Shape an unhandled, non-propagated exception into a 500 response.

        In debug mode this serves the rich HTML traceback to an HTML client and
        the plain-text traceback to curl / CLI / programmatic clients, keeping
        the debug-mode Content-Type contract unchanged for each.
        """
        if self.debug:
            if _prefers_html(request):
                body = render_traceback_html(exc).encode()
                content_type = MIME_TEXT_HTML_UTF8
            else:
                body = "".join(
                    traceback.format_exception(type(exc), exc, exc.__traceback__)
                ).encode()
                content_type = MIME_TEXT_PLAIN_UTF8
            response = Response(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                body=body,
                content_type=content_type,
            )
            response = await self._run_response_phase(cp.http_post, request, response, excluded)
            return response
        return await self._handle_error(
            request,
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            JSONResponse(
                {"detail": MSG_INTERNAL_SERVER_ERROR},
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            ),
            exc,
        )

    # ── Hooks and route resolution ─────────────────────────

    async def _run_before_hooks(self, request: Request) -> tuple[Response | None, str | None]:
        """Run before_request hooks; return `(short_circuit_response, bp_name)`.

        App-level hooks fire first, then the matched blueprint's (the
        blueprint bucket is selected from `request.endpoint` *after* the
        app-level hooks run, so a hook that rewrites the endpoint is
        honoured). A hook returning a non-None value short-circuits: it is
        coerced and passed through response middleware (unconditionally,
        matching the original early-return path) and returned.

        `bp_name` is the matched blueprint - `None` while the app-level hooks
        are still running, then the endpoint's blueprint once they complete.
        The orchestrator records it as the teardown blueprint, so a
        short-circuit inside an app-level hook leaves it `None` (no blueprint
        teardown) exactly as the inline version did.
        """
        for hook in self._before_request_hooks:
            result = await self._call_handler(hook, {"request": request})
            if result is not None:
                response = await self._run_response_middleware(
                    request, self._coerce_response(result)
                )
                return response, None
        bp_name = _endpoint_blueprint(request.endpoint)
        if self._bp_before_hooks and bp_name is not None:
            for hook in self._bp_before_hooks.get(bp_name, ()):
                result = await self._call_handler(hook, {"request": request})
                if result is not None:
                    response = await self._run_response_middleware(
                        request, self._coerce_response(result)
                    )
                    return response, bp_name
        return None, bp_name

    async def _resolve_route(
        self,
        request: Request,
        match: Any,
        matched_path: str,
        matched_method: str,
        cp: CompiledPipeline,
        excluded: bool,
    ) -> Any:
        """Resolve the route to dispatch, or a terminal Response.

        Checks mounted sub-apps and static handlers first, re-matches when a
        before_request hook rewrote the path or method, enforces
        subdomain/host constraints, applies slash redirects, and produces the
        405/404 responses. Returns either a Response (already through response
        middleware) or the match to dispatch, having populated `path_params`,
        defaults, endpoint, and url_rule and run URL value preprocessors.
        Raises `HTTPException` for the 404 / constraint-mismatch cases.
        """
        # Check mounted sub-apps.
        #
        # A linear prefix scan, kept deliberately. The `has_mounted_apps` gate
        # makes an app with no mounts pay nothing, and beyond that the cost is
        # ~0.07 us per mount per request (measured on the project's benchmark
        # host, min-of-7 over 5k requests each: 0 mounts 5.54 us, 1 mount
        # 6.74 us, 3 mounts 6.91 us, 10 mounts 7.33 us, 50 mounts 10.14 us).
        # A prefix trie would lose to a list scan at the two or three mounts a
        # typical app registers, and an overlapping mount is now a
        # registration-time `ValueError` rather than a shadowing to disambiguate
        # here. Revisit if an app is ever seen with mounts in the dozens.
        if cp.has_mounted_apps:
            for prefix, prefix_slash, sub_app in self._mounted_apps:
                if request.path.startswith(prefix_slash) or request.path == prefix:
                    sub_path = request.path[len(prefix) :] or "/"
                    # Only the sub-app knows whether its route streams. Hand the
                    # body source straight over when it does, so a `stream=True`
                    # route keeps streaming once mounted; otherwise drain here,
                    # which is what the transport would have done at top level.
                    # `mount()` routes a non-`Veloce` app to `_asgi_mounts` or
                    # `_static_handlers`, so every entry here is a `Veloce` and
                    # has both methods. Probing for them would turn a missing one
                    # into a silent fall-through to the next mount, with this
                    # request's body already drained into `sub_request`.
                    sub_match = sub_app.match(request.method, sub_path)
                    streams = sub_match is not None and sub_match.route_info.stream
                    # `derive_for_mount` owns what the mount changes and what it
                    # carries forward; it stacks under the parent's own root_path
                    # when the parent is itself mounted.
                    sub_request = Request.derive_for_mount(
                        request,
                        sub_path,
                        b"" if streams else await request.body(),
                        sub_app,
                        prefix,
                        body_source=request._body_source if streams else None,
                    )
                    response = await sub_app.handle_request(sub_request)
                    return await self._run_response_middleware(request, response)

        # Check static files
        if cp.has_static_handlers:
            for static in self._static_handlers:
                response = await static.handle(request)
                if response is not None:
                    return await self._run_response_middleware(request, response)

        # Route matching - reuse the match taken before the before_request
        # hooks ran unless a hook rewrote the request path or method, in
        # which case the routing inputs changed and we must re-match.
        if request.path != matched_path or request.method != matched_method:
            match = self.match(request.method, request.path)

        # Subdomain constraint check - if the matched route declares a
        # `subdomain`, the request's host must be `{subdomain}.{SERVER_NAME}`.
        # Mismatch raises 404 directly (not 405, because
        # the path is reachable, just not from this host).
        # `subdomain="*"` accepts any non-empty subdomain.
        if (
            match is not None
            and match.route_info.subdomain is not None
            and not self._subdomain_matches(request, match.route_info.subdomain)
        ):
            raise HTTPException(status.HTTP_404_NOT_FOUND, MSG_NOT_FOUND)

        # Host constraint check - the full `Host` header must equal
        # the route's declared `host` (case-insensitive, port-stripped).
        # Mismatch -> 404 (the path is reachable, just not from this host).
        if match is not None and match.route_info.host is not None:
            req_host = _extract_host(request.headers.get(HEADER_HOST, "") or "")
            if req_host != match.route_info.host:
                raise HTTPException(status.HTTP_404_NOT_FOUND, MSG_NOT_FOUND)

        # Redirect slashes (like common web frameworks): /users -> /users/ or vice versa
        if match is None and self.redirect_slashes:
            alt = (
                request.path.rstrip("/")
                if request.path.endswith("/") and request.path != "/"
                else request.path + "/"
            )
            alt_match = self.match(request.method, alt)
            if alt_match is not None:
                code = (
                    status.HTTP_308_PERMANENT_REDIRECT
                    if request.method != HTTP_METHOD_GET
                    else status.HTTP_307_TEMPORARY_REDIRECT
                )
                # `alt` is matched against this (sub-)app's router, so it is the
                # mount-local path. The `Location` the client follows must carry
                # the mount prefix (the request's `root_path`); otherwise a slash
                # redirect inside a mounted sub-app points at a path that does not
                # exist on the parent. `root_path` is "" for a top-level app, so
                # the unmounted case is unchanged.
                response = RedirectResponse(request.root_path + alt, status_code=code)
                response = await self._run_response_phase(cp.http_post, request, response, excluded)
                return response

        if match is None:
            # Check if path exists but method is wrong
            allowed = self.get_allowed_methods(request.path)
            if allowed:
                # RFC 9110 Sec. 9.3.7: OPTIONS auto-responds with `Allow:` and
                # an empty body even when no handler is registered.
                if request.method == HTTP_METHOD_OPTIONS:
                    response = self.make_default_options_response(
                        request.path, allowed_methods=allowed
                    )
                    response = await self._run_response_phase(
                        cp.http_post, request, response, excluded
                    )
                    return response
                return await self._handle_error(
                    request,
                    status.HTTP_405_METHOD_NOT_ALLOWED,
                    JSONResponse(
                        {"detail": MSG_METHOD_NOT_ALLOWED, "allowed": allowed},
                        status_code=status.HTTP_405_METHOD_NOT_ALLOWED,
                        headers={HEADER_ALLOW: ", ".join(allowed)},
                    ),
                )
            raise HTTPException(status.HTTP_404_NOT_FOUND, MSG_NOT_FOUND)

        # Set path params + endpoint name on request.
        request.path_params = match.path_params
        # the routing-rule `defaults` - fill in fixed values for params
        # not already supplied by the matched URL.
        if match.route_info.defaults:
            for _dk, _dv in match.route_info.defaults.items():
                request.path_params.setdefault(_dk, _dv)
        request.endpoint = match.route_info.name
        request._state["url_rule"] = match.route_info.path_template

        # URL value preprocessors: mutate path_params in place before the
        # handler sees them. Endpoint is the route name. Kept inline (rather
        # than calling `_run_url_value_preprocessors`) so a route with no
        # preprocessors pays zero extra call frames on the match hot path; the
        # MCP tool path runs the identical chain via that helper.
        if self._url_value_preprocessors:
            endpoint = match.route_info.name
            for proc in self._url_value_preprocessors:
                proc(endpoint, request.path_params)
        if self._bp_url_value_preprocessors:
            endpoint = match.route_info.name
            bp = _endpoint_blueprint(endpoint)
            bp_procs = self._bp_url_value_preprocessors.get(bp) if bp is not None else None
            if bp_procs is not None:
                for proc in bp_procs:
                    proc(endpoint, request.path_params)

        return match

    def _run_url_value_preprocessors(
        self, endpoint: str | None, path_params: dict[str, Any]
    ) -> None:
        """Run every registered `url_value_preprocessor` against `path_params`.

        Each processor receives `(endpoint, path_params)` and may mutate
        `path_params` in place (e.g. pop a locale segment into `g`). App-level
        App-level processors run first, then the ones the endpoint's blueprint
        contributes - the same order the request hooks use. Shared by HTTP
        dispatch and the MCP route-backed tool path so a processor sees the
        same call on both.
        """
        if self._url_value_preprocessors:
            for proc in self._url_value_preprocessors:
                proc(endpoint, path_params)
        if self._bp_url_value_preprocessors:
            bp = _endpoint_blueprint(endpoint)
            bp_procs = self._bp_url_value_preprocessors.get(bp) if bp is not None else None
            if bp_procs is not None:
                for proc in bp_procs:
                    proc(endpoint, path_params)

    # ── Dependencies and response building ─────────────────

    async def _resolve_dependencies(
        self, request: Request, match: Any
    ) -> tuple[dict, DependencyResolver | None]:
        """Build the handler kwargs and the resolver that backs them.

        Returns `(kwargs, resolver)`. The resolver is `None` for trivial /
        request-only plans (no dependencies to resolve). The caller must
        hold the returned resolver so the dispatch `finally` can run its
        yield-dependency teardowns even when the handler raises.
        """
        # Fast path: consume the pre-built handler plan that Router.add_route
        # cached on RouteInfo at registration time.
        route_info = match.route_info
        if route_info.handler_plan is not None:
            if route_info.is_trivial_plan:
                return {}, None
            if route_info.is_request_only_plan:
                return {route_info.request_param_name: request}, None
            resolver = DependencyResolver()
            resolver._overrides = self._dependency_overrides
            resolver._override_subplans = self._override_subplans
            kwargs = await resolver.resolve_plan(
                route_info.handler_plan,
                request,
                match.path_params,
                route_info.route_dep_plans,
            )
            return kwargs, resolver
        resolver = DependencyResolver()
        resolver._overrides = self._dependency_overrides
        resolver._override_subplans = self._override_subplans
        kwargs = await resolver.resolve(
            route_info.handler,
            request,
            match.path_params,
            route_dependencies=[d for d in route_info.dependencies if isinstance(d, Depends)],
        )
        return kwargs, resolver

    def _build_response(self, request: Request, match: Any, result: Any) -> Response:
        """Turn a handler return value into the final Response.

        Applies the route `response_model`, coerces to a Response, applies the
        route-level status_code override, and merges any handler-injected
        Response's status / headers.
        """
        route_info = match.route_info
        # Apply response_model validation + dump flags before coercion.
        # The handler may return a dict/BaseModel/list; if the route
        # declared a response_model, route the value through it so
        # extra fields drop, aliases apply, and unset/None filters fire.
        # Every backend shapes. `_apply_response_model` dispatches a msgspec
        # struct (or `list[Struct]`) to the backend-agnostic shaper like any
        # other value: a backend that reached `_coerce_response` unshaped would
        # filter nothing, and a subclass would put its extra fields on the wire.
        if route_info.response_model is not None and not isinstance(result, Response):
            result = self._apply_response_model(result, route_info)

        response = self._coerce_response(result, route_info.response_class)

        # Apply route-level status_code override
        if (
            route_info.status_code != status.HTTP_200_OK
            and response.status_code == status.HTTP_200_OK
        ):
            response.status_code = route_info.status_code
            response._encoded = None

        # Response injection - merge a handler-injected
        # Response's status_code + headers onto the final response.
        # Skipped when the handler returned a Response itself (its own
        # status/headers already win). `status_code == 0` means the
        # handler never touched it, so it is not applied.
        injected = request._state.get(STATE_INJECTED_RESPONSE) if request._state else None
        if injected is not None and not isinstance(result, Response):
            if injected.status_code:
                response.status_code = injected.status_code
            for hk, hv in injected.headers.items():
                if hk.lower() == "set-cookie":
                    response._append_set_cookie_header(hv)
                else:
                    response.headers[hk] = hv
            response._encoded = None

        return response

    async def _run_after_hooks(
        self, request: Request, response: Response, bp_name: str | None
    ) -> Response:
        """Run after_request hooks and one-shot `after_this_request` callbacks.

        App-level hooks fire in reverse registration order, then the matched
        blueprint's, then the per-request one-shot callbacks. Each may return
        a replacement Response.
        """
        # Run after_request hooks - app-level then matched blueprint.
        for hook in reversed(self._after_request_hooks):
            hook_result = await self._call_after_hook(hook, request, response)
            if hook_result is not None and isinstance(hook_result, Response):
                response = hook_result
        if self._bp_after_hooks and bp_name is not None:
            for hook in reversed(self._bp_after_hooks.get(bp_name, ())):
                hook_result = await self._call_after_hook(hook, request, response)
                if hook_result is not None and isinstance(hook_result, Response):
                    response = hook_result

        # Drain one-shot `after_this_request(fn)` callbacks. These run
        # *after* the global hooks (so per-request adjustments see the
        # global hooks' mutations) and only for the current request.
        one_shot = request._state.get("_after_this_request") if request._state else None
        if one_shot:
            for fn in one_shot:
                fn_result = await self._call_after_hook(fn, request, response)
                if fn_result is not None and isinstance(fn_result, Response):
                    response = fn_result
        return response

    def _schedule_background_tasks(self, request: Request, response: Response) -> bool:
        """Schedule the DI-injected queue and response-attached background task.

        Both run through `spawn()`, the single tracked-task path: each is held
        by a strong reference (so the loop cannot GC it mid-flight) and is
        cancelled-and-drained on shutdown alongside app-spawned tasks rather
        than orphaned, and its failures surface through the same logging path.

        Returns whether anything was scheduled. A background task outlives the
        response and may still be reading an upload, so when one exists the
        request's spool files are released after it finishes rather than at
        teardown; the caller uses this to decide which.
        """
        # Response-attached background task (shape:
        # `Response(content=..., background=BackgroundTask(fn))`). Read through
        # `getattr` rather than `response.background`: the attribute is a
        # `Response` slot every construction path in this tree initialises, but a
        # user subclass whose `__init__` skips `super()` would turn a direct read
        # into an `AttributeError` on the response path.
        injected = request._background_tasks
        attached_bg = getattr(response, "background", None)
        # Both sources checked before anything is allocated: almost every
        # response has neither, and used to build a list only to discard it.
        if injected is None and attached_bg is None:
            return False

        coros = []
        if injected is not None:
            coros.append(injected.run_all())

        if attached_bg is not None:
            # `BackgroundTasks` collection -> `.run_all()`;
            # single `BackgroundTask` -> `.run()`. Anything else with
            # a `run()` coroutine method is supported too.
            if hasattr(attached_bg, "run_all"):
                coro = attached_bg.run_all()
            elif hasattr(attached_bg, "run"):
                coro = attached_bg.run()
            else:
                coro = None
            if coro is not None:
                coros.append(coro)

        if not coros:
            return False
        tasks = [self.spawn(coro) for coro in coros]

        # Release the request's upload spool files once the last of them
        # finishes. A done-callback rather than another spawned task: it adds
        # no tracked task to drain at shutdown, and leaves the tasks running
        # concurrently as they did before. A failed task still counts as done,
        # so a failure cannot strand the files.
        form = request._form
        if form is not None:
            remaining = len(tasks)

            # Closes over the *form*, not the request. Capturing `request` kept
            # the whole object alive - headers, body bytes, state, scope and the
            # ASGI callables - for as long as the slowest background task ran,
            # which can be far longer than the response it belonged to. The
            # spool files are all this callback needs.
            def _release(_task: asyncio.Task[Any]) -> None:
                nonlocal remaining
                remaining -= 1
                if remaining == 0:
                    _close_form_uploads(form)

            for task in tasks:
                task.add_done_callback(_release)
        return True

    # ── Error handling and handler invocation ──────────────

    async def _handle_error(
        self,
        request: Request,
        status_code: int,
        default: Response,
        exc: BaseException | None = None,
    ) -> Response:
        """Check for status-code handler, fall back to default response."""
        # Prefer a handler on the failing request's blueprint chain (a
        # `@bp.errorhandler(500)` scoped to its own routes) before the app-level
        # table, so a blueprint status handler still fires on the unhandled
        # exception -> 500 path, not only on the HTTPException path.
        handler = self._find_scoped_status_handler(status_code, request)
        if handler:
            # Adapt to the handler's signature rather than assuming it takes
            # only `request`. The same handler registered for a status code is
            # reachable from here and from `handle_http_exception`, which passes
            # the exception - so a `(request, exc)` handler must be callable on
            # both paths, or the `TypeError` escapes dispatch itself.
            if exc is None:
                exc = HTTPException(status_code=status_code)
            result = await self._call_exc_handler(handler, request, exc)
            return await self._run_response_middleware(request, self._coerce_response(result))
        return await self._run_response_middleware(request, default)

    def _subdomain_matches(self, request: Request, subdomain: str) -> bool:
        """Check whether `request`'s host carries the expected subdomain.

        `subdomain` is the literal subdomain string (`"api"`, `"admin"`); `"*"`
        matches any non-empty subdomain. What the request's subdomain *is* comes
        from `Request.subdomain`, which is the same question a handler asks.

        Deriving it here separately would let the two disagree, and the way
        they disagree is not benign: `Request.subdomain` short-circuits an IP
        literal, because its dots are address structure and not name labels. A
        second derivation without that rule matches a route declared
        `subdomain="192"` against a request to `192.168.1.1` - and the handler
        it matched then asks the framework the same question and is told the
        subdomain is empty.
        """
        actual = request.subdomain
        if subdomain == "*":
            return bool(actual)
        return actual == subdomain

    async def _call_handler(
        self, handler: Callable, kwargs: dict, is_coro: bool | None = None
    ) -> Any:
        """Call a handler, supporting both sync and async.

        Sync handlers are offloaded to the default thread pool executor
        to prevent blocking the event loop. When the caller already knows
        whether the handler is a coroutine - the handler plan precomputes
        it at registration - it passes `is_coro` to skip the per-request
        `inspect.iscoroutinefunction` probe.
        """
        if is_coro is None:
            is_coro = _is_async_callable(handler)
        if is_coro:
            return await handler(**kwargs)
        # Run sync handlers in the thread pool so they cannot block the event
        # loop; `offload` preserves request-scoped ContextVars.
        return await offload(handler, **kwargs)

    async def _call_after_hook(self, hook: Callable, request: Request, response: Response) -> Any:
        """Call an after-request hook, adapting kwargs to match its signature."""
        kwargs = _adapt_hook_kwargs(hook, _after_hook_sig_cache, "response", request, response)
        return await self._call_handler(hook, kwargs)

    async def _call_exc_handler(
        self, handler: Callable, request: Request, exc: BaseException
    ) -> Any:
        """Call an exception handler, adapting kwargs to match its signature."""
        kwargs = _adapt_hook_kwargs(handler, _exc_handler_sig_cache, "exc", request, exc)
        return await self._call_handler(handler, kwargs)

    async def _dispatch_exc_handler(
        self, handler: Callable, request: Request, exc: BaseException
    ) -> Response:
        """Invoke a user exception handler with a guard around its own raises.

        A user error handler that itself raises must not escape dispatch
        uncaught - that would surface as a bare 500 with no targeted log and
        lose the original exception's context. This logs the secondary
        failure (naming the handler and the request path) and returns
        Veloce's standard 500, so a buggy handler degrades gracefully in
        production. When `PROPAGATE_EXCEPTIONS` is in effect (tests/dev), the
        secondary exception is re-raised so the handler bug is visible.
        """
        try:
            result = await self._call_exc_handler(handler, request, exc)
        except Exception as handler_exc:
            if self._should_propagate_exceptions():
                raise
            handler_exc.__context__ = exc
            self.logger.error(
                "Exception handler %s raised while handling %s %s",
                getattr(handler, "__qualname__", repr(handler)),
                request.method,
                request.path,
                exc_info=handler_exc,
            )
            return self._coerce_response(
                JSONResponse(
                    {
                        "detail": MSG_INTERNAL_SERVER_ERROR,
                        "status_code": status.HTTP_500_INTERNAL_SERVER_ERROR,
                    },
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                )
            )
        return self._coerce_response(result)

    # ── Response shaping ───────────────────────────────────

    def _apply_response_model(self, result: Any, route_info: Any) -> Any:
        """Route the handler return through `response_model` + dump flags.

        - Coerces dict / BaseModel / list-of-either through `model_validate`
          so undeclared fields drop.
        - Honours `response_model_exclude_unset`, `_exclude_defaults`,
          `_by_alias`, `_exclude_none`, `_include`, `_exclude` on `model_dump`.
        - For `list[Model]` declarations (e.g. response_model=list[User]),
          validates each element individually.
        """
        model = route_info.response_model
        # Resolved once when the route was registered - see `RouteInfo.__init__`.
        # Read, never mutated: every route shares its own mapping across requests.
        dump_kwargs = route_info.response_dump_kwargs

        origin = route_info.response_model_origin
        # Sequence-style response models - `response_model=list[Item]` - dump
        # each element through the inner model.
        if origin is list:
            args = get_args(model)
            if args:
                inner = args[0]
                if not isinstance(result, (list, tuple)):
                    return result  # let downstream coercion handle the mismatch
                if is_pydantic_model(inner):
                    dumped: list[Any] = []
                    for item in result:
                        # Fast path: an element already exactly the target model
                        # is dumped directly - skipping a re-validation
                        # round-trip and preserving the fields-set markers
                        # that `exclude_unset` reads (matching the scalar
                        # branch below). A subclass is re-shaped instead, so it
                        # cannot leak fields the declared element type excludes.
                        if type(item) is inner:
                            dumped.append(item.model_dump(**dump_kwargs))
                        else:
                            # Dump a model element to a dict before validating:
                            # Pydantic does not revalidate an instance that
                            # already satisfies the target type, so a subclass
                            # would pass through carrying its own extra fields.
                            payload = (
                                item.model_dump() if isinstance(item, _PydanticBaseModel) else item
                            )
                            dumped.append(inner.model_validate(payload).model_dump(**dump_kwargs))
                    return dumped
                if backend_of(inner) is not ModelBackend.NONE:
                    # msgspec / dataclass / TypedDict elements shape through the
                    # backend-agnostic shaper. `dump_kwargs` is Pydantic's
                    # vocabulary and does not apply. An element that is exactly
                    # the declared type skips the round-trip, as the scalar
                    # branch below does.
                    return [
                        item if type(item) is inner else shape_through_model(item, inner)
                        for item in result
                    ]
            return result

        # Scalar Pydantic model.
        if is_pydantic_model(model):
            # Exactly the target model dumps directly - the dump-then-validate
            # roundtrip would erase the `__pydantic_fields_set__` info that
            # drives `exclude_unset`. A SUBCLASS must not take this path: it
            # would dump the subclass's own fields, so a richer object returned
            # under a base-model contract would leak the fields the contract
            # excludes. It goes through validation below and is re-shaped.
            if type(result) is model:
                return result.model_dump(**dump_kwargs)
            # Cross-model or dict input: dump any incoming BaseModel to a
            # dict first so model_validate can re-shape it. Cross-model
            # coercion (e.g. internal -> public view) works as expected;
            # `exclude_unset` semantics necessarily reset because the
            # fields-set markers don't transfer across model types.
            payload = result.model_dump() if isinstance(result, _PydanticBaseModel) else result
            validated = model.model_validate(payload)
            return validated.model_dump(**dump_kwargs)

        # msgspec struct / dataclass / TypedDict: shape through the shared
        # backend-agnostic shaper so a declared output contract filters the same
        # way whichever kind of type declared it. Skipping this let a subclass
        # returned under a base-model contract put its extra fields on the wire.
        # Exactly the declared type carries no field the contract excludes, so it
        # needs no reshaping and is handed to the encoder as before. Tested
        # before classifying the backend: `backend_of` walks an isinstance
        # ladder, and this is the common case on every response.
        if type(result) is model:
            return result
        if route_info.response_model_backend is not ModelBackend.NONE:
            # A subclass, or a mapping - the case that leaked. The shaper is a
            # full builtins round-trip, which is why the exact-type check above
            # keeps it off the common path.
            return shape_through_model(result, model)

        # Not a model at all (e.g. a plain class) - pass through unchanged.
        return result

    @staticmethod
    def _response_class_mismatch(response_class: Any, result: Any) -> str:
        """Build the message for a return the declared response class cannot render.

        A text response class encodes what it is given, so a `dict` reached
        `.encode()` and produced `AttributeError: 'dict' object has no attribute
        'encode'` - a 500 naming neither the class that was asked for nor the
        route that returned the value.
        """
        name = getattr(response_class, "__name__", response_class)
        return (
            f"{name} cannot render a {type(result).__name__}; it encodes str or bytes. "
            f"Return a str, or declare response_class=JSONResponse on this route "
            f"to send the value as JSON."
        )

    def _json_from_handler(self, data: Any) -> Response:
        """Build the JSON response for a handler's `dict` / `list` / model return.

        Takes the direct path unless the application configured a provider or a
        JSON option, in which case that dialect applies here the way it already
        applies to `jsonify` - and, since the dialect was extended to every
        response, the way it applies to an error payload and a validation report
        too. Only genuine protocol frames stay outside it: a signed cookie, a
        JWT, an MCP JSON-RPC envelope.
        """
        dumps = self._json_dumps_override()
        # `from_bytes` on both branches, not a bare `Response`: the return type
        # is part of the contract - a `default_response_class` check and an
        # `isinstance` on the coerced response both expect a `JSONResponse`.
        # And encoding here rather than through `JSONResponse(data)` avoids
        # resolving the dialect twice: this line has just done it. The
        # constructor resolves it for a handler that builds one itself, which
        # is the path that has no other way to learn the application's dialect.
        body = orjson.dumps(data, default=orjson_default) if dumps is None else dumps(data)
        return JSONResponse._from_encoded(body)

    def _json_dumps_override(self) -> Any:
        """Return the configured serialiser, or `None` to take the direct path.

        Resolved once and cached. `None` is the stock case - the default
        provider with no options set - where the direct path already emits
        exactly what the provider would, so nothing is paid for the indirection.
        """
        dumps = self._handler_json_dumps
        if dumps is _UNRESOLVED_JSON_DUMPS:
            dumps = self._handler_json_dumps = self._resolve_handler_json_dumps()
        return dumps

    def _coerce_response(self, result: Any, response_class: Any = None) -> Response:
        """Convert handler return value to a Response object."""
        if isinstance(result, Response):
            return result
        # Exact `dict` with no custom response_class is the overwhelmingly common
        # handler return - serve it straight from `JSONResponse` before the
        # msgspec probe, so a plain dict never pays the `_is_msgspec_payload`
        # scan (it is not a struct or struct list and would fall through anyway).
        # Gated on `response_class is None` so the JSONResponse-subclass branch
        # below still owns dicts when a class was requested.
        if type(result) is dict and response_class is None:
            return self._json_from_handler(result)
        # A msgspec struct (or a list of structs) encodes in C with no
        # intermediate dict. With no response_class it is written straight to a
        # JSON Response; with one, it is normalized to builtins so the requested
        # class renders it the usual way. A `(struct, status)` tuple is excluded
        # by `_is_msgspec_payload` and flows to the tuple handler below, which
        # recurses on the struct body.
        if _HAS_MSGSPEC and _is_msgspec_payload(result):
            if response_class is None:
                dumps = self._json_dumps_override()
                if dumps is None:
                    return Response(body=_msgspec.json.encode(result), content_type=MIME_JSON)
                # A configured dialect outranks the struct fast path: convert to
                # builtins so the same serialiser answers for every return type.
                return JSONResponse.from_bytes(dumps(_msgspec.to_builtins(result)))
            result = _msgspec.to_builtins(result)
        # A `(body, status[, headers])` return, unpacked once for both the
        # `response_class` and no-class paths. Two copies had drifted: a
        # one-element tuple meant the body with a class and a one-item JSON array
        # without, and a four-element tuple lost its status and headers in
        # silence. Any other length is not a response tuple and falls through as
        # a plain value.
        if isinstance(result, tuple):
            length = len(result)
            if length == 2 or length == 3:
                if length == 3:
                    body, code, headers = result
                else:
                    body, second = result
                    if isinstance(second, dict):
                        code, headers = status.HTTP_200_OK, second
                    else:
                        code = second if isinstance(second, int) else int(second)
                        headers = None
                resp = self._coerce_response(body, response_class)
                resp.status_code = code
                if headers:
                    resp.headers.update(headers)
                # The body was coerced and may already carry a cached encoding;
                # the status line and headers just changed, so it is stale.
                resp._encoded = None
                return resp
        # Use custom response_class if specified
        if response_class is not None:
            if isinstance(response_class, type) and issubclass(response_class, JSONResponse):
                if isinstance(result, _PydanticBaseModel):
                    result = result.model_dump()
                dumps = self._json_dumps_override()
                if dumps is None:
                    return response_class(result)
                # `from_bytes` on the requested class, so a subclass keeps its
                # own `default_media_type` while the dialect still applies.
                return response_class.from_bytes(dumps(result))
            if isinstance(result, (str, bytes)):
                return response_class(result)
            raise TypeError(self._response_class_mismatch(response_class, result))
        if isinstance(result, (dict, list)):
            return self._json_from_handler(result)
        if isinstance(result, str):
            # A bare `str` return defaults to text/html - the same default
            # `make_response()` applies, so the media type is consistent
            # whichever path produced the response.
            return Response(body=result.encode(), content_type=MIME_HTML)
        if isinstance(result, bytes):
            return Response(body=result, content_type=MIME_HTML)
        # Pydantic model
        if isinstance(result, _PydanticBaseModel):
            return self._json_from_handler(result.model_dump())
        return self._json_from_handler(result)

    # ── Middleware phases ──────────────────────────────────

    async def _run_request_middleware(
        self, request: Request, chain: list[Middleware] | None = None
    ) -> Response | None:
        """Run the middleware request phase in registration order.

        Each `Middleware.process_request` runs in turn; the first to return a
        `Response` short-circuits the chain (the caller is responsible for
        running that response back through the response phase). Returns `None`
        when no middleware short-circuits. Extracted so the MCP dispatch path
        can replay the identical request-phase chain a route-backed tool call
        would see on the HTTP path.

        `chain` defaults to the app's full middleware list. A route declaring
        `exclude_middleware` must skip the excluded middleware over MCP exactly
        as on the HTTP path, so the MCP caller passes the route's pre-filtered
        request-phase chain (from `_route_middleware_chains`) instead.
        """
        for mw in self._middlewares if chain is None else chain:
            early_response = await mw.process_request(request)
            if early_response is not None:
                return early_response
        return None

    def _route_middleware_chains(
        self, route_info: Any
    ) -> tuple[list[Middleware], list[Middleware]] | None:
        """Resolve the filtered (request-order, response-order) chains for a route.

        Returns `None` when the route excludes nothing - the common case -
        signalling callers to use the app's middleware list directly with no
        copy or filter, so the dispatch hot path pays nothing extra. When a
        route declares `exclude_middleware`, the filtered chains are computed
        once per (route, middleware-generation) and memoised on the
        RouteInfo, keyed on `self._mw_version`, so later requests reuse the
        cached lists rather than re-filtering.
        """
        excluded = route_info.excluded_middleware
        if excluded is None:
            return None
        cache = route_info._mw_chain_cache
        version = self._mw_version
        if cache is not None and cache[0] == version:
            return cache[1], cache[2]
        names, types = excluded
        request_chain = [
            mw
            for mw in self._middlewares
            if mw.middleware_name not in names and not isinstance(mw, types)
        ]
        response_chain = request_chain[::-1]
        route_info._mw_chain_cache = (version, request_chain, response_chain)
        return request_chain, response_chain

    async def _run_response_phase(
        self,
        fused: tuple[Callable, ...] | None,
        request: Request,
        response: Response,
        excluded: bool,
    ) -> Response:
        """Apply the response phase, preferring the compile-time fused chain.

        `excluded` is the caller's record of whether this request stashed a
        per-route filtered response chain under `_MW_RESPONSE_CHAIN_KEY`; the
        caller already knows it as a local, so the decision is passed in rather
        than re-probed from `request._state` on every response. When set, the
        dynamic reversed-filtered walk runs so the route's opt-out is honoured.
        The common case iterates `fused` - the `process_response` bound methods
        reversed once at compile - with no per-response `reversed` alloc. `fused`
        is `None` only when no middleware is registered, so nothing runs.
        """
        if excluded:
            return await self._run_response_middleware(request, response)
        if fused is not None:
            for process_response in fused:
                response = await process_response(request, response)
        return response

    async def _run_response_middleware(self, request: Request, response: Response) -> Response:
        """Run middleware response phase in reverse order.

        Honours a per-route filtered chain stashed on `request._state` by the
        request phase, so a route's `exclude_middleware` opt-out applies
        symmetrically to `process_response`. Absent that key (no exclusions),
        the app's middleware list is walked in reverse as before.
        """
        chain = request._state.get(_MW_RESPONSE_CHAIN_KEY)
        if chain is None:
            chain = reversed(self._middlewares)
        for mw in chain:
            response = await mw.process_response(request, response)
        return response

    # ── Instrumentation ────────────────────────────────────

    async def _run_instrumentation(
        self,
        request: Request,
        status_code: int,
        duration_ms: float,
        streamed: bool = False,
        end_time_ns: int | None = None,
    ) -> None:
        """Deliver a `RequestMetrics` record to every instrumentation hook.

        A hook may be sync or async; one that raises is logged and skipped
        so observability code can never break the response.

        `streamed` marks responses whose body is emitted later on the ASGI
        send path; for those `duration_ms`/`status_code` cover only response
        production, not stream completion. See `RequestMetrics.streamed`.
        `end_time_ns` is the wall-clock end captured before any hook runs.
        """
        # Surface the originating exception's class name (set on request state
        # by the dispatch error paths) only for a server error, so a handler
        # that deliberately returns a 5xx without raising is not mislabelled.
        # The class name only is carried - never the message or the instance.
        error_type = (
            request._state.get("_error_type")
            if status_code >= status.HTTP_500_INTERNAL_SERVER_ERROR
            else None
        )
        metrics = RequestMetrics(
            method=request.method,
            path=request.path,
            route=request.url_rule,
            status_code=status_code,
            duration_ms=duration_ms,
            streamed=streamed,
            end_time_ns=end_time_ns,
            error_type=error_type,
            # Inbound distributed-trace headers, carried verbatim so a tracing
            # bridge (e.g. veloce.otel) can extract a parent context and
            # continue the trace. Built on every dispatch path here - never via
            # a before_request hook, which a short-circuiting hook could skip.
            # `None` when the request carries no trace headers.
            parent_context=_trace_carrier(request),
        )
        # Per-hook route-template exclusions are sparse: when none are
        # configured the membership test is skipped entirely so the common path
        # is unchanged. A hook with an exclusion set is suppressed for a request
        # whose matched route template is in that set (health/metrics/etc).
        excludes = self._instrumentation_excludes
        route = metrics.route
        for hook in self._instrumentation:
            if excludes and route is not None:
                excluded = excludes.get(hook)
                if excluded is not None and route in excluded:
                    continue
            try:
                result = hook(metrics)
                if inspect.isawaitable(result):
                    await result
            except Exception:
                self.logger.exception("instrumentation hook raised an exception")

    # ── Server ────────────────────────────────────────────
