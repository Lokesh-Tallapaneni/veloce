"""HTTP request dispatch — the per-request pipeline mixed into Veloce.

`handle_request` -> `_dispatch_request` runs the middleware phases, the
before/after hooks, route and dependency resolution, the handler call, response
shaping and teardown.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import inspect
import time
import traceback
import weakref
from collections.abc import Callable, MutableMapping
from typing import Any, get_args, get_origin

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
    _serving_error_page_var,
    _unpack_response_tuple,
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
from veloce.app._host import AppHost
from veloce.blueprints import _endpoint_blueprint
from veloce.debug import render_traceback_html
from veloce.dependency import DependencyResolver, Depends
from veloce.encoders import orjson_default
from veloce.exceptions import (
    HTTPException,
    MethodNotAllowed,
    NotFound,
    http_exception_payload,
)
from veloce.helpers import g, jsonify
from veloce.http._body import body_too_large, too_large_payload
from veloce.http.request import Request
from veloce.http.response import (
    JSONResponse,
    RedirectResponse,
    Response,
    header_present,
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


# Cache of the `(request_key, exc_key)` binding per exception handler.
_HookBinding = tuple[str | None, str | None]

_exc_handler_sig_cache: weakref.WeakKeyDictionary[Callable[..., Any], _HookBinding] = (
    weakref.WeakKeyDictionary()
)

# Cache of the `(request_key, response_key)` binding per after-request hook.
_after_hook_sig_cache: weakref.WeakKeyDictionary[Callable[..., Any], _HookBinding] = (
    weakref.WeakKeyDictionary()
)

# `request._state` key for a route's filtered response-phase chain.
_MW_RESPONSE_CHAIN_KEY = "_mw_response_chain"


# ── Module helpers ─────────────────────────────────────────


def _prefers_html(request: Request) -> bool:
    """Whether the client prefers an HTML response over plain text."""
    accept = request.headers.get(HEADER_ACCEPT)
    if not accept:
        return False
    return request.accept_mimetypes.best_match([MIME_TEXT_PLAIN, MIME_TEXT_HTML]) == MIME_TEXT_HTML


def _trace_carrier(request: Request) -> dict[str, str] | None:
    """Inbound W3C trace headers as a carrier dict, or `None` if absent.

    Only `traceparent` / `tracestate` are copied, keeping the core free of any
    OpenTelemetry dependency.
    """
    traceparent = request.headers.get(TRACE_HEADER_TRACEPARENT)
    if traceparent is None:
        return None
    return build_trace_carrier(traceparent, request.headers.get(TRACE_HEADER_TRACESTATE))


def _is_msgspec_payload(result: Any) -> bool:
    """Whether a handler return value should be encoded by msgspec.

    A `tuple` is deliberately excluded - it is the `(body, status[, headers])`
    response idiom, not a JSON array.
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


# The type an annotation must name to claim the second value. `BaseException`
# rather than the raised class, so any base of it matches alike.
_SECOND_VALUE_TYPES: dict[str, type[Any]] = {"response": Response, "exc": BaseException}


def _resolve_hook_binding(fn: Callable[..., Any], second_name: str) -> _HookBinding:
    """Decide which parameters of `fn` receive the request and the second value.

    Three rules per slot, in order: the declared name (`request` and `response` /
    `exc`), then the annotation, then position. A lone unmatched parameter is the
    exception - it receives the second value. Binding is by keyword, so a
    positional-only parameter cannot be filled and a third required one raises.
    """
    # PEP 563 strings, evaluated once per callable; `eval_str` is all-or-none.
    evaluated = True
    try:
        params = inspect.signature(fn, eval_str=True).parameters
    except Exception:  # noqa: BLE001 - any unevaluatable annotation, not just NameError
        params = inspect.signature(fn).parameters
        evaluated = False

    if any(p.kind is p.VAR_KEYWORD for p in params.values()):
        return "request", second_name

    request_key = "request" if "request" in params else None
    second_key = second_name if second_name in params else None
    # No `VAR_KEYWORD` can reach here - the `any(...)` above returned.
    bindable = [p for p in params.values() if p.kind is not p.VAR_POSITIONAL]

    if evaluated and (request_key is None or second_key is None):
        second_type = _SECOND_VALUE_TYPES[second_name]
        for p in bindable:
            # Skip what cannot answer `issubclass` (`Request | None`, a string).
            if p.name in (request_key, second_key) or not isinstance(p.annotation, type):
                continue
            if request_key is None and issubclass(p.annotation, Request):
                request_key = p.name
            elif second_key is None and issubclass(p.annotation, second_type):
                second_key = p.name

    # Position fills only a parameter with no value yet; a default is the author
    # stating one, which the closure-capture idiom relies on.
    fillable = [
        p.name
        for p in bindable
        if p.name != request_key
        and p.name != second_key
        and p.default is p.empty
        and p.name not in ("self", "cls")
    ]
    if fillable and (request_key is None or second_key is None):
        if request_key is None and second_key is None and len(fillable) == 1:
            # Binding the request instead would fail silently.
            second_key = fillable[0]
        elif request_key is not None:
            second_key = fillable[0]
        elif second_key is None:
            for name in fillable:
                if request_key is None:
                    request_key = name
                elif second_key is None:
                    second_key = name
                else:
                    break
        # Left unbound deliberately, to reach the `unfillable` refusal below -
        # which fires on the first request this hook runs for, not at registration.

    unfillable = [n for n in fillable if n != request_key and n != second_key]
    if unfillable:
        raise TypeError(
            f"{getattr(fn, '__qualname__', None) or fn!r} declares required "
            f"parameter(s) {', '.join(unfillable)} that cannot be filled: only "
            f"the request and the {second_name} are passed, so give them a "
            "default or remove them"
        )
    return request_key, second_key


