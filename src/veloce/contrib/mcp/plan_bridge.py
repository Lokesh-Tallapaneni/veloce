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

from typing import TYPE_CHECKING, Any, Literal, get_origin

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
    _unwrap_optional,
)
from veloce._model_backend import (
    ModelBackend,
    adapter_for,
    is_adaptable_model,
    is_pydantic_model,
)
from veloce._route_contract import describe_slot
from veloce.contrib.mcp.context import MCPContext
from veloce.contrib.openapi import (
    _adapted_to_schema,
    _pydantic_to_schema,
    _python_type_to_schema,
)
from veloce.dependency import DependencyResolver, SecurityScopes, _coerce_value
from veloce.exceptions import RequestValidationError
from veloce.http.datastructures import FormData, QueryParams
from veloce.http.request import Request
from veloce.routing.converters import path_param_schemas

if TYPE_CHECKING:  # pragma: no cover
    from veloce._handler_plan import HandlerPlan, _Slot


# Slot kinds an agent supplies as a JSON argument (the kinds `_slot_schema`
# turns into a declared input property). A required slot of one of these kinds
# that is absent from `arguments` is a binding error, not a handler error.
_INPUT_KINDS = frozenset({K_BODY_MODEL, K_QUERY_LIST, K_PARAM_MARKER, K_QUERY})

# Schema keywords that annotate a property rather than constrain its type, so
# they stay outside the `anyOf` when a null branch is added.
_ANNOTATION_KEYWORDS = frozenset({"description", "title", "default"})


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
    plan: HandlerPlan,
    schemas_registry: dict[str, dict[str, Any]],
    path_template: str | None = None,
    *,
    dependency_inputs: dict[str, _Slot] | None = None,
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

    `dependency_inputs`, when given, is filled with the published inputs that
    are declared *inside* a `Depends` sub-dependency, keyed by name. Those are
    the ones `bind_arguments` does not coerce itself - it seeds them onto the
    synthetic request for the HTTP resolver to read - so the caller needs them
    to hold the call to the same declared types this schema advertises.
    Collected here rather than by a second walk, because this is already the one
    traversal that decides what a tool publishes.

    `path_template` is the backing route's path, for a tool exposed from one. Its
    parameters are part of the call's contract whether or not a signature names
    them - a dependency reading `request.path_params` consumes the same value -
    so a placeholder no slot declares is advertised from the route itself. Left
    out, an agent reading the schema would see no way to supply a value the route
    requires, and would call the tool with that value missing.
    """
    properties: dict[str, Any] = {}
    required: list[str] = []

    _collect_input_slots(
        plan.slots, properties, required, schemas_registry, set(), input_slots=dependency_inputs
    )
    if path_template:
        for name, param_schema in path_param_schemas(path_template).items():
            if name not in properties:
                properties[name] = param_schema
                required.append(name)

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
    model: Any,
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
    if is_adaptable_model(model):
        ref = _adapted_to_schema(model, schemas_registry)
    else:
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


def _spread_model_fields(
    model: Any,
    properties: dict[str, Any],
    required: list[str],
    schemas_registry: dict[str, dict[str, Any]],
) -> None:
    """Declare a model's fields as top-level inputs, by alias where one is set."""
    ref = _pydantic_to_schema(model, schemas_registry)
    resolved = _collect_defs({"__probe__": ref}, schemas_registry)
    body = resolved.get(model.__name__, {})
    field_required = set(body.get("required", ()))
    for name, field_schema in (body.get("properties") or {}).items():
        if name in properties:
            continue
        properties[name] = field_schema
        if name in field_required and name not in required:
            required.append(name)


