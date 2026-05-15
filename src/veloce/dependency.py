"""Dependency injection — pre-planned at registration, executed per request.

Public API: `Depends`, `Security`, `DependencyResolver`. The resolver walks a
pre-built `HandlerPlan` (see `veloce._handler_plan`) rather than reflecting on
the handler signature per request.
"""

from __future__ import annotations

import contextlib
import inspect
from collections.abc import Callable
from typing import Any, get_args, get_origin

from pydantic import ValidationError as PydanticValidationError

from veloce.exceptions import RequestValidationError, ValidationError
from veloce.http.request import Request


class Depends:
    """Dependency marker — use in function signature defaults.

    `dependency` may be omitted (`Depends()`); the resolver then infers
    it from the parameter's type annotation — the shorthand for
    `x: SomeClass = Depends()`.
    """

    __slots__ = ("dependency", "use_cache")

    def __init__(self, dependency: Callable | None = None, use_cache: bool = True) -> None:
        self.dependency = dependency
        self.use_cache = use_cache


class Security(Depends):
    """Dependency marker with OAuth2 scopes for OpenAPI emission."""

    __slots__ = ("scopes",)

    def __init__(
        self,
        dependency: Callable | None = None,
        scopes: list[str] | None = None,
        use_cache: bool = True,
    ) -> None:
        super().__init__(dependency=dependency, use_cache=use_cache)
        self.scopes = scopes or []


class SecurityScopes:
    """Aggregated OAuth 2.0 scopes for the current Security() chain.

    A handler / sub-dependency that declares a parameter of this type
    receives the union of all `Security(..., scopes=[...])` calls between
    the route entry and this point in the dependency graph. Typical use:
    an authorising dependency checks `security_scopes.scopes` against
    the scopes the bearer token actually carries and builds a
    `WWW-Authenticate: Bearer scope="<...>"` header when denying.

    Per RFC 6749 §3.3 the scope-string serialisation is space-separated.
    """

    __slots__ = ("scopes", "scope_str")

    def __init__(self, scopes: list[str] | None = None) -> None:
        self.scopes: list[str] = list(scopes) if scopes else []
        self.scope_str: str = " ".join(self.scopes)

    def __repr__(self) -> str:
        return f"SecurityScopes({self.scopes!r})"


def _coerce_value(value: Any, target_type: Any, param_name: str, loc: str) -> Any:
    """Coerce a string value to the target type."""
    if target_type is str or target_type is Any or target_type is None:
        return value
    try:
        if target_type is int:
            return int(value)
        if target_type is float:
            return float(value)
        if target_type is bool:
            if isinstance(value, str):
                return value.lower() in ("true", "1", "yes")
            return bool(value)
        # Enum (or any class with __members__).
        if hasattr(target_type, "__members__"):
            try:
                return target_type(value)
            except (ValueError, KeyError) as err:
                valid = list(target_type.__members__.keys())
                raise RequestValidationError(
                    [
                        {
                            "loc": [loc, param_name],
                            "msg": f"Invalid value for {param_name}: must be one of {valid}",
                            "type": "type_error",
                        }
                    ]
                ) from err
        return value
    except (ValueError, TypeError) as err:
        raise RequestValidationError(
            [
                {
                    "loc": [loc, param_name],
                    "msg": f"Invalid value for {param_name}: expected {getattr(target_type, '__name__', target_type)}",
                    "type": "type_error",
                }
            ]
        ) from err


_MARKER_LOC = {
    # Map MK_* → openapi-style location string. Resolved lazily to avoid import
    # ordering issues.
    0: "query",
    1: "path",
    2: "header",
    3: "cookie",
    4: "body",
    5: "form",
    6: "form",
}


