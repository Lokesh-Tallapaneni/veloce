"""HandlerPlan -> MCP tool definition: input JSON Schema + argument binding.

The input schema is derived from the same `HandlerPlan` the HTTP dispatch
path uses, so a tool's declared inputs always match the handler signature.
Type -> JSON Schema conversion reuses `contrib.openapi._python_type_to_schema`
(and `_pydantic_to_schema` for body models) so MCP and OpenAPI never drift.

At call time `bind_arguments` walks the plan and produces the handler kwargs
from the JSON ``arguments`` mapping the client sent, resolving any `Depends`
graph through the shared `DependencyResolver` with an `MCPContext` in the
slot a `Request` would otherwise occupy - mirroring WebSocket DI, which
resolves a plan without an HTTP `Request`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import BaseModel
from pydantic import ValidationError as PydanticValidationError

from veloce._handler_plan import (
    K_BG_TASKS,
    K_BODY_MODEL,
    K_DEPENDS,
    K_PARAM_MARKER,
    K_QUERY,
    K_QUERY_LIST,
    K_REQUEST,
    K_RESPONSE,
    K_SECURITY_SCOPES,
    MK_BODY,
)
from veloce.background import BackgroundTasks
from veloce.contrib.mcp.context import MCPContext
from veloce.contrib.openapi import _pydantic_to_schema, _python_type_to_schema
from veloce.dependency import SecurityScopes, _coerce_value
from veloce.http.datastructures import Cookies, FormData, Headers, QueryParams
from veloce.http.request import Request
from veloce.http.response import Response

if TYPE_CHECKING:  # pragma: no cover
    from veloce._handler_plan import HandlerPlan, _Slot
    from veloce.dependency import DependencyResolver


# Slot kinds an MCP tool input cannot source from the JSON arguments (they
# belong to the HTTP request/response cycle or the DI graph). Skipped during
# schema build; bound to the MCPContext or resolved at call time.
_SKIP_INPUT_KINDS = frozenset({K_REQUEST, K_DEPENDS})

# Slot kinds an agent supplies as a JSON argument (the kinds `_slot_schema`
# turns into a declared input property). A required slot of one of these kinds
# that is absent from `arguments` is a binding error, not a handler error.
_INPUT_KINDS = frozenset({K_BODY_MODEL, K_QUERY_LIST, K_PARAM_MARKER, K_QUERY})


def _is_context_slot(slot: _Slot) -> bool:
    """Whether `slot` binds the MCPContext rather than an agent input.

    Detected purely by the parameter's TYPE annotation being `MCPContext` (the
    way `HandlerPlan` classifies every other typed slot), never by name. A
    parameter merely *named* `ctx` / `context` but typed as a normal value
    (`context: str`) stays an agent input. A parameter typed `MCPContext` lands
    on a K_QUERY slot - it is neither `Request` nor a model - so the K_QUERY
    guard scopes the check.
    """
    return slot.kind == K_QUERY and slot.target_type is MCPContext


# OpenAPI component ref prefix `_pydantic_to_schema` emits; an MCP input
# schema must be standalone, so refs are rewritten under this local prefix and
# the referenced defs inlined into the per-tool schema.
_OPENAPI_REF_PREFIX = "#/components/schemas/"
_MCP_REF_PREFIX = "#/$defs/"


def build_input_schema(plan: HandlerPlan, schemas_registry: dict[str, dict]) -> dict[str, Any]:
    """Build the MCP tool input JSON Schema from a handler plan.

    Each handler parameter an agent supplies becomes a property; a parameter
    with no default is required. `Depends`, the `MCPContext`, and
    `Request`/`Response` slots are not agent inputs and are omitted. Nested
    Pydantic body models are referenced by `$ref` and inlined under a `$defs`
    key, so each tool's input schema is standalone and resolvable by a client
    with no external component envelope.
    """
    properties: dict[str, Any] = {}
    required: list[str] = []

    for slot in plan.slots:
        if slot.kind in _SKIP_INPUT_KINDS or _is_context_slot(slot):
            continue
        prop_schema, is_required = _slot_schema(slot, schemas_registry)
        if prop_schema is None:
            continue
        properties[slot.name] = prop_schema
        if is_required:
            required.append(slot.name)

    schema: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required

    defs = _collect_defs(schema, schemas_registry)
    if defs:
        schema["$defs"] = defs
    return schema


def _collect_defs(schema: dict[str, Any], schemas_registry: dict[str, dict]) -> dict[str, dict]:
    """Inline every component this schema references into a `$defs` map.

    Walks the built schema for `#/components/schemas/<Name>` refs, pulls each
    referenced component out of `schemas_registry`, follows nested refs
    transitively, rewrites all refs to the local `#/$defs/<Name>` form (in
    place, including inside the collected defs), and returns the `$defs` map.
    """
    collected: dict[str, dict] = {}
    pending = _rewrite_refs(schema)
    while pending:
        name = pending.pop()
        if name in collected:
            continue
        component = schemas_registry.get(name)
        if component is None:
            continue
        component = _deepcopy_schema(component)
        collected[name] = component
        pending |= _rewrite_refs(component)
    return collected


def _rewrite_refs(node: Any) -> set[str]:
    """Rewrite OpenAPI refs to `$defs` refs in place; return referenced names.

    Pydantic stores a nested model's inner refs in their native `#/$defs/<Name>`
    form (only the top-level wrapper is rewritten to the OpenAPI prefix), and
    the referenced component lives in `schemas_registry` under its bare name.
    Such refs are already in the local MCP form, so they need no rewrite, but
    the name must still be collected so the component is inlined.
    """
    names: set[str] = set()
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str):
            if ref.startswith(_OPENAPI_REF_PREFIX):
                name = ref[len(_OPENAPI_REF_PREFIX) :]
                node["$ref"] = _MCP_REF_PREFIX + name
                names.add(name)
            elif ref.startswith(_MCP_REF_PREFIX):
                names.add(ref[len(_MCP_REF_PREFIX) :])
        for value in node.values():
            names |= _rewrite_refs(value)
    elif isinstance(node, list):
        for item in node:
            names |= _rewrite_refs(item)
    return names


def _deepcopy_schema(node: Any) -> Any:
    """Copy a JSON-Schema fragment so in-place ref rewriting never mutates the
    shared component registry."""
    if isinstance(node, dict):
        return {key: _deepcopy_schema(value) for key, value in node.items()}
    if isinstance(node, list):
        return [_deepcopy_schema(item) for item in node]
    return node


def _slot_schema(
    slot: _Slot, schemas_registry: dict[str, dict]
) -> tuple[dict[str, Any] | None, bool]:
    """Return `(schema, required)` for one parameter slot, or `(None, _)` to skip."""
    kind = slot.kind

    if kind == K_BODY_MODEL:
        model = slot.model
        if isinstance(model, type) and issubclass(model, BaseModel):
            return _pydantic_to_schema(model, schemas_registry), not slot.is_optional
        return {"type": "object"}, not slot.is_optional

    if kind == K_QUERY_LIST:
        item = _python_type_to_schema(slot.list_inner)
        return {"type": "array", "items": item}, not (slot.has_default or slot.is_optional)

    if kind == K_PARAM_MARKER:
        target = slot.target_type
        if (
            slot.marker_kind == MK_BODY
            and isinstance(target, type)
            and issubclass(target, BaseModel)
        ):
            prop = _pydantic_to_schema(target, schemas_registry)
        else:
            prop = _python_type_to_schema(target)
        marker = slot.marker
        if marker is not None and getattr(marker, "description", None):
            prop = {**prop, "description": marker.description}
        required = (marker is None or not marker.has_default) and not slot.is_optional
        return prop, required

    if kind == K_QUERY:
        return _python_type_to_schema(slot.target_type), not (slot.has_default or slot.is_optional)

    # Any other kind (background tasks, websocket, upload-file, security
    # scopes, response) is not an agent input.
    return None, False


def _coerce_argument(slot: _Slot, value: Any) -> Any:
    """Coerce one JSON argument value onto its handler parameter type."""
    kind = slot.kind

    if kind == K_BODY_MODEL:
        model = slot.model
        if isinstance(model, type) and issubclass(model, BaseModel):
            return _validate_model(value, model)
        return value

    if kind == K_PARAM_MARKER:
        target = slot.target_type
        if (
            slot.marker_kind == MK_BODY
            and isinstance(target, type)
            and issubclass(target, BaseModel)
        ):
            return _validate_model(value, target)
        marker = slot.marker
        if target and target is not str and not isinstance(value, (dict, list)):
            value = _coerce_value(value, target, slot.name, "body")
        if marker is not None:
            return marker.validate(value, slot.name)
        return value

    if kind == K_QUERY_LIST:
        inner = slot.list_inner
        if not isinstance(value, list):
            value = [value]
        return [_coerce_value(v, inner, slot.name, "body") for v in value]

    if kind == K_QUERY:
        target = slot.target_type
        if target and target is not str and not isinstance(value, (dict, list)):
            return _coerce_value(value, target, slot.name, "body")
        return value

    return value


def _validate_model(value: Any, model: type[BaseModel]) -> Any:
    """Validate `value` into a Pydantic model, re-raising as a clear error."""
    try:
        return model.model_validate(value)
    except PydanticValidationError as exc:
        raise ValueError(str(exc)) from exc


def _scalar_str(value: Any) -> str | None:
    """Render a JSON scalar as the string a request source would carry.

    Query / header / cookie / form values arrive as strings on the HTTP path,
    where `_coerce_value` then coerces them onto the parameter type. A tool
    argument may already be typed (`int`, `bool`, `float`), so it is rendered
    back to the wire string the same coercion expects: `True` -> `"true"`
    (lower-cased to satisfy the bool coercion's truthy set), numbers via `str`.
    A non-scalar (dict / list / None) is not a query-style value and is skipped
    so it cannot poison the source; such values are read from the JSON body.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return value
    return None


def _build_request(tool_name: str, arguments: dict[str, Any] | None = None) -> Request:
    """Construct the `Request` injected for a tool call's `Request` slots.

    A tool call has no HTTP request, but a handler or dependency may still read
    `request.headers` / `request.state` (built-in auth deps such as
    `HTTPBearer` / `APIKeyHeader` do). Binding the `MCPContext` there would
    raise `AttributeError`; instead a real `Request` is supplied with
    `request.state` as a fresh, usable store. The synthetic method/path mark the
    call's MCP origin.

    The tool `arguments` are seeded onto the request's value sources so a
    *sub-dependency* marker resolves from them exactly as a top-level tool
    parameter does: a scalar argument feeds `query_params` / `headers` /
    `cookies` / `form` (where a `Query` / `Header` / `Cookie` / `Form` marker
    reads it, by the same string then `_coerce_value` path the HTTP resolver
    uses), and the whole mapping feeds the JSON body (where a `Body` marker or
    a body model reads it). Top-level slots are bound from `arguments` directly
    by `bind_arguments`; this seeding is what lets the *same* argument satisfy a
    `Query(...)` / `Body(...)` parameter declared inside a `Depends` sub-plan.
    """
    request = Request(
        method="MCP",
        path=f"/mcp/{tool_name}",
        query_string="",
        headers=[],
        body=b"",
    )
    if not arguments:
        return request

    # Body markers / models read `request.json()`; the whole argument mapping is
    # the synthetic body so a sub-dependency `item: Item = Body(...)` validates
    # against it (and `Body(embed=True)` finds its key inside the dict).
    request._json = arguments

    # Scalar arguments feed the string-keyed value sources a Query / Header /
    # Cookie / Form marker reads. Headers are case-insensitive and an un-aliased
    # `Header` param looks up its hyphenated name, so the argument name is
    # registered under both its raw and hyphenated forms.
    scalars: list[tuple[str, str]] = []
    for name, value in arguments.items():
        text = _scalar_str(value)
        if text is not None:
            scalars.append((name, text))
    request._query_params = QueryParams(scalars)
    request._form = FormData(scalars)
    request._cookies = Cookies(scalars)
    header_items: list[tuple[str, str]] = []
    for name, text in scalars:
        header_items.append((name, text))
        hyphenated = name.replace("_", "-")
        if hyphenated != name:
            header_items.append((hyphenated, text))
    request.headers = Headers(header_items)
    return request


def _injected_response(request: Request) -> Response:
    """Return the request-scoped injected `Response`, creating it on first use.

    Mirrors the HTTP resolver's `K_RESPONSE` slot: one `Response` per request,
    stored on `request._state["_injected_response"]` and shared by the handler
    and any dependency that also injects `Response`. `status_code = 0` is the
    "handler never set it" sentinel `_build_response` checks before merging the
    injected status onto the final response.
    """
    injected = request._state.get("_injected_response")
    if injected is None:
        injected = Response()
        injected.status_code = 0
        request._state["_injected_response"] = injected
    return injected


def _background_tasks(request: Request) -> BackgroundTasks:
    """Return the request-scoped `BackgroundTasks` queue, creating it once.

    Mirrors the HTTP resolver's `K_BG_TASKS` slot: a single queue per request,
    reused for every `BackgroundTasks` injection point so work scheduled by a
    dependency is not discarded when the handler's own `tasks` parameter binds.
    """
    tasks = request._background_tasks
    if tasks is None:
        tasks = BackgroundTasks()
        request._background_tasks = tasks
    return tasks


async def bind_arguments(
    plan: HandlerPlan,
    arguments: dict[str, Any],
    context: MCPContext,
    resolver: DependencyResolver,
    route_dep_plans: list[Any] | None = None,
    request: Request | None = None,
) -> tuple[dict[str, Any], Request]:
    """Resolve handler kwargs for a tool call from the JSON `arguments`.

    Returns `(kwargs, request)`; the returned `Request` carries any
    `BackgroundTasks` the handler scheduled (on `request._background_tasks`),
    which the caller runs after the handler, mirroring the HTTP path.

    Scalar / model parameters are read from `arguments` and coerced through
    Pydantic; `Depends` graphs resolve through `resolver` against a minimal
    `Request` (so a dependency that reads `request.headers` / `request.state`
    works); a parameter typed `Request` receives that same request, and one
    typed `MCPContext` receives `context`. Framework slots a handler may
    declare - `Response`, `BackgroundTasks`, `SecurityScopes` - are injected
    just as the HTTP path injects them. Run `resolver.run_teardowns()` after the
    handler returns to drain any `yield`-style dependency teardowns.

    Route-level dependencies (`route_dep_plans`) run first, before any handler
    slot is bound, mirroring `resolve_plan` / `resolve_ws_plan`; a guard that
    raises here aborts the call before the handler sees an argument.
    """
    resolver.reset()

    # Expose the MCPContext to the resolver so a sub-dependency that declares a
    # parameter typed `MCPContext` receives it. The top-level handler's context
    # slot is bound below directly; this covers the same type appearing inside
    # any `Depends` sub-plan, which the resolver binds by declared type.
    resolver._mcp_context = context

    # A real, minimal Request stands in for the HTTP request the resolver and
    # handler expect (mirroring WS DI, which passes a WebSocket). The JSON
    # `arguments` map is handed to the resolver where the HTTP path hands
    # `path_params`: a sub-dependency that declares a scalar parameter named
    # like a tool argument then sources its value from `arguments`, with the
    # same string coercion the HTTP path applies to a path parameter. Tools have
    # no URL path, so this is the only place an agent-supplied value can enter
    # the DI graph. Built once per call and reused for every Request slot. A
    # caller that already bound this request onto the request context (the
    # route-derived path, which runs `before_request` first) passes it in.
    if request is None:
        request = _build_request(context.tool_name, arguments)

    # Route-level dependencies run before the handler graph (RFC-equivalent to
    # the HTTP/WS paths), so an auth/permission guard fires even though the
    # call arrived over MCP rather than HTTP.
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

        # A handler may declare `response: Response`; supply the one
        # request-scoped injected Response the HTTP resolver stores on
        # `request._state["_injected_response"]`, creating it on first use.
        # Reusing it (rather than handing out a fresh Response) means a
        # dependency and the handler that both inject `Response` mutate the
        # same object, and the route path's `_build_response` merges its
        # status / headers onto the tool result - exactly as on the HTTP path.
        if kind == K_RESPONSE:
            kwargs[name] = _injected_response(request)
            continue

        # A handler may declare `tasks: BackgroundTasks`; reuse the single
        # request-scoped queue (created lazily on first injection point) so a
        # dependency that injected `BackgroundTasks` and scheduled work shares
        # it with the handler's own `tasks` parameter - HTTP keeps one queue per
        # request regardless of how many slots ask for it. The caller runs it
        # after the handler returns, mirroring deferred HTTP execution.
        if kind == K_BG_TASKS:
            kwargs[name] = _background_tasks(request)
            continue

        if kind == K_SECURITY_SCOPES:
            # A handler may declare `scopes: SecurityScopes` directly, as on the
            # HTTP/DI path. An MCP tool call has no enclosing Security() chain,
            # so the correct value is an empty SecurityScopes - the same value a
            # route with no scopes receives.
            kwargs[name] = SecurityScopes(list(resolver._scope_stack))
            continue

        if kind == K_DEPENDS:
            kwargs[name] = await resolver._exec_depends(slot, request, arguments)
            continue

        if name in arguments:
            kwargs[name] = _coerce_argument(slot, arguments[name])
        elif slot.has_default:
            kwargs[name] = slot.default
        elif slot.is_optional:
            kwargs[name] = None
        elif kind in _INPUT_KINDS:
            # A required agent input is absent. Raise at the binding boundary
            # (not by leaving the kwarg unset for the handler to trip over) so
            # the server maps it to an invalid-params error the agent can
            # correct, never confusing it with a handler-body failure.
            raise TypeError(f"missing required argument: {name!r}")

    return kwargs, request