def _collect_input_slots(
    slots: list[_Slot],
    properties: dict[str, Any],
    required: list[str],
    schemas_registry: dict[str, dict[str, Any]],
    seen_plans: set[int],
    in_depends: bool = False,
    *,
    input_slots: dict[str, _Slot] | None = None,
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
            _collect_input_slots(
                sub_plan.slots,
                properties,
                required,
                schemas_registry,
                seen_plans,
                True,
                input_slots=input_slots,
            )
            continue

        if slot.kind == K_REQUEST or _is_context_slot(slot):
            continue

        # A body model on a sub-dependency validates against the whole argument
        # mapping, not against `arguments[name]` the way a top-level one does, so
        # its fields are the tool's inputs. Declaring the parameter name instead
        # would publish a shape the call path rejects.
        if in_depends and slot.kind == K_BODY_MODEL and is_pydantic_model(slot.model):
            _spread_model_fields(slot.model, properties, required, schemas_registry)
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
        if in_depends and input_slots is not None:
            input_slots[slot.name] = slot


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


def _item_schema(inner: Any, schemas_registry: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Return the schema for one element of a list parameter.

    `_python_type_to_schema` describes a value that arrived as text, so it calls
    a model `{"type": "string"}` - right for `?tag={"name":"x"}` over HTTP, wrong
    here: an MCP argument is JSON, and the binder builds the model from a real
    object. Publishing the string form would have a client send the one shape the
    schema describes and the handler least expects.
    """
    if is_pydantic_model(inner):
        return _pydantic_to_schema(inner, schemas_registry)
    if inner is not None and is_adaptable_model(inner):
        return _adapted_to_schema(inner, schemas_registry)
    return _python_type_to_schema(inner)


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
        elif slot.backend == ModelBackend.ADAPTED:
            prop = _adapted_to_schema(d.model, schemas_registry)
        else:
            prop = {"type": "object"}
    elif d.is_list:
        prop = {"type": "array", "items": _item_schema(d.target_type, schemas_registry)}
    else:
        prop = _python_type_to_schema(d.target_type)

    if d.marker is not None:
        if getattr(d.marker, "description", None):
            prop = {**prop, "description": d.marker.description}
        if getattr(d.marker, "title", None):
            prop = {**prop, "title": d.marker.title}
        # Advertise the declared default so a schema-aware client can populate
        # the field itself instead of omitting it and relying on the server's
        # fallback. A `default_factory` builds a per-call value with no single
        # value to publish, so only a static default is emitted.
        marker_default = getattr(d.marker, "default", ...)
        if marker_default is not ... and getattr(d.marker, "default_factory", None) is None:
            prop = {**prop, "default": marker_default}
    elif d.has_default and d.default is not None:
        # A bare Python default is as much a part of the contract as a marker's,
        # and a client that can read it can populate the field itself instead of
        # omitting it. `None` is skipped: it is carried by the null branch below,
        # where it describes an optional field rather than a value worth sending.
        prop = {**prop, "default": d.default}

    # An optional parameter accepts an explicit null as well as omission. Without
    # the null branch the published type rejects a value the call path takes.
    if d.is_optional and d.model is None:
        prop = _with_null_branch(prop)

    return prop, not (d.has_default or d.is_optional)


def _with_null_branch(prop: dict[str, Any]) -> dict[str, Any]:
    """Widen a property schema to also accept an explicit null."""
    if "anyOf" in prop:
        if {"type": "null"} in prop["anyOf"]:
            return prop
        return {**prop, "anyOf": [*prop["anyOf"], {"type": "null"}]}
    keywords = {k: v for k, v in prop.items() if k not in _ANNOTATION_KEYWORDS}
    carried = {k: v for k, v in prop.items() if k in _ANNOTATION_KEYWORDS}
    if not keywords:
        return prop
    return {**carried, "anyOf": [keywords, {"type": "null"}]}


# What a JSON value of each Python type is called in an error a model reads. The
# names are JSON's, not Python's: the model wrote JSON and has to fix JSON.
_JSON_TYPE_NAMES = {
    bool: "a boolean",
    int: "a number",
    float: "a number",
    str: "a string",
    list: "an array",
    dict: "an object",
    type(None): "null",
}

# Scalar targets a JSON object or array can never fill. The HTTP binder hands an
# already-structured value straight back, which is right there - a scalar
# parameter's value arrives as text - but an MCP argument is raw JSON from the
# client, so an array reaching an `int` parameter has to be refused here or it
# lands in the handler as one.
_SCALAR_TARGETS = frozenset({str, int, float, bool})


def _json_type_name(value: Any) -> str:
    """Name `value`'s JSON type the way the client that sent it would."""
    return _JSON_TYPE_NAMES.get(type(value), "a value")


def _wrong_type(name: str, expected: str, value: Any) -> RequestValidationError:
    """Build the binding error for an argument whose JSON type is not the declared one."""
    return RequestValidationError(
        [
            {
                "loc": ["body", name],
                "msg": f"Invalid value for {name}: expected {expected}, got {_json_type_name(value)}",
                "type": "type_error",
            }
        ]
    )


def _coerce_json_scalar(slot: _Slot, value: Any, target: Any) -> Any:
    """Coerce one JSON argument onto a scalar parameter, refusing a wrong type.

    The tool published a schema; an argument contradicting it is the model's
    mistake to correct, and it can only correct what it is told. Passing the
    value through instead leaves the handler holding a type it never declared.
    """
    if not target:
        return value
    if value is None:
        if slot.is_optional:
            return None
        raise _wrong_type(slot.name, _declared_type_name(target), value)
    if target is str:
        # The HTTP binder hands a `str` target's value back untouched, since over
        # HTTP it is already text. A JSON argument need not be.
        if isinstance(value, str):
            return value
        raise _wrong_type(slot.name, "a string", value)
    if target is bool:
        # JSON has a boolean; a number or a string is not it. The HTTP coercer
        # reads "yes" / "1" as true because a query string has nothing else to
        # offer, which would silently make "maybe" false here.
        if value is True or value is False:
            return value
        raise _wrong_type(slot.name, "a boolean", value)
    if value is True or value is False:
        # `bool` is a subclass of `int`, so an unguarded number target would take
        # `true` and hand the handler 1.
        if target is int or target is float:
            raise _wrong_type(slot.name, "a number", value)
    elif target is int and isinstance(value, float) and not value.is_integer():
        # JSON Schema's `integer` accepts a number whose fractional part is zero,
        # and nothing else. Coercing would hand the handler a different value than
        # the one that was sent, which is the failure that does not surface.
        raise RequestValidationError(
            [
                {
                    "loc": ["body", slot.name],
                    "msg": (
                        f"Invalid value for {slot.name}: expected an integer, "
                        f"got {value!r}, which would lose its fractional part"
                    ),
                    "type": "type_error",
                }
            ]
        )
    if isinstance(value, (dict, list)):
        if (
            target in _SCALAR_TARGETS
            or hasattr(target, "__members__")
            or get_origin(target) is Literal
        ):
            raise _wrong_type(slot.name, _declared_type_name(target), value)
        # A parameter declared to take an object or an array takes this one.
        return value
    return _coerce_value(value, target, slot.name, "body")


def _coerce_list_item(slot: _Slot, value: Any, target: Any, nullable: bool) -> Any:
    """Coerce one array element, whose nullability is the inner type's own.

    `_coerce_json_scalar` reads `slot.is_optional`, which describes the whole
    parameter: `list[str] | None` may be omitted, but its members are strings.
    The `None` case is therefore settled here before delegating.
    """
    if value is None:
        if nullable:
            return None
        raise _wrong_type(slot.name, _declared_type_name(target), None)
    # A declared member type that is a model is validated onto it, the way a
    # model-typed parameter is; `_coerce_json_scalar` only settles scalars and
    # would hand the handler the raw mapping.
    if is_pydantic_model(target):
        return _validate_model(value, target)
    if is_adaptable_model(target):
        return _validate_adapted(value, target)
    return _coerce_json_scalar(slot, value, target)


def _declared_type_name(target: Any) -> str:
    """Name the JSON type a declared parameter type accepts."""
    if target is bool:
        return "a boolean"
    if target in (int, float):
        return "a number"
    if target is str:
        return "a string"
    return f"a {getattr(target, '__name__', target)}"


def _coerce_argument(slot: _Slot, value: Any) -> Any:
    """Coerce one JSON argument value onto its handler parameter type."""
    kind = slot.kind

    if kind == K_BODY_MODEL:
        model = slot.model
        if is_pydantic_model(model):
            return _validate_model(value, model)
        if slot.backend == ModelBackend.ADAPTED:
            return _validate_adapted(value, model)
        return value

    if kind == K_PARAM_MARKER:
        target = slot.target_type
        if slot.marker_kind == MK_BODY and is_pydantic_model(target):
            return _validate_model(value, target)
        marker = slot.marker
        value = _coerce_json_scalar(slot, value, target)
        if marker is not None:
            return marker.validate(value, slot.name)
        return value

    if kind == K_QUERY_LIST:
        # The tool published an array, so a scalar is the model's mistake to
        # correct exactly as a wrong scalar type is. Wrapping it handed the
        # handler a one-element list nothing asked for: a search told to filter
        # by `'["a","b"]'` - a shape models really do send - filtered by one
        # nonsense tag and returned a plausible empty result with nothing in the
        # trace to say why. Elements go through the same strict coercion as a
        # bare parameter of the inner type, not the query-string one, which
        # would read `42` as a `list[str]` member.
        if value is None and slot.is_optional:
            # The parameter itself is nullable, so the array is simply absent.
            return None
        if not isinstance(value, list):
            raise _wrong_type(slot.name, "an array", value)
        item_nullable, item_type = _unwrap_optional(slot.list_inner)
        return [_coerce_list_item(slot, item, item_type, item_nullable) for item in value]

    if kind == K_QUERY:
        return _coerce_json_scalar(slot, value, slot.target_type)

    return value


def _validate_adapted(value: Any, model: Any) -> Any:
    """Validate `value` onto a dataclass / `TypedDict`, re-raising as a clear error.

    Mirrors `_validate_model` so an adapted type reports failures the same way a
    `BaseModel` parameter does.
    """
    try:
        return adapter_for(model).validate_python(value)
    except PydanticValidationError as exc:
        raise ValueError(str(exc)) from exc


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


# The `K_RESPONSE` / `K_BG_TASKS` slots bind exactly as they do on the HTTP
# path, so they delegate to the resolver rather than restating it: one
# `Response` and one `BackgroundTasks` queue per request, shared by the handler
# and every dependency that injects them, with `status_code = 0` as the "handler
# never set it" sentinel `_build_response` checks. Aliased here so the call sites
# below read the same as the slots they fill.
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

    `dependency_inputs` names the published inputs declared inside a `Depends`
    sub-dependency. Those are seeded onto the synthetic request for the HTTP
    resolver to read, and that resolver reads a query string - where `"1"` and
    `"yes"` have to mean true, because a query string has nothing else to offer.
    An agent sends typed JSON and the tool's own `inputSchema` advertises the
    declared type, so they are coerced here by the same strict rule the
    top-level slots use. Without it, moving a parameter behind a dependency
    silently changed whether the same value was accepted.
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

    # Hold a sub-dependency's inputs to the type this tool published, before the
    # seeding below hands them to a resolver that reads query-string rules. A
    # fresh dict is built only when there is something to coerce, so a tool with
    # no `Depends` inputs pays one truthiness test.
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

        # A handler may declare `response: Response`; supply the one the HTTP
        # resolver would. Reusing it (rather than handing out a fresh Response)
        # means a dependency and the handler that both inject `Response` mutate
        # the same object, and the route path's `_build_response` merges its
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
        elif (marker := slot.marker) is not None and marker.has_default:
            # A parameter marker owns its declared default (`Body(500)`), and the
            # slot's own `default` holds the hoisted marker rather than the value,
            # so the marker is the only source of the real one. `resolve_default`
            # also runs a `default_factory`, giving each call a fresh value -
            # matching what the HTTP resolver does for the same declaration.
            kwargs[name] = marker.resolve_default()
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