def _adapt_hook_kwargs(
    fn: Callable[..., Any],
    cache: MutableMapping[Any, _HookBinding],
    second_name: str,
    request: Request,
    second_value: Any,
) -> dict[str, Any]:
    """Select the kwargs `fn` accepts, from `request` and one other value.

    The binding is cached per callable, so the per-request cost is one lookup
    and the dict build. Not a coroutine: it is called from inside an existing
    `await`, and wrapping it would add a coroutine per hook per request.
    """
    # A callable that cannot be weakly referenced raises on lookup, not just insert.
    try:
        binding = cache.get(fn)
    except TypeError:
        binding = None
    if binding is None:
        binding = _resolve_hook_binding(fn, second_name)
        with contextlib.suppress(TypeError):
            cache[fn] = binding
    request_key, second_key = binding
    kwargs: dict[str, Any] = {}
    if request_key is not None:
        kwargs[request_key] = request
    if second_key is not None:
        kwargs[second_key] = second_value
    return kwargs


class DispatchMixin(AppHost):
    """The per-request HTTP dispatch pipeline, mixed into Veloce.

    Not slotted: the first-request latch assigns host state onto the composed
    `Veloce`, which carries a `__dict__`.
    """

    # ── Entry point and core dispatch ──────────────────────

    async def _body_too_large_response(
        self, request: Request, cp: CompiledPipeline | None, max_size: int | None
    ) -> Response:
        """Build the 413 for an over-`MAX_CONTENT_LENGTH` body, run the response phase.

        Offered to the app's error handlers as the `RequestEntityTooLarge` the
        streamed drain raises, so one registration reshapes the refusal at every
        point it can be made.
        """
        exc = body_too_large(max_size)
        handler = self._find_error_handler(request, exc)
        if handler is not None:
            response = await self._dispatch_exc_handler(handler, request, exc)
        else:
            # Encoded against `self`: the app contextvar is not bound yet.
            response = JSONResponse.from_bytes(
                dumps_for(self, too_large_payload(max_size)),
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            )
        # No route matched on a reject, so no per-route exclusion chain exists.
        if cp is not None and cp.http_post is not None:
            response = await self._run_response_phase(cp.http_post, request, response, False)
        return response

    async def handle_request(
        self, request: Request, cp: CompiledPipeline | None = None, match: Any = None
    ) -> Response:
        """Handle one request - run the middleware chain, then route dispatch.

        `cp` is the compiled pipeline, threaded in by `__call__` so the
        generation check runs once per request; a direct caller passes `None`.
        """
        if cp is None:
            cp = self._ensure_pipeline()
        # Lazy, so routes exist on first request whatever the entry point.
        if not self._openapi_setup:
            self._setup_openapi()

        request.app = self

        # Fall-through is intentional: dispatch may span tasks a token cannot.
        _current_app_var.set(self)
        _current_request_var.set(request)
        g._reset()

        # Inlined so an app with no receivers pays no call and no `**kwargs` pack.
        if request_started._subs:
            try:
                request_started.send(self, request=request)
            except Exception:
                self.logger.exception("request_started signal receiver raised")

        # One latch drains `before_first_request` and decides the setup lock; the
        # double-check guarantees single-fire under a race.
        if not self._first_request_fired:
            if self._first_request_lock is None:
                self._first_request_lock = asyncio.Lock()
            async with self._first_request_lock:
                if not self._first_request_fired:
                    for hook in self._before_first_request_hooks:
                        await self._call_handler(hook, {})
                    # Frozen outside DEBUG/TESTING so hot-reload and test
                    # monkeypatching can keep registering.
                    self._setup_locked = self._setup_lock_enabled and not (
                        self.debug or _coerce_bool(self.config.get("TESTING"))
                    )
                    self._first_request_fired = True

        # On the declared Content-Length and the buffered size; per RFC 9110
        # Sec. 15.5.14 the status is 413. Skipped when the transport applied it.
        max_size = None if request._length_enforced else self.config.get("MAX_CONTENT_LENGTH")
        if max_size is not None:
            declared = request.content_length
            over = declared is not None and declared > max_size
            # A streamed body has not arrived - draining it here would defeat
            # streaming - and the source raises 413 mid-read instead.
            if not over and request._body_drained:
                buffered = await request.body()
                over = len(buffered) > max_size
            if over:
                return await self._body_too_large_response(request, cp, max_size)

        # `None` exactly when `self._instrumentation` is empty, so an
        # un-instrumented app does not even read the clock.
        instrument = cp.http_finish is not None
        started = time.perf_counter() if instrument else 0.0

        try:
            if cp.http_around is not None:
                response = await self._run_http_middleware_chain(request, cp)
            else:
                response = await self._dispatch_request(request, cp, match)
        except Exception as exc:
            # Recorded before the exception continues out, so an error request is
            # never dropped from observability.
            if instrument:
                # Set by `_dispatch_request` before re-raising; unset elsewhere.
                request._state.setdefault("_error_type", type(exc).__qualname__)
                with contextlib.suppress(Exception):
                    await self._run_instrumentation(
                        request,
                        status.HTTP_500_INTERNAL_SERVER_ERROR,
                        (time.perf_counter() - started) * 1000.0,
                        end_time_ns=time.time_ns(),
                    )
            raise

        # Captured before the receivers run, so a span window stays accurate.
        timing: tuple[int, float] | None = None
        if instrument:
            timing = (time.time_ns(), (time.perf_counter() - started) * 1000.0)

        # Receivers may peek at the response, not replace it.
        if request_finished._subs:
            try:
                request_finished.send(self, response=response, request=request)
            except Exception:
                self.logger.exception("request_finished signal raised an exception")

        if timing is not None:
            # A HEAD response never iterates its body, so it is not a live stream.
            is_streamed = response.is_streamed and request.method != HTTP_METHOD_HEAD
            await self._run_instrumentation(
                request,
                response.status_code,
                timing[1],
                streamed=is_streamed,
                end_time_ns=timing[0],
            )

        return response

    async def _run_http_middleware_chain(self, request: Request, cp: CompiledPipeline) -> Response:
        """Run @app.middleware('http') functions with call_next pattern.

        `cp.http_around` is `None` when none are registered, in which case the
        caller does not reach here - so the slot is read with no `None` guard.
        """
        funcs: tuple[Callable[..., Any], ...] = cp.http_around  # type: ignore[assignment]

        def _make_next(level: int) -> Callable[..., Any]:
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

        Deliberately inline: extracting a phase out of this loop measured about
        5% per request. The teardown state the `finally` reads is bound before
        the `try` rather than where it is first used.
        """
        _exc: Exception | None = None
        # Bound here so the `finally` reads it even when dispatch raises early.
        _scheduled_bg = False
        _bp_name: str | None = None
        # Deferred until a non-trivial route demands it, and fresh per request: a
        # shared one would let one request's `reset()` clobber another's teardowns.
        resolver: DependencyResolver | None = None
        # A local, so the response phase skips a `_state` probe per response.
        excluded = False
        try:
            # Matched before the middleware request phase, so a route's
            # `exclude_middleware` opt-out can be honoured, and reused below.
            _matched_path = request.path
            _matched_method = request.method
            if match is None:
                match = self.match(_matched_method, _matched_path)
            if match is not None:
                request.endpoint = match.route_info.name
                request._state["url_rule"] = match.route_info.path_template

            # Straight-line fast path: with no app-level feature active and a
            # fast-eligible route, every phase skipped here is a no-op.
            if cp.is_bare and match is not None and match.route_info.is_fast_eligible:
                route_info = match.route_info
                # `is_bare` rules out url-value preprocessors and
                # `is_fast_eligible` route `defaults`, so these params are final.
                request.path_params = match.path_params
                # A blueprint's processors apply to its own routes, so they cost
                # the whole app only this lookup.
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
                # `_build_response` reduces to this one call, because
                # `is_fast_eligible` rules out every other branch of it.
                response = self._coerce_response(result)
                # `is_bare` leaves only the one-shot callbacks; probed inline.
                if request._state and request._state.get("_after_this_request"):
                    response = await self._run_after_hooks(request, response, None)
                # The `finally` still runs, so this decision is taken there.
                _scheduled_bg = self._schedule_background_tasks(request, response)
                return response

            # Runs on a route miss too, so a CORS preflight is still answered.
            if cp.http_pre is not None or (
                match is not None and match.route_info.excluded_middleware is not None
            ):
                early_response, excluded = await self._run_request_phase(request, match, cp)
                if early_response is not None:
                    return early_response

            # With none registered the await is skipped, but `bp_name` is still
            # derived: `_resolve_route` can exit early and the teardown needs it.
            if self._before_request_hooks or self._bp_before_hooks:
                early, _bp_name = await self._run_before_hooks(request)
                if early is not None:
                    return early
            else:
                _bp_name = _endpoint_blueprint(request.endpoint)

            resolved = await self._resolve_route(
                request, match, _matched_path, _matched_method, cp, excluded
            )
            if isinstance(resolved, Response):
                return resolved
            match = resolved
            _bp_name = _endpoint_blueprint(request.endpoint)

            # The response-phase chain is NOT refreshed from the final matched
            # route: the set that ran `process_request` is the set that unwinds.

            # Bound before the handler call, so the `finally` sees it on a raise.
            kwargs, resolver = await self._resolve_dependencies(request, match)
            route_info = match.route_info
            result = await self._call_handler(
                route_info.handler,
                kwargs,
                is_coro=(
                    route_info.handler_plan.is_coro if route_info.handler_plan is not None else None
                ),
            )

            response = self._build_response(request, match, result)

            # With none registered the helper does no work, so skip the frame.
            if (
                self._after_request_hooks
                or (self._bp_after_hooks and _bp_name is not None)
                or (request._state and request._state.get("_after_this_request"))
            ):
                response = await self._run_after_hooks(request, response, _bp_name)

            # Fire-and-forget; a scheduled task owns releasing the spool files,
            # because it may still be reading them.
            _scheduled_bg = self._schedule_background_tasks(request, response)

            # `None` when no middleware is registered. A route that stashed a
            # filtered chain takes the dynamic walk instead of the fused one.
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
            return await self._http_exception_response(request, exc, cp, excluded)
        except Exception as exc:
            _exc = exc
            handler = self._find_scoped_exception_handler(type(exc), request)
            if handler:
                return await self._run_exc_handler_response(request, exc, handler, cp, excluded)
            # The class name only, so no exception object reaches observability.
            request._state["_error_type"] = type(exc).__qualname__
            # Inline, so the bare `raise` keeps the original traceback.
            if self._should_propagate_exceptions():
                raise
            # Logged here or it is lost: the exception does not leave the app, so
            # no server ever sees it.
            self.log_exception(exc, request)
            return await self._shape_server_error(request, exc, cp, excluded)
        finally:
            # Yield-dependency teardowns first - they wrap the request. A failure
            # is logged; under PROPAGATE_EXCEPTIONS it re-raises at the END of
            # this `finally`, so the hooks and signals below still run.
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

            # Always run; MCP replays them via `_run_request_teardown`.
            if self._teardown_request_hooks or self._bp_teardown_hooks:
                _td_hooks = self._select_teardown_request_hooks(_bp_name)
                if _td_hooks:
                    await self._run_teardown_hooks(_td_hooks, _exc, "teardown_request")

            # Skipped when a background task was scheduled - it releases them.
            if not _scheduled_bg and request._form is not None:
                request._close_uploads()

            # The app context pops per request. Errors are logged, not re-raised.
            if self._teardown_appcontext_hooks:
                await self._run_teardown_hooks(
                    self._teardown_appcontext_hooks, _exc, "teardown_appcontext"
                )

            # A receiver may raise - log + continue, so it cannot poison dispatch.
            if (_exc is not None and got_request_exception._subs) or request_tearing_down._subs:
                try:
                    if _exc is not None and got_request_exception._subs:
                        got_request_exception.send(self, exception=_exc)
                    if request_tearing_down._subs:
                        request_tearing_down.send(self, exc=_exc)
                except Exception:
                    self.logger.exception("signal receiver raised an exception")

            # Deferred from above, now that the hooks and signals have run.
            if _teardown_to_propagate is not None:
                raise _teardown_to_propagate

    # ── Request phase and error shaping ────────────────────

    async def _run_request_phase(
        self, request: Request, match: Any, cp: CompiledPipeline
    ) -> tuple[Response | None, bool]:
        """Run the request-phase middleware; return `(early_response, excluded)`.

        A route declaring `exclude_middleware` runs a memoised filtered chain and
        stashes the matching response-phase chain on `request._state` for
        symmetric skip. A non-`None` response has been through the response phase.
        """
        if match is not None and match.route_info.excluded_middleware is not None:
            # `None` would mean no exclusions, which the caller ruled out.
            filtered = self._route_middleware_chains(match.route_info)
            request._state[_MW_RESPONSE_CHAIN_KEY] = filtered[1]  # type: ignore[index]
            for mw in filtered[0]:  # type: ignore[index]
                early = await mw.process_request(request)
                if early is not None:
                    return await self._run_response_middleware(request, early), True
            return None, True
        # `or ()` keeps the loop typed; the caller's guard set `cp.http_pre`.
        for process_request in cp.http_pre or ():
            early = await process_request(request)
            if early is not None:
                return await self._run_response_phase(cp.http_post, request, early, False), False
        return None, False

    async def _run_exc_handler_response(
        self,
        request: Request,
        exc: Exception,
        handler: Callable[..., Any],
        cp: CompiledPipeline,
        excluded: bool,
    ) -> Response:
        """Run a registered exception handler and apply the response phase."""
        response = await self._dispatch_exc_handler(handler, request, exc)
        response = await self._run_response_phase(cp.http_post, request, response, excluded)
        return response

    def _find_error_handler(
        self, request: Request, exc: HTTPException
    ) -> Callable[..., Any] | None:
        """Find the error handler for `exc` on this request, or `None`.

        A status-code handler wins over a class handler; the class walk follows
        the MRO, and both prefer the request's blueprint chain over the
        app-level tables. Returns the handler, not the response, because each
        caller owns a different default and a different phase to run.
        """
        return self._find_scoped_status_handler(
            exc.status_code, request
        ) or self._find_scoped_exception_handler(type(exc), request)

    async def _http_exception_response(
        self,
        request: Request,
        exc: HTTPException,
        cp: CompiledPipeline | None,
        excluded: bool = False,
    ) -> Response:
        """Build the response for an `HTTPException` and run the response phase.

        The default body is `{"detail": ..., "status_code": ...}` with
        `exc.headers` applied; a validation error emits its structured `.errors`
        list as `{"detail": [...]}`. `cp` is `None` for a caller with no pipeline.
        """
        handler = self._find_error_handler(request, exc)
        if handler is not None:
            response = await self._dispatch_exc_handler(handler, request, exc)
        else:
            response = JSONResponse.from_bytes(
                dumps_for(self, http_exception_payload(exc)),
                status_code=exc.status_code,
                headers=exc.headers or None,
            )
        # `excluded` implies middleware exists, so `http_post` is not None.
        if cp is not None and cp.http_post is not None:
            response = await self._run_response_phase(cp.http_post, request, response, excluded)
        return response

    async def _shape_server_error(
        self, request: Request, exc: Exception, cp: CompiledPipeline, excluded: bool
    ) -> Response:
        """Shape an unhandled, non-propagated exception into a 500 response.

        In debug mode an HTML client gets the rich traceback page and every
        other client the plain-text traceback.
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

        The blueprint bucket is selected from `request.endpoint` *after* the
        app-level hooks run, so a hook that rewrites the endpoint is honoured.
        `bp_name` is the teardown blueprint, so a short-circuit inside an
        app-level hook means no blueprint teardown.
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

        Returns either a Response (already through response middleware) or the
        match to dispatch, having populated `path_params`, defaults, endpoint
        and url_rule and run the URL value preprocessors. Raises `HTTPException`
        for the 404 / constraint-mismatch cases.
        """
        # A linear prefix scan, kept deliberately: ~0.07 us per mount per request
        # (measured min-of-7 over 5k requests each: 0 mounts 5.54 us, 1 mount
        # 6.74 us, 3 mounts 6.91 us, 10 mounts 7.33 us, 50 mounts 10.14 us).
        if cp.has_mounted_apps:
            for prefix, prefix_slash, sub_app in self._mounted_apps:
                if request.path.startswith(prefix_slash) or request.path == prefix:
                    sub_path = request.path[len(prefix) :] or "/"
                    # Only the sub-app knows whether its route streams: hand the
                    # body source over when it does, otherwise drain here.
                    sub_match = sub_app.match(request.method, sub_path)
                    streams = sub_match is not None and sub_match.route_info.stream
                    # Stacks under the parent's root_path when it is mounted too.
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

        if cp.has_static_handlers:
            for static in self._static_handlers:
                response = await static.handle(request)
                if response is not None:
                    return await self._run_response_middleware(request, response)

        # Re-match only when a hook rewrote the path or method.
        if request.path != matched_path or request.method != matched_method:
            match = self.match(request.method, request.path)

        # The host must be `{subdomain}.{SERVER_NAME}`; `"*"` accepts any. A
        # mismatch is 404, not 405 - the path is reachable, not from this host.
        if (
            match is not None
            and match.route_info.subdomain is not None
            and not self._subdomain_matches(request, match.route_info.subdomain)
        ):
            raise NotFound()

        # Case-insensitive and port-stripped; a mismatch is likewise a 404.
        if match is not None and match.route_info.host is not None:
            req_host = _extract_host(request.headers.get(HEADER_HOST, "") or "")
            if req_host != match.route_info.host:
                raise NotFound()

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
                # `alt` is mount-local, so `Location` must carry `root_path`.
                target = request.root_path + alt
                if target.startswith("//"):
                    # `//` is protocol-relative: the browser reads what follows
                    # as a host, so a reflected path is an open redirect.
                    target = "/" + target.lstrip("/")
                response = RedirectResponse(target, status_code=code)
                response = await self._run_response_phase(cp.http_post, request, response, excluded)
                return response

        if match is None:
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
                # RFC 9110 Sec. 15.5.6: a 405 names the methods it does serve, so
                # `Allow` rides on the exception and the body carries `allowed`.
                exc_405 = MethodNotAllowed(headers={HEADER_ALLOW: ", ".join(allowed)})
                return await self._handle_error(
                    request,
                    exc_405.status_code,
                    JSONResponse(
                        {"detail": exc_405.detail, "allowed": allowed},
                        status_code=exc_405.status_code,
                        headers=exc_405.headers,
                    ),
                    exc_405,
                )
            raise NotFound()

        request.path_params = match.path_params
        if match.route_info.defaults:
            for _dk, _dv in match.route_info.defaults.items():
                request.path_params.setdefault(_dk, _dv)
        request.endpoint = match.route_info.name
        request._state["url_rule"] = match.route_info.path_template

        # Inline so a route with none pays no frame; MCP runs the chain via the
        # helper below.
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

        Each receives `(endpoint, path_params)` and may mutate it in place.
        App-level processors run first, then the endpoint's blueprint's.
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
    ) -> tuple[dict[str, Any], DependencyResolver | None]:
        """Build the handler kwargs and the resolver that backs them.

        `None` for trivial / request-only plans. The caller must hold the
        resolver so the dispatch `finally` can run its yield-dependency
        teardowns even when the handler raises.
        """
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
        """Turn a handler return value into the final Response."""
        route_info = match.route_info
        # Every backend shapes, or a subclass would put its extra fields on the wire.
        if route_info.response_model is not None and not isinstance(result, Response):
            result = self._apply_response_model(result, route_info)

        response = self._coerce_response(result, route_info.response_class)

        # Applies only while the response still carries the default 200, so one
        # the handler set itself wins.
        if (
            route_info.status_code != status.HTTP_200_OK
            and response.status_code == status.HTTP_200_OK
        ):
            response.status_code = route_info.status_code
            response._encoded = None

        # A returned Response keeps its own; `0` means the handler set none.
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

        Reverse registration order, and each may return a replacement Response.
        """
        for hook in reversed(self._after_request_hooks):
            hook_result = await self._call_after_hook(hook, request, response)
            if isinstance(hook_result, Response):
                response = hook_result
        if self._bp_after_hooks and bp_name is not None:
            for hook in reversed(self._bp_after_hooks.get(bp_name, ())):
                hook_result = await self._call_after_hook(hook, request, response)
                if isinstance(hook_result, Response):
                    response = hook_result

        # After the global hooks, so a per-request adjustment sees their work.
        one_shot = request._state.get("_after_this_request") if request._state else None
        if one_shot:
            for fn in one_shot:
                fn_result = await self._call_after_hook(fn, request, response)
                if isinstance(fn_result, Response):
                    response = fn_result
        return response

    def _schedule_background_tasks(self, request: Request, response: Response) -> bool:
        """Schedule the DI-injected queue and response-attached background task.

        Both run through `spawn()`, the single tracked-task path. Returns whether
        anything was scheduled: a background task may still be reading an upload,
        so the caller uses this to decide when to release the spool files.
        """
        injected = request._background_tasks
        attached_bg = response.background
        # Checked before anything is allocated: most responses have neither.
        if injected is None and attached_bg is None:
            return False

        coros = []
        if injected is not None:
            coros.append(injected.run_all())

        if attached_bg is not None:
            # Anything with a `run_all()` or `run()` coroutine method works.
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

        # A done-callback rather than another spawned task: a failed task still
        # counts as done, so a failure cannot strand the files.
        form = request._form
        if form is not None:
            remaining = len(tasks)

            # Not over `request`, whose body and scope would stay alive with it.
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
        self, request: Request, status_code: int, default: Response, exc: BaseException
    ) -> Response:
        """Build an error whose body is composed here, through a handler when one matches.

        For the refusals that are not raised - the 405 and the degraded 500.
        `default` is what the framework answers when nothing is registered.
        """
        if isinstance(exc, HTTPException):
            # Status table then class table, so a broad handler reshapes this too.
            handler = self._find_error_handler(request, exc)
            response = None
            if handler is not None:
                response = await self._dispatch_exc_handler(handler, request, exc)
                # A handler answering this same status inherits the refusal's
                # headers: RFC 9110 Sec. 15.5.6 makes `Allow` a MUST on a 405.
                if exc.headers and response.status_code == exc.status_code:
                    for _name, _value in exc.headers.items():
                        if not header_present(response.headers, _name):
                            response.headers[_name] = _value
        else:
            # A `@bp.errorhandler(500)` wins over the app-level table. The class
            # tables are deliberately not re-walked: the caller already missed them.
            handler = self._find_scoped_status_handler(status_code, request)
            # Through the same guard, since a handler raising here has no outer
            # `except`.
            response = (
                await self._dispatch_exc_handler(handler, request, exc)
                if handler is not None
                else None
            )
        return await self._run_response_middleware(
            request, default if response is None else response
        )

    def _subdomain_matches(self, request: Request, subdomain: str) -> bool:
        """Check whether `request`'s host carries the expected subdomain.

        `"*"` matches any non-empty subdomain. The request's own subdomain comes
        from `Request.subdomain`, which short-circuits an IP literal - a second
        derivation would match `subdomain="192"` against `192.168.1.1`.
        """
        actual = request.subdomain
        if subdomain == "*":
            return bool(actual)
        return actual == subdomain

    async def _call_handler(
        self, handler: Callable[..., Any], kwargs: dict[str, Any], is_coro: bool | None = None
    ) -> Any:
        """Call a handler, supporting both sync and async.

        Sync handlers are offloaded to the thread pool. `is_coro` lets a caller
        that already knows skip the per-request probe.
        """
        if is_coro is None:
            is_coro = _is_async_callable(handler)
        if is_coro:
            return await handler(**kwargs)
        # `offload` preserves request-scoped ContextVars.
        return await offload(handler, **kwargs)

    async def _call_after_hook(
        self, hook: Callable[..., Any], request: Request, response: Response
    ) -> Any:
        """Call an after-request hook, adapting kwargs to match its signature."""
        kwargs = _adapt_hook_kwargs(hook, _after_hook_sig_cache, "response", request, response)
        return await self._call_handler(hook, kwargs)

    async def _call_exc_handler(
        self, handler: Callable[..., Any], request: Request, exc: BaseException
    ) -> Any:
        """Call an exception handler, adapting kwargs to match its signature.

        A file such a handler returns stands in for a representation the request
        did not get, so the error-page flag set here keeps it from being sliced
        to that request's `Range`.
        """
        kwargs = _adapt_hook_kwargs(handler, _exc_handler_sig_cache, "exc", request, exc)
        token = _serving_error_page_var.set(True)
        try:
            return await self._call_handler(handler, kwargs)
        finally:
            _serving_error_page_var.reset(token)

    async def _dispatch_exc_handler(
        self, handler: Callable[..., Any], request: Request, exc: BaseException
    ) -> Response:
        """Invoke a user exception handler with a guard around its own raises.

        The secondary failure is logged, naming the handler and the request
        path, and the standard 500 returned; under `PROPAGATE_EXCEPTIONS` it is
        re-raised so the handler bug is visible.
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

        A `list[Model]` declaration validates each element individually.
        """
        model = route_info.response_model
        # Resolved at registration; read, never mutated - shared across requests.
        dump_kwargs = route_info.response_dump_kwargs

        origin = route_info.response_model_origin
        if origin is list:
            args = get_args(model)
            if args:
                inner = args[0]
                if not isinstance(result, (list, tuple)):
                    return result  # let downstream coercion handle the mismatch
                if is_pydantic_model(inner):
                    dumped: list[Any] = []
                    for item in result:
                        # Exactly the target model dumps directly, preserving the
                        # fields-set markers `exclude_unset` reads.
                        if type(item) is inner:
                            dumped.append(item.model_dump(**dump_kwargs))
                        else:
                            # Dumped first: Pydantic does not revalidate an
                            # instance that already satisfies the target.
                            payload = (
                                item.model_dump() if isinstance(item, _PydanticBaseModel) else item
                            )
                            dumped.append(inner.model_validate(payload).model_dump(**dump_kwargs))
                    return dumped
                if backend_of(inner) is not ModelBackend.NONE:
                    # `dump_kwargs` is Pydantic's vocabulary and cannot apply here.
                    return [
                        item if type(item) is inner else shape_through_model(item, inner)
                        for item in result
                    ]
            return result

        # Scalar Pydantic model.
        if is_pydantic_model(model):
            # The roundtrip would erase the `__pydantic_fields_set__` info that
            # drives `exclude_unset`, so a SUBCLASS must not take this path.
            if type(result) is model:
                return result.model_dump(**dump_kwargs)
            # `exclude_unset` resets: the fields-set markers do not transfer.
            payload = result.model_dump() if isinstance(result, _PydanticBaseModel) else result
            validated = model.model_validate(payload)
            return validated.model_dump(**dump_kwargs)

        # msgspec struct / dataclass / TypedDict shape through the shared shaper.
        # The exact type is tested first - `backend_of` walks an isinstance ladder.
        if type(result) is model:
            return result
        if route_info.response_model_backend is not ModelBackend.NONE:
            # A full builtins round-trip, kept off the common path above.
            return shape_through_model(result, model)

        return result

    @staticmethod
    def _response_class_mismatch(response_class: Any, result: Any) -> str:
        """Build the message for a return the declared response class cannot render.

        Without it a `dict` reaches `.encode()` and produces an `AttributeError`
        naming neither the class asked for nor the value returned.
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
        JSON option, in which case that dialect applies here too.
        """
        dumps = self._json_dumps_override()
        # `from_bytes` on both branches: encoding here rather than through
        # `JSONResponse(data)` avoids resolving the dialect twice.
        body = orjson.dumps(data, default=orjson_default) if dumps is None else dumps(data)
        return JSONResponse._from_encoded(body)

    def _json_dumps_override(self) -> Any:
        """Return the configured serialiser, or `None` to take the direct path.

        Resolved once and cached. `None` is the stock case, where the direct
        path already emits exactly what the provider would.
        """
        dumps = self._handler_json_dumps
        if dumps is _UNRESOLVED_JSON_DUMPS:
            dumps = self._handler_json_dumps = self._resolve_handler_json_dumps()
        return dumps

    def _coerce_response(self, result: Any, response_class: Any = None) -> Response:
        """Convert handler return value to a Response object."""
        if isinstance(result, Response):
            return result
        # Gated on `response_class is None` so the JSONResponse-subclass branch
        # below still owns dicts.
        if type(result) is dict and response_class is None:
            return self._json_from_handler(result)
        # A msgspec struct encodes in C with no intermediate dict; with a
        # response_class it is normalized to builtins so that class renders it.
        if _HAS_MSGSPEC and _is_msgspec_payload(result):
            if response_class is None:
                dumps = self._json_dumps_override()
                if dumps is None:
                    return Response(body=_msgspec.json.encode(result), content_type=MIME_JSON)
                # A configured dialect outranks the struct fast path.
                return JSONResponse.from_bytes(dumps(_msgspec.to_builtins(result)))
            result = _msgspec.to_builtins(result)
        # Through the one table `make_response` uses; any other length is a value.
        if isinstance(result, tuple):
            unpacked = _unpack_response_tuple(result)
            if unpacked is not None:
                body, code, headers = unpacked
                resp = self._coerce_response(body, response_class)
                if code is not None:
                    resp.status_code = code
                if headers:
                    resp.headers.update(headers)
                # The status line and headers changed, so a cached encoding is stale.
                resp._encoded = None
                return resp
        if response_class is not None:
            if isinstance(response_class, type) and issubclass(response_class, JSONResponse):
                if isinstance(result, _PydanticBaseModel):
                    result = result.model_dump()
                dumps = self._json_dumps_override()
                if dumps is None:
                    return response_class(result)
                # On the requested class, so a subclass keeps its media type.
                return response_class.from_bytes(dumps(result))
            if isinstance(result, (str, bytes)):
                return response_class(result)
            # Try, then translate: refusing before attempting would also turn
            # away a user-defined serialiser that renders the value perfectly well.
            try:
                return response_class(result)
            except (AttributeError, TypeError) as exc:
                raise TypeError(self._response_class_mismatch(response_class, result)) from exc
        if isinstance(result, (dict, list)):
            return self._json_from_handler(result)
        if isinstance(result, str):
            # text/html is the same default `make_response()` applies.
            return Response(body=result.encode(), content_type=MIME_HTML)
        if isinstance(result, bytes):
            return Response(body=result, content_type=MIME_HTML)
        if isinstance(result, _PydanticBaseModel):
            return self._json_from_handler(result.model_dump())
        return self._json_from_handler(result)

    # ── Middleware phases ──────────────────────────────────

    async def _run_request_middleware(
        self, request: Request, chain: list[Middleware] | None = None
    ) -> Response | None:
        """Run the middleware request phase in registration order.

        The first `process_request` to return a `Response` short-circuits the
        chain. `chain` defaults to the app's full middleware list; the MCP caller
        passes the route's pre-filtered chain so `exclude_middleware` applies there.
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

        `None` when the route excludes nothing, signalling callers to use the
        app's middleware list directly with no copy or filter. Otherwise the
        chains are memoised on the RouteInfo, keyed on `self._mw_version`.
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
        fused: tuple[Callable[..., Any], ...] | None,
        request: Request,
        response: Response,
        excluded: bool,
    ) -> Response:
        """Apply the response phase, preferring the compile-time fused chain.

        `excluded` records whether this request stashed a per-route filtered
        response chain, passed in rather than re-probed from `request._state`
        per response. `fused` is `None` only when no middleware is registered.
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
        request phase, so `exclude_middleware` applies symmetrically.
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

        A hook may be sync or async; one that raises is logged and skipped so
        observability code can never break the response. `streamed` marks a
        response whose `duration_ms` / `status_code` cover only its production,
        not stream completion.
        """
        # Only for a 5xx, so a deliberate 5xx return is not mislabelled.
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
            # Built on every dispatch path - a before_request hook could be skipped.
            parent_context=_trace_carrier(request),
        )
        # Sparse, so the membership test is skipped when none are configured.
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

    # ── Dispatch aliases and response coercion ─────────────

    # Both alias `_dispatch_request`, which runs the full chain inline.
    async def dispatch_request(self, request: Request) -> Any:
        """Alias for `_dispatch_request`."""
        return await self._dispatch_request(request, self._ensure_pipeline())

    async def full_dispatch_request(self, request: Request) -> Any:
        """Dispatch `request` through the full before/after-request hook chain."""
        return await self._dispatch_request(request, self._ensure_pipeline())

    async def preprocess_request(self, request: Request) -> Any:
        """Run all `before_request` hooks for `request`.

        A non-None return short-circuits the chain and becomes the response.
        App-level hooks fire first, then the matched-blueprint bucket.
        """
        for hook in self._before_request_hooks:
            result = await self._call_handler(hook, {"request": request})
            if result is not None:
                return result
        # `endpoint` is a slot assigned in `__init__`, so no default can apply.
        bp = _endpoint_blueprint(request.endpoint)
        if bp is not None and self._bp_before_hooks:
            for hook in self._bp_before_hooks.get(bp, ()):
                result = await self._call_handler(hook, {"request": request})
                if result is not None:
                    return result
        return None

    async def process_response(self, request: Request, response: Response) -> Response:
        """Run all `after_request` hooks for `(request, response)`.

        Hooks fire in **reverse** registration order; each may return a
        replacement `Response`, and any other return keeps the existing one.
        This is the dispatch path itself, not a re-implementation.
        """
        return await self._run_after_hooks(request, response, _endpoint_blueprint(request.endpoint))

    @staticmethod
    def ensure_sync(func: Callable[..., Any]) -> Callable[..., Any]:
        """Wrap `func` so it is callable from synchronous code.

        A regular function is returned unchanged; a coroutine function comes
        back wrapped to run on a dedicated event loop. Use it to bridge async
        handlers into CLI commands, background workers or test scaffolding.
        """
        if not _is_async_callable(func):
            return func

        @functools.wraps(func)
        def _sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            return asyncio.run(func(*args, **kwargs))

        return _sync_wrapper

    def make_response(self, value: Any) -> Response:
        """Coerce a handler-return value into a `Response`.

        Accepts (with this coercion table):
        - `Response` -> returned as-is
        - `str` / `bytes` -> wrapped as a text/HTML response
        - `dict` / `list` -> wrapped as a JSON response via `jsonify`
        - `tuple` of `(body, status)`, `(body, status, headers)` or
          `(body, headers)` -> unpacked and re-coerced
        - anything else -> JSON, matching what dispatch does with the same
          value returned from a handler

        A tuple of any other length is not a response tuple and is answered as
        a plain value.
        """
        if isinstance(value, Response):
            return value
        if isinstance(value, tuple):
            unpacked = _unpack_response_tuple(value)
            if unpacked is not None:
                body, code, headers = unpacked
                resp = self.make_response(body)
                if code is not None:
                    resp.status_code = code
                if headers:
                    items = headers.items() if isinstance(headers, dict) else headers
                    for k, v in items:
                        resp.headers[k] = v
                # The status line and headers changed, so a cached encoding is stale.
                resp._encoded = None
                return resp
        if isinstance(value, (dict, list)):
            return jsonify(value)
        if isinstance(value, bytes):
            return Response(body=value, content_type=MIME_HTML)
        if isinstance(value, str):
            return Response(
                body=value.encode("utf-8"),
                content_type=MIME_HTML,
            )
        # JSON, matching what dispatch does with the same value from a handler.
        return jsonify(value)
