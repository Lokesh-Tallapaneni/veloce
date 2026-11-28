"""HTTP request dispatch — the per-request pipeline mixed into Veloce.

`handle_request` -> `_dispatch_request` runs the middleware phases, the
before/after hooks, route and dependency resolution, the handler call, response
shaping and teardown.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Callable
from typing import Any

import veloce.status as status
from veloce._handler_plan import split_positional_only
from veloce._internal import (
    _coerce_bool,
    _current_app_var,
    _current_request_var,
    _is_async_callable,
    dumps_for,
    offload,
)
from veloce._pipeline import (
    CompiledPipeline,
)
from veloce._protocol_constants import (
    HTTP_METHOD_HEAD,
)
from veloce.app._host import AppHost
from veloce.app.dispatch_aliases import AliasDispatchMixin
from veloce.app.dispatch_errors import ErrorDispatchMixin
from veloce.app.dispatch_hooks import HookDispatchMixin
from veloce.app.dispatch_middleware import MiddlewareDispatchMixin
from veloce.app.dispatch_response import ResponseDispatchMixin
from veloce.app.dispatch_support import (
    _FIRST_REQUEST_POLL_SECONDS,
)
from veloce.app.dispatch_support import _adapt_hook_kwargs as _adapt_hook_kwargs
from veloce.app.dispatch_support import _after_hook_sig_cache as _after_hook_sig_cache
from veloce.app.dispatch_support import _exc_handler_sig_cache as _exc_handler_sig_cache
from veloce.app.dispatch_support import _resolve_hook_binding as _resolve_hook_binding
from veloce.blueprints import _endpoint_blueprint
from veloce.dependency import DependencyResolver
from veloce.exceptions import (
    HTTPException,
)
from veloce.helpers import g
from veloce.http._body import body_too_large, too_large_payload
from veloce.http.request import Request
from veloce.http.response import (
    JSONResponse,
    Response,
)
from veloce.signals import (
    got_request_exception,
    request_finished,
    request_started,
    request_tearing_down,
)


class DispatchMixin(
    ErrorDispatchMixin,
    HookDispatchMixin,
    ResponseDispatchMixin,
    MiddlewareDispatchMixin,
    AliasDispatchMixin,
    AppHost,
):
    """The per-request HTTP dispatch pipeline, mixed into Veloce.

    The entry points (`handle_request`, `_dispatch_request`, `_call_handler`)
    live here; error shaping, hooks and route resolution, response shaping,
    the middleware phases and the public aliases are sibling mixins composed in
    through the bases.

    Not slotted: the first-request latch assigns host state onto the composed
    `Veloce`, which carries a `__dict__`.
    """

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

    async def _fire_first_request(self) -> None:
        """Run the `before_first_request` hooks once per app, on any loop.

        The latch cannot be an `asyncio.Lock`: it binds to the first event loop
        that blocks on it, so an app behind a loop per thread answered later
        first requests with `RuntimeError: ... is bound to a different event
        loop`. A `threading.Lock` elects the one caller that runs the hooks,
        and the others poll the flag rather than wait on a loop-bound future.

        A hook that raises releases the claim, so the next request retries.
        """
        while not self._first_request_fired:
            with self._first_request_guard:
                claimed = not self._first_request_running
                if claimed:
                    self._first_request_running = True
            if not claimed:
                # Polling, not an executor: a sync `before_first_request` hook
                # may itself be occupying the default executor's threads.
                await asyncio.sleep(_FIRST_REQUEST_POLL_SECONDS)
                continue
            try:
                for hook in self._before_first_request_hooks:
                    await self._call_handler(hook, {})
                # Frozen outside DEBUG/TESTING, so hot-reload keeps working.
                self._setup_locked = self._setup_lock_enabled and not (
                    self.debug or _coerce_bool(self.config.get("TESTING"))
                )
                self._first_request_fired = True
            finally:
                with self._first_request_guard:
                    self._first_request_running = False

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

        # One latch for both. Kept to a flag read here; the rest is cold.
        if not self._first_request_fired:
            await self._fire_first_request()

        # On the declared Content-Length and the buffered size; RFC 9110
        # Sec. 15.5.14 makes it a 413.
        max_size = None if request._length_enforced else self.config.get("MAX_CONTENT_LENGTH")
        if max_size is not None:
            declared = request.content_length
            over = declared is not None and declared > max_size
            # A streamed body has not arrived; its source raises 413 mid-read.
            if not over and request._body_drained:
                buffered = await request.body()
                over = len(buffered) > max_size
            if over:
                return await self._body_too_large_response(request, cp, max_size)

        # Empty instrumentation means the clock is not even read.
        instrument = cp.http_finish is not None
        started = time.perf_counter() if instrument else 0.0

        try:
            if cp.http_around is not None:
                response = await self._run_http_middleware_chain(request, cp)
            else:
                response = await self._dispatch_request(request, cp, match)
        except Exception as exc:
            # Recorded before the exception continues out, or it is never seen.
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
        end_time_ns = 0
        duration_ms = 0.0
        if instrument:
            end_time_ns = time.time_ns()
            duration_ms = (time.perf_counter() - started) * 1000.0

        # Receivers may peek at the response, not replace it.
        if request_finished._subs:
            try:
                request_finished.send(self, response=response, request=request)
            except Exception:
                self.logger.exception("request_finished signal raised an exception")

        if instrument:
            # A HEAD response never iterates its body, so it is not a live stream.
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

        `cp.http_around` is `None` when none are registered, in which case the
        caller does not reach here - so the slot is read with no `None` guard.
        """
        funcs: tuple[Callable[..., Any], ...] = cp.http_around  # type: ignore[assignment]  # narrowed by the is_bare check above

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
        # Fresh per request: a shared one's `reset()` would clobber the teardowns.
        resolver: DependencyResolver | None = None
        # A local, so the response phase skips a `_state` probe per response.
        excluded = False
        try:
            # Before the request phase, so `exclude_middleware` can be honoured.
            _matched_path = request.path
            _matched_method = request.method
            if match is None:
                match = self.match(_matched_method, _matched_path)
            if match is not None:
                request.endpoint = match.route_info.name
                request._state["url_rule"] = match.route_info.path_template

            # Straight-line fast path: every phase skipped here is a no-op.
            if cp.is_bare and match is not None and match.route_info.is_fast_eligible:
                route_info = match.route_info
                # No preprocessors and no route `defaults`, so these are final.
                request.path_params = match.path_params
                # A blueprint's processors cost the whole app only this lookup.
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
                # `is_fast_eligible` rules out every other `_build_response` branch.
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

            # `bp_name` is derived either way: the teardown below needs it.
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

            # Not refreshed from the final match: the set that ran
            # `process_request` is the set that unwinds.
            kwargs, resolver = await self._resolve_dependencies(request, match)
            route_info = match.route_info
            handler_plan = route_info.handler_plan
            invoke = handler_plan.invoke if handler_plan is not None else None
            if invoke is not None:
                result = await invoke(**kwargs)
            else:
                result = await self._call_handler(
                    route_info.handler,
                    kwargs,
                    is_coro=(handler_plan.is_coro if handler_plan is not None else None),
                    positional_only=(
                        handler_plan.positional_only if handler_plan is not None else ()
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

            # Fire-and-forget; a scheduled task releases the spool files itself.
            _scheduled_bg = self._schedule_background_tasks(request, response)

            # A route that stashed a filtered chain takes the dynamic walk.
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
            # Logged here or lost: the exception does not leave the app.
            self.log_exception(exc, request)
            return await self._shape_server_error(request, exc, cp, excluded)
        finally:
            # A teardown failure is logged; under PROPAGATE_EXCEPTIONS it re-raises
            # at the END of this `finally`, so the hooks and signals still run.
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

            if _teardown_to_propagate is not None:
                raise _teardown_to_propagate

    async def _call_handler(
        self,
        handler: Callable[..., Any],
        kwargs: dict[str, Any],
        is_coro: bool | None = None,
        positional_only: tuple[str, ...] = (),
    ) -> Any:
        """Call a handler, supporting both sync and async.

        Sync handlers are offloaded to the thread pool. `is_coro` lets a caller
        that already knows skip the per-request probe. `positional_only` names
        the resolved kwargs that must be passed positionally, for a handler
        declaring a `POSITIONAL_ONLY` parameter (`def f(a, b, /)`).
        """
        if is_coro is None:
            is_coro = _is_async_callable(handler)
        if positional_only:
            positional, kwargs = split_positional_only(kwargs, positional_only, handler)
        else:
            positional = ()
        if is_coro:
            return await handler(*positional, **kwargs)
        # `offload` preserves request-scoped ContextVars.
        return await offload(handler, *positional, **kwargs)