class DependencyResolver:
    """Walks a `HandlerPlan` to produce kwargs for a handler.

    Per-request lifecycle: `_cache` is cleared on entry to `resolve_plan`.
    `_overrides` is set by the application (dependency
    overrides) and persists across requests.
    """

    __slots__ = (
        "_cache",
        "_overrides",
        "_override_subplans",
        "_teardowns",
        "_scope_stack",
    )

    def __init__(self) -> None:
        self._cache: dict[Callable, Any] = {}
        self._overrides: dict[Callable, Callable] = {}
        # Per-override sub-plan cache: build once on first call to an override.
        self._override_subplans: dict[Callable, Any] = {}
        # Per-request stack of yield-style dependency teardowns. Each entry is
        # `(kind, generator)` where kind is "sync" or "async". The stack is
        # drained in reverse by `run_teardowns()` after the response.
        self._teardowns: list[tuple[str, Any]] = []
        # Per-request accumulator of `Security(..., scopes=[...])` scopes.
        # Pushed before a Security sub-plan resolves, popped after. A
        # `SecurityScopes` parameter inside the chain reads this stack.
        self._scope_stack: list[str] = []

    async def resolve_plan(
        self,
        plan: Any,
        request: Request,
        path_params: dict[str, str],
        route_dep_plans: list[Any] | None = None,
    ) -> dict[str, Any]:
        """Fast path — consume a pre-built `HandlerPlan`."""
        self._cache.clear()
        self._teardowns.clear()
        self._scope_stack.clear()

        if route_dep_plans:
            for slot in route_dep_plans:
                await self._exec_depends(slot, request, path_params)

        return await self._resolve_slots(plan.slots, request, path_params)

    async def run_teardowns(self, exc: BaseException | None = None) -> None:
        """Run yield-dependency teardowns in reverse registration order.

        Each generator is advanced one step (or thrown into if `exc` is set)
        so the code after its `yield` executes. Exceptions inside teardown
        code are logged but do not interrupt the chain.
        """
        # Drain in reverse so the most recently set-up dependency tears down
        # first — matches Python's contextlib.ExitStack semantics.
        while self._teardowns:
            kind, gen = self._teardowns.pop()
            try:
                if kind == "sync":
                    if exc is not None:
                        # Teardown's own exception while handling the outer
                        # one: swallow to keep draining the stack.
                        with contextlib.suppress(StopIteration, Exception):
                            gen.throw(exc)
                    else:
                        with contextlib.suppress(StopIteration):
                            next(gen)
                else:  # async
                    if exc is not None:
                        with contextlib.suppress(StopAsyncIteration, Exception):
                            await gen.athrow(exc)
                    else:
                        with contextlib.suppress(StopAsyncIteration):
                            await gen.__anext__()
            except Exception:
                # Last-resort safety net — never crash the response cycle
                # because a teardown raised.
                pass

    async def resolve(
        self,
        handler: Callable,
        request: Request,
        path_params: dict[str, str],
        route_dependencies: list[Depends] | None = None,
    ) -> dict[str, Any]:
        """Back-compat path — build a plan on demand. Tests and direct
        callers that did not pre-plan land here.
        """
        from veloce._handler_plan import build_plan, build_route_dep_plans

        plan = build_plan(handler)
        rdp = build_route_dep_plans(route_dependencies) if route_dependencies else None
        return await self.resolve_plan(plan, request, path_params, rdp)

    async def _resolve_slots(
        self,
        slots: list[Any],
        request: Request,
        path_params: dict[str, str],
    ) -> dict[str, Any]:
        # Local imports of kind tags + types: import once per call rather than
        # per slot. These are module-level constants; the local binding is a
        # micro-opt that keeps the per-slot branch costs minimal.
        from veloce._handler_plan import (
            K_BG_TASKS,
            K_BODY_MODEL,
            K_DEPENDS,
            K_PARAM_MARKER,
            K_PATH,
            K_QUERY,
            K_QUERY_LIST,
            K_REQUEST,
            K_RESPONSE,
            K_SECURITY_SCOPES,
            K_UPLOAD_FILE,
        )
        from veloce.background import BackgroundTasks

        kwargs: dict[str, Any] = {}

        for slot in slots:
            kind = slot.kind
            name = slot.name

            if kind == K_REQUEST:
                kwargs[name] = request
                continue

            if kind == K_BG_TASKS:
                if request._background_tasks is None:
                    request._background_tasks = BackgroundTasks()
                kwargs[name] = request._background_tasks
                continue

            if kind == K_RESPONSE:
                # Response injection. One Response per
                # request, shared between the handler and any dependency
                # that also declares the parameter. `status_code = 0` is
                # the "not set by the handler" sentinel the dispatcher
                # checks before merging.
                from veloce.http.response import Response

                injected = request._state.get("_injected_response")
                if injected is None:
                    injected = Response()
                    injected.status_code = 0
                    request._state["_injected_response"] = injected
                kwargs[name] = injected
                continue

            if kind == K_SECURITY_SCOPES:
                # Snapshot the accumulated scopes — copy so later
                # mutations of `_scope_stack` don't affect this instance.
                kwargs[name] = SecurityScopes(list(self._scope_stack))
                continue

            if kind == K_DEPENDS:
                kwargs[name] = await self._exec_depends(slot, request, path_params)
                continue

            if kind == K_PARAM_MARKER:
                kwargs[name] = await self._resolve_marker(slot, request, path_params)
                continue

            if kind == K_BODY_MODEL:
                kwargs[name] = self._resolve_body_model(slot, request)
                continue

            if kind == K_UPLOAD_FILE:
                form = await request.form()
                upload = form.get(name)
                if upload is None and slot.is_optional:
                    kwargs[name] = None
                elif upload is not None:
                    kwargs[name] = upload
                # else: leave unset (handler default will apply if any)
                continue

            if kind == K_QUERY_LIST:
                # Lists may come from path or query; prefer path.
                if name in path_params:
                    raw = path_params[name]
                    kwargs[name] = [_coerce_value(raw, slot.list_inner, name, "path")]
                elif name in request.query_params:
                    # MultiDict.getall returns every value the URL carried
                    # for this key. `?tag=a&tag=b` → ["a", "b"].
                    values = request.query_params.getall(name)
                    kwargs[name] = [
                        _coerce_value(v, slot.list_inner, name, "query") for v in values
                    ]
                elif slot.has_default:
                    kwargs[name] = slot.default
                elif slot.is_optional:
                    kwargs[name] = None
                continue

            if kind == K_QUERY:
                # Path-or-query: a handler param that wasn't otherwise tagged.
                # Path bindings win if the route's matched params include this
                # name; else fall back to query string.
                if name in path_params:
                    kwargs[name] = _coerce_value(
                        path_params[name], slot.target_type or str, name, "path"
                    )
                elif name in request.query_params:
                    val = request.query_params[name]
                    kwargs[name] = _coerce_value(val, slot.target_type or str, name, "query")
                elif slot.has_default:
                    kwargs[name] = slot.default
                elif slot.is_optional:
                    kwargs[name] = None
                continue

            # K_PATH and K_NONE are not currently emitted by build_plan; future-proof.
            if kind == K_PATH:
                if name in path_params:
                    kwargs[name] = _coerce_value(
                        path_params[name], slot.target_type or str, name, "path"
                    )
                elif slot.has_default:
                    kwargs[name] = slot.default
                elif slot.is_optional:
                    kwargs[name] = None
                continue

        return kwargs

    def _resolve_body_model(self, slot: Any, request: Request) -> Any:
        try:
            body_data = request.json()
            return slot.model.model_validate(body_data)
        except PydanticValidationError as e:
            raise RequestValidationError(
                [
                    {
                        "loc": err["loc"],
                        "msg": err["msg"],
                        "type": err["type"],
                    }
                    for err in e.errors()
                ]
            ) from e
        except ValidationError:
            raise
        except Exception as err:
            raise RequestValidationError(
                [
                    {
                        "loc": ["body"],
                        "msg": "Invalid request body",
                        "type": "value_error",
                    }
                ]
            ) from err

    async def _resolve_marker(
        self, slot: Any, request: Request, path_params: dict[str, str]
    ) -> Any:
        marker = slot.marker
        mk = slot.marker_kind
        lookup = slot.lookup_name

        # List-typed query / header / cookie / form marker
        # (`tags: list[str] = Query(...)` / `Header(...)` / `Cookie(...)`
        # / `Form(...)`) — collect every repeated value, not just the
        # first.
        if mk in (0, 2, 3, 5) and get_origin(slot.target_type) in (list, set, tuple):
            inner_args = get_args(slot.target_type)
            inner = inner_args[0] if inner_args else str
            if mk == 5:  # MK_FORM
                values = (await request.form()).getlist(lookup)
            elif mk == 2:  # MK_HEADER
                values = request.headers.getlist(lookup.lower())
            elif mk == 3:  # MK_COOKIE
                values = request.cookies.getlist(lookup)
            else:  # MK_QUERY
                values = request.query_params.getlist(lookup)
            loc = _MARKER_LOC[mk]
            if not values:
                if marker.has_default:
                    return marker.default
                if slot.is_optional:
                    return None
                raise RequestValidationError(
                    [
                        {
                            "loc": [loc, slot.name],
                            "msg": f"Missing required parameter: {slot.name}",
                            "type": "value_error.missing",
                        }
                    ]
                )
            return [v if inner is str else _coerce_value(v, inner, slot.name, loc) for v in values]

        if mk == 1:  # MK_PATH
            raw = path_params.get(lookup)
        elif mk == 2:  # MK_HEADER
            raw = request.headers.get(lookup.lower())
        elif mk == 3:  # MK_COOKIE
            raw = request.cookies.get(lookup)
        elif mk == 4:  # MK_BODY
            body = request.json() if request.body else None
            # `Body(embed=True)` — the value lives under the param name
            # inside the JSON object, rather than being the whole body.
            if getattr(marker, "embed", False) and isinstance(body, dict):
                raw = body.get(lookup)
            else:
                raw = body
        elif mk in (5, 6):  # MK_FORM / MK_FILE
            form = await request.form()
            raw = form.get(lookup)
        else:  # MK_QUERY (0)
            raw = request.query_params.get(lookup)

        if raw is None:
            if marker.has_default:
                return marker.default
            if slot.is_optional:
                return None
            raise RequestValidationError(
                [
                    {
                        "loc": [_MARKER_LOC[mk], slot.name],
                        "msg": f"Missing required parameter: {slot.name}",
                        "type": "value_error.missing",
                    }
                ]
            )

        target = slot.target_type
        if target and target is not str and not isinstance(raw, (dict, list)):
            raw = _coerce_value(raw, target, slot.name, _MARKER_LOC[mk])

        try:
            return marker.validate(raw, slot.name)
        except ValueError as e:
            raise RequestValidationError(
                [
                    {
                        "loc": [_MARKER_LOC[mk], slot.name],
                        "msg": str(e),
                        "type": "value_error",
                    }
                ]
            ) from e

    async def _exec_depends(self, slot: Any, request: Request, path_params: dict[str, str]) -> Any:
        """Resolve a K_DEPENDS slot with caching and override support."""
        dep_callable = slot.dep_callable
        actual = self._overrides.get(dep_callable, dep_callable)

        if slot.use_cache and actual in self._cache:
            return self._cache[actual]

        if actual is dep_callable:
            sub_plan = slot.sub_plan
            is_coro = slot.dep_is_coro
            is_gen = slot.dep_is_gen
            is_async_gen = slot.dep_is_async_gen
        else:
            # Override: build (and cache) a sub-plan for the replacement.
            sub_plan = self._override_subplans.get(actual)
            if sub_plan is None:
                from veloce._handler_plan import build_plan

                sub_plan = build_plan(actual)
                self._override_subplans[actual] = sub_plan
            is_coro = inspect.iscoroutinefunction(actual)
            is_gen = inspect.isgeneratorfunction(actual)
            is_async_gen = inspect.isasyncgenfunction(actual)

        # Push this Security()'s scopes onto the stack so a `SecurityScopes`
        # slot inside the sub-plan sees them. Plain `Depends` has no scopes
        # so the push is a no-op. The stack is popped after the sub-plan
        # resolves regardless of success/failure.
        new_scopes: list[str] = slot.target_type if isinstance(slot.target_type, list) else []
        if new_scopes:
            self._scope_stack.extend(new_scopes)
        try:
            sub_kwargs = await self._resolve_slots(sub_plan.slots, request, path_params)
        finally:
            if new_scopes:
                del self._scope_stack[-len(new_scopes) :]

        if is_gen:
            # Sync generator: start it, capture the first yielded value as
            # the dependency result, and push the live generator onto the
            # teardown stack so `run_teardowns` advances past the yield.
            gen = actual(**sub_kwargs)
            try:
                result = next(gen)
            except StopIteration as err:
                raise RuntimeError(
                    f"yield dependency {actual!r} returned without yielding a value"
                ) from err
            self._teardowns.append(("sync", gen))
        elif is_async_gen:
            agen = actual(**sub_kwargs)
            try:
                result = await agen.__anext__()
            except StopAsyncIteration as err:
                raise RuntimeError(
                    f"yield dependency {actual!r} returned without yielding a value"
                ) from err
            self._teardowns.append(("async", agen))
        elif is_coro:
            result = await actual(**sub_kwargs)
        else:
            result = actual(**sub_kwargs)

        if slot.use_cache:
            self._cache[actual] = result
        return result
