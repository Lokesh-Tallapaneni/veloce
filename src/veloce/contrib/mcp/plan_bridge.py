"""Plan bridge — HandlerPlan to MCP tool definition (input schema + binding).

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
from veloce._model_backend import is_pydantic_model
from veloce._route_contract import describe_slot
from veloce.background import BackgroundTasks
from veloce.contrib.mcp.context import MCPContext
from veloce.contrib.openapi import _pydantic_to_schema, _python_type_to_schema
from veloce.dependency import SecurityScopes, _coerce_scalar, _coerce_value
from veloce.http.datastructures import FormData, QueryParams
from veloce.http.request import Request
from veloce.http.response import Response

if TYPE_CHECKING:  # pragma: no cover
    from veloce._handler_plan import HandlerPlan, _Slot
    from veloce.dependency import DependencyResolver


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

# JSON Schema dialect MCP tool input/output schemas default to. The spec assumes
# Draft 2020-12 when a schema omits `$schema`; declaring it explicitly on the
# emitted top-level schema removes that ambiguity for a strict validator.
JSON_SCHEMA_DIALECT = "https://json-schema.org/draft/2020-12/schema"


def build_input_schema(
    plan: HandlerPlan, schemas_registry: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    """Build the MCP tool input JSON Schema from a handler plan.

    Each handler parameter an agent supplies becomes a property; a parameter
    with no default is required. `Depends`, the `MCPContext`, and
    `Request`/`Response` slots are not agent inputs and are omitted. Nested
    Pydantic body models are referenced by `$ref` and inlined under a `$defs`
    key, so each tool's input schema is standalone and resolvable by a client
    with no external component envelope.

    A client-supplied parameter declared *inside* a `Depends` sub-dependency
    (e.g. `user_id: int = Query(...)` or a `Body` model on a dependency) is
    sourced from the same JSON `arguments` mapping `bind_arguments` seeds onto
    the synthetic request, so it must be advertised as a tool input too -
    otherwise `tools/list` would declare no required inputs while `tools/call`
    rejects the call. The `Depends` graph is walked recursively and every such
    input is merged in by name; the `Depends` slots themselves (and other
    inject-only slots) are never inputs.
    """
    properties: dict[str, Any] = {}
    required: list[str] = []

    _collect_input_slots(plan.slots, properties, required, schemas_registry, set())

    schema: dict[str, Any] = {
        "$schema": JSON_SCHEMA_DIALECT,
        "type": "object",
        "properties": properties,
    }
    if required:
        schema["required"] = required

    defs = _collect_defs(schema, schemas_registry)
    if defs:
        schema["$defs"] = defs
    return schema


def build_output_schema(
    model: type[BaseModel],
    schemas_registry: dict[str, dict[str, Any]],
    by_alias: bool = True,
) -> dict[str, Any] | None:
    """Build a standalone MCP output JSON Schema from a Pydantic model.

    Mirrors `build_input_schema`: the model renders through the shared OpenAPI
    converter and every referenced component is inlined under `$defs`, so the
    schema resolves with no external envelope. The model's own object schema is
    returned at the top level (an MCP `outputSchema` describes the structured
    tool result, which is always a JSON object). Returns `None` if the model's
    schema could not be produced.

    Rendered in serialization mode so the advertised schema matches what
    `model_dump(mode="json")` emits: a computed field or serialization alias
    that surfaces in the structured result is documented here too. `by_alias`
    matches the schema's property keys to how the structured value will be
    dumped, so the emitted `structuredContent` conforms to the advertised schema.
    """
    ref = _pydantic_to_schema(model, schemas_registry, mode="serialization", by_alias=by_alias)
    name = ref["$ref"][len(_OPENAPI_REF_PREFIX) :]
    base = schemas_registry.get(name)
    if base is None:
        return None
    schema = _deepcopy_schema(base)
    defs = _collect_defs(schema, schemas_registry)
    if defs:
        schema["$defs"] = defs
    # Declare the dialect on the emitted top-level schema (the model's own object
    # schema), so a strict client validates `structuredContent` under 2020-12.
    schema["$schema"] = JSON_SCHEMA_DIALECT
    return schema


def _collect_input_slots(
    slots: list[_Slot],
    properties: dict[str, Any],
    required: list[str],
    schemas_registry: dict[str, dict[str, Any]],
    seen_plans: set[int],
) -> None:
    """Accumulate client-supplied input properties from a slot list.

    Recurses into every `Depends` sub-plan so a `Query`/`Body`/`Header`/
    `Cookie`/`Form` parameter (or body model) declared on a sub-dependency is
    advertised exactly as a top-level input of that kind. Merge is by name: the
    first declaration of a name wins (a later identical-name slot is the same
    cached dependency consuming the same argument, so re-adding it would be
    redundant). Inject-only slots (`Request`/`Response`/`BackgroundTasks`/
    `SecurityScopes`/`MCPContext`/`Depends`) contribute no input of their own.
    Cycle-guarded via `seen_plans` (the plan builder forbids `Depends` cycles,
    but a diamond-shaped graph can still reach one sub-plan twice).
    """
    for slot in slots:
        if slot.kind == K_DEPENDS:
            sub_plan = slot.sub_plan
            if sub_plan is None:
                continue
            plan_id = id(sub_plan)
            if plan_id in seen_plans:
                continue
            seen_plans.add(plan_id)
            _collect_input_slots(sub_plan.slots, properties, required, schemas_registry, seen_plans)
            continue

        if slot.kind == K_REQUEST or _is_context_slot(slot):
            continue

        # A name already declared (by an earlier sibling or sub-dependency)
        # is the same client-supplied value; declare it once.
        if slot.name in properties:
            continue

        prop_schema, is_required = _slot_schema(slot, schemas_registry)
        if prop_schema is None:
            continue
        properties[slot.name] = prop_schema
        if is_required:
            required.append(slot.name)


def _collect_defs(
    schema: dict[str, Any], schemas_registry: dict[str, dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    """Inline every component this schema references into a `$defs` map.

    Walks the built schema for `#/components/schemas/<Name>` refs, pulls each
    referenced component out of `schemas_registry`, follows nested refs
    transitively, rewrites all refs to the local `#/$defs/<Name>` form (in
    place, including inside the collected defs), and returns the `$defs` map.
    """
    collected: dict[str, dict[str, Any]] = {}
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
    slot: _Slot, schemas_registry: dict[str, dict[str, Any]]
) -> tuple[dict[str, Any] | None, bool]:
    """Return `(schema, required)` for one parameter slot, or `(None, _)` to skip.

    Classification is shared with the OpenAPI and dispatch lowerings via
    `describe_slot`, so a tool's declared inputs track the same plan; only the
    JSON-Schema construction is MCP-local. A file upload is not expressible as a
    JSON tool argument, so a bare `UploadFile` slot is skipped.
    """
    d = describe_slot(slot, ())
    if d is None:
        return None, False
    if d.is_file and d.marker is None:
        return None, False

    if d.model is not None:
        if is_pydantic_model(d.model):
            prop = _pydantic_to_schema(d.model, schemas_registry)
        else:
            prop = {"type": "object"}
    elif d.is_list:
        prop = {"type": "array", "items": _python_type_to_schema(d.target_type)}
    else:
        prop = _python_type_to_schema(d.target_type)

    if d.marker is not None and getattr(d.marker, "description", None):
        prop = {**prop, "description": d.marker.description}

    return prop, not (d.has_default or d.is_optional)


def _coerce_argument(slot: _Slot, value: Any) -> Any:
    """Coerce one JSON argument value onto its handler parameter type."""
    kind = slot.kind

    if kind == K_BODY_MODEL:
        model = slot.model
        if is_pydantic_model(model):
            return _validate_model(value, model)
        return value

    if kind == K_PARAM_MARKER:
        target = slot.target_type
        if slot.marker_kind == MK_BODY and is_pydantic_model(target):
            return _validate_model(value, target)
        marker = slot.marker
        value = _coerce_scalar(value, target, slot.name, "body")
        if marker is not None:
            return marker.validate(value, slot.name)
        return value

    if kind == K_QUERY_LIST:
        inner = slot.list_inner
        if not isinstance(value, list):
            value = [value]
        return [_coerce_value(v, inner, slot.name, "body") for v in value]

    if kind == K_QUERY:
        return _coerce_scalar(value, slot.target_type, slot.name, "body")

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


def _build_request(
    tool_name: str,
    arguments: dict[str, Any] | None = None,
    *,
    method: str | None = None,
    path: str | None = None,
) -> Request:
    """Construct the `Request` injected for a tool call's `Request` slots.

    A tool call has no HTTP request, but a handler or dependency may still read
    `request.headers` / `request.state` (built-in auth deps such as
    `HTTPBearer` / `APIKeyHeader` do). Binding the `MCPContext` there would
    raise `AttributeError`; instead a real `Request` is supplied with
    `request.state` as a fresh, usable store.

    For a route-backed tool the caller passes the wrapped route's real HTTP
    `method` and its rule `path` (the `path_template` pattern), so a handler /
    dependency / `before_request` hook that branches on `request.method` or
    `request.path` sees the route's actual values rather than a synthetic MCP
    marker - `request.path_params` stays the authoritative source of the
    concrete parameter values. For a pure `@app.mcp_tool` (no route) there is
    no wrapped route, so a synthetic `"MCP"` method and `/mcp/<tool>` path mark
    the call's MCP origin.

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
        method=method if method is not None else "MCP",
        path=path if path is not None else f"/mcp/{tool_name}",
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

    # Scalar arguments feed the query / form value sources a `Query` / `Form`
    # marker reads (and the whole mapping feeds the JSON body above). Headers and
    # cookies are deliberately NOT seeded from tool arguments: an agent has no HTTP
    # headers or cookies, so letting a tool argument populate them would let it
    # masquerade as transport-authenticated input - a `Security` scheme
    # (`HTTPBearer`, `APIKeyHeader`, `APIKeyCookie`) would then read attacker
    # controlled data as a credential. Over MCP, authentication is the validated
    # principal, never a request header/cookie, so these stay empty.
    #
    # Query and form ARE seeded by necessity - they are the legitimate
    # `Query(...)` / `Form(...)` tool-input channels - so a query/form-based
    # `Security` scheme (e.g. `APIKeyQuery`) would still read agent-supplied input.
    # That is inherent to those being inputs; the `Principal` / `mcp_scopes` model
    # is the intended authorization gate over MCP, not request-derived schemes.
    scalars: list[tuple[str, str]] = []
    for name, value in arguments.items():
        text = _scalar_str(value)
        if text is not None:
            scalars.append((name, text))
    request._query_params = QueryParams(scalars)
    request._form = FormData(scalars)
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
    route_defaults: dict[str, Any] | None = None,
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

    `route_defaults` are the route's rule `defaults=` values. The HTTP path
    merges them into `path_params` (without overriding URL-matched values), so a
    handler / dependency parameter named by a default - including a non-URL key
    such as `defaults={"mode": "summary"}` - resolves from it. The MCP path has
    no URL, so the defaults are overlaid *under* the explicit `arguments`
    (explicit argument > route default > Python default) and the merged mapping
    feeds both handler-kwarg binding and the DI graph, matching HTTP precedence.
    """
    resolver.reset()

    # Overlay route defaults under the explicit arguments so a handler /
    # dependency parameter named by a default resolves from it, while an
    # explicit argument always wins. A fresh dict is built only when defaults
    # actually add a key, so the common (no-default) call keeps the caller's
    # mapping untouched and pays nothing.
    if route_defaults:
        merged = {k: v for k, v in route_defaults.items() if k not in arguments}
        if merged:
            merged.update(arguments)
            arguments = merged

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
