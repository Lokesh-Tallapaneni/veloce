"""Argument binding — a tool call's arguments resolved into handler arguments."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from veloce._handler_plan import (
    K_BG_TASKS,
    K_BODY_MODEL,
    K_DEPENDS,
    K_MODEL_GROUP,
    K_REQUEST,
    K_RESPONSE,
    K_SECURITY_SCOPES,
)
from veloce._internal import _current_request_var
from veloce.contrib.mcp.context import MCPContext
from veloce.dependency import (
    DependencyResolver,
    SecurityScopes,
)
from veloce.http.datastructures import FormData, QueryParams
from veloce.http.request import Request

if TYPE_CHECKING:  # pragma: no cover
    from veloce._handler_plan import HandlerPlan
from veloce.contrib.mcp.plan_bridge._coercion import (
    _bind_model_group,
    _coerce_argument,
    _scalar_str,
)
from veloce.contrib.mcp.plan_bridge._slots import _INPUT_KINDS, _is_context_slot


def _carrier_scope() -> dict[str, Any] | None:
    """Build an ASGI-shaped scope carrying the peer, or `None` off-transport.

    A replayed request has no socket of its own, so without this everything
    keyed on caller identity - rate limiting above all - sees an unknown peer.
    The carrier's *resolved* `client_host` propagates, so a `ProxyFix`
    correction carries over. Only the peer crosses: copying headers would let a
    credential presented to the transport be re-read by a `Security` scheme.
    """
    carrier = _current_request_var.get()
    if carrier is None:
        return None
    host = carrier.client_host
    return {"client": (host, 0)} if host else None


def _build_request(
    tool_name: str,
    arguments: dict[str, Any] | None = None,
    *,
    method: str | None = None,
    path: str | None = None,
) -> Request:
    """Construct the `Request` injected for a tool call's `Request` slots.

    A handler or dependency may still read `request.headers` / `request.state`,
    so a real `Request` is supplied rather than the `MCPContext`. A route-backed
    tool carries the wrapped route's real `method` and `path`; a pure
    `@app.mcp_tool` gets a synthetic `"MCP"` method and `/mcp/<tool>` path.

    The tool `arguments` are seeded onto the request's value sources, which lets
    the same argument satisfy a `Query(...)` / `Body(...)` parameter declared
    inside a `Depends` sub-plan.
    """
    request = Request(
        method=method if method is not None else "MCP",
        path=path if path is not None else f"/mcp/{tool_name}",
        query_string="",
        headers=[],
        body=b"",
        scope=_carrier_scope(),
    )
    if not arguments:
        return request

    # The whole argument mapping is the synthetic body `request.json()` returns.
    request._json = arguments

    # Scalar arguments feed the query / form sources a `Query` / `Form` marker reads.
    # Headers and cookies are deliberately NOT seeded: a `Security` scheme
    # (`HTTPBearer`, `APIKeyHeader`) would then read a tool argument as a credential.
    scalars: list[tuple[str, str]] = []
    for name, value in arguments.items():
        text = _scalar_str(value)
        if text is not None:
            scalars.append((name, text))
    request._query_params = QueryParams(scalars)
    request._form = FormData(scalars)
    return request


# One `Response` and one `BackgroundTasks` queue per request, as on HTTP.
_injected_response = DependencyResolver._bind_injected_response
_background_tasks = DependencyResolver._bind_background_tasks


async def bind_arguments(
    plan: HandlerPlan,
    arguments: dict[str, Any],
    context: MCPContext,
    resolver: DependencyResolver,
    route_dep_plans: list[Any] | None = None,
    request: Request | None = None,
    route_defaults: dict[str, Any] | None = None,
    dependency_inputs: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], Request]:
    """Resolve handler kwargs for a tool call from the JSON `arguments`.

    Returns `(kwargs, request)`; the returned `Request` carries any
    `BackgroundTasks` the handler scheduled, which the caller runs afterwards.

    Scalar / model parameters are read from `arguments` and coerced through
    Pydantic; `Depends` graphs resolve through `resolver` against a minimal
    `Request`; a `Request` slot receives that same request and an `MCPContext`
    slot receives `context`. `Response`, `BackgroundTasks` and `SecurityScopes`
    are injected as the HTTP path injects them. Run `resolver.run_teardowns()`
    afterwards to drain `yield`-style teardowns.

    Route-level dependencies (`route_dep_plans`) run before any handler slot is
    bound, so a guard that raises aborts the call before the handler sees an
    argument. `route_defaults` are overlaid under the explicit `arguments`
    (explicit argument > route default > Python default).

    `dependency_inputs` names the published inputs declared inside a `Depends`
    sub-dependency; they are coerced here by the same strict rule the top-level
    slots use, so moving a parameter behind a dependency changes nothing.
    """
    resolver.reset()

    # An explicit argument always wins; a fresh dict only when a default adds a key.
    if route_defaults:
        merged = {k: v for k, v in route_defaults.items() if k not in arguments}
        if merged:
            merged.update(arguments)
            arguments = merged

    # Held to the published type before a resolver reading query-string rules sees them.
    if dependency_inputs:
        coerced: dict[str, Any] | None = None
        for name, slot in dependency_inputs.items():
            if name not in arguments:
                continue
            value = _coerce_argument(slot, arguments[name])
            if value is not arguments[name]:
                if coerced is None:
                    coerced = dict(arguments)
                coerced[name] = value
        if coerced is not None:
            arguments = coerced

    # This covers the same type inside any `Depends` sub-plan, bound by declared type.
    resolver._mcp_context = context

    # A minimal Request stands in for the one the resolver expects; `arguments`
    # goes where HTTP hands `path_params`, the only entry into the DI graph.
    if request is None:
        request = _build_request(context.tool_name, arguments)

    # Route-level dependencies run first, so an auth guard fires over MCP too.
    if route_dep_plans:
        for slot in route_dep_plans:
            await resolver._exec_depends(slot, request, arguments)

    kwargs: dict[str, Any] = {}

    for slot in plan.slots:
        kind = slot.kind
        name = slot.name

        if kind == K_REQUEST:
            kwargs[name] = request
            continue

        if _is_context_slot(slot):
            kwargs[name] = context
            continue

        # Reused, so a dependency and the handler mutate the same `Response`.
        if kind == K_RESPONSE:
            kwargs[name] = _injected_response(request)
            continue

        # One request-scoped queue however many slots ask for it, as on HTTP.
        if kind == K_BG_TASKS:
            kwargs[name] = _background_tasks(request)
            continue

        if kind == K_SECURITY_SCOPES:
            # No enclosing `Security()` chain, so the empty `SecurityScopes`.
            kwargs[name] = SecurityScopes(list(resolver._scope_stack))
            continue

        if kind == K_DEPENDS:
            kwargs[name] = await resolver._exec_depends(slot, request, arguments)
            continue

        if kind == K_MODEL_GROUP:
            kwargs[name] = _bind_model_group(slot, arguments)
        elif name in arguments:
            kwargs[name] = _coerce_argument(slot, arguments[name])
        elif (marker := slot.marker) is not None and marker.has_default:
            # The slot's `default` holds the hoisted marker, so the marker is the
            # only source of the value; `resolve_default` runs a factory too.
            kwargs[name] = marker.resolve_default()
        elif slot.has_default:
            kwargs[name] = slot.default
        elif slot.is_optional and kind != K_BODY_MODEL:
            # A nullable body still demands a JSON document; only a default
            # makes the parameter itself omittable (`slot.has_default` above).
            kwargs[name] = None
        elif kind in _INPUT_KINDS:
            # At the binding boundary, so the server maps it to invalid-params.
            raise TypeError(f"missing required argument: {name!r}")

    return kwargs, request
