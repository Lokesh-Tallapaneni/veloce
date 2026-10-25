"""Plan bridge — HandlerPlan to MCP tool definition (input schema + binding).

The input schema is derived from the same `HandlerPlan` the HTTP dispatch path
uses, through the OpenAPI lowering's own type conversion, so the two cannot
drift. At call time `bind_arguments` produces the handler kwargs from the JSON
``arguments`` mapping, resolving any `Depends` graph through the shared
`DependencyResolver`.
"""

from __future__ import annotations

from collections.abc import Callable
from types import UnionType
from typing import TYPE_CHECKING, Any, Literal, Union, get_args, get_origin

from pydantic import BaseModel
from pydantic import ValidationError as PydanticValidationError

from veloce._handler_plan import (
    K_BG_TASKS,
    K_BODY_MODEL,
    K_DEPENDS,
    K_MODEL_GROUP,
    K_PARAM_MARKER,
    K_QUERY,
    K_QUERY_LIST,
    K_REQUEST,
    K_RESPONSE,
    K_SECURITY_SCOPES,
    MARKER_LOC,
    MK_BODY,
    _unwrap_optional,
)
from veloce._internal import _current_request_var
from veloce._model_backend import (
    ModelBackend,
    adapter_for,
    is_adaptable_model,
    is_pydantic_model,
)
from veloce._route_contract import describe_slot
from veloce.contrib._jsonschema import (
    _NO_DEFAULT,
    _adapted_to_schema,
    _apply_marker_constraints,
    _publishable_default,
    _pydantic_to_schema,
    _python_type_to_schema,
)
from veloce.contrib.mcp.context import MCPContext
from veloce.dependency import (
    _SEQUENCE_ORIGINS,
    DependencyResolver,
    SecurityScopes,
    _as_declared_sequence,
    _coerce_value,
    _fixed_shape_args,
    _validate_list_marker,
)
from veloce.exceptions import RequestValidationError
from veloce.http.datastructures import FormData, QueryParams
from veloce.http.request import Request
from veloce.routing.converters import path_param_schemas

if TYPE_CHECKING:  # pragma: no cover
    from veloce._handler_plan import HandlerPlan, _Slot


# Slot kinds an agent supplies as JSON; a required one absent from `arguments`
# is a binding error, not a handler error.
_INPUT_KINDS = frozenset({K_BODY_MODEL, K_QUERY_LIST, K_PARAM_MARKER, K_QUERY})

# Keywords that annotate rather than constrain, so they stay outside the `anyOf`.
_ANNOTATION_KEYWORDS = frozenset({"description", "title", "default"})


# ── Reading the handler plan ──────────────────────────────
def _is_context_slot(slot: _Slot) -> bool:
    """Whether `slot` binds the MCPContext rather than an agent input.

    Detected by the parameter's TYPE annotation, never by name: a parameter merely
    *named* `ctx` but typed as a normal value stays an agent input. A parameter
    typed `MCPContext` lands on a K_QUERY slot - it is neither `Request` nor a
    model - so the K_QUERY guard scopes the check.
    """
    return slot.kind == K_QUERY and slot.target_type is MCPContext


def _plan_reaches_context(plan: Any, _seen: set[int] | None = None) -> bool:
    """Whether this plan, or anything it depends on, binds the `MCPContext`.

    Every extra message a `tools/call` can emit is reached through `MCPContext`,
    so a graph that never binds it produces exactly one response. This reads a
    signature, so a handler dispatching to another is `MCPTool.dispatches_tools`
    instead; an unwalkable plan answers `True` and keeps the streaming response.
    """
    if plan is None:
        return True
    slots = getattr(plan, "slots", None)
    if slots is None:
        return True
    _seen = set() if _seen is None else _seen
    if id(plan) in _seen:
        return False
    _seen.add(id(plan))
    for slot in slots:
        if _is_context_slot(slot):
            return True
        if slot.kind == K_DEPENDS and _plan_reaches_context(slot.sub_plan, _seen):
            return True
    return False


# An MCP input schema must be standalone, so refs are rewritten and defs inlined.
_OPENAPI_REF_PREFIX = "#/components/schemas/"
_MCP_REF_PREFIX = "#/$defs/"

# The spec assumes Draft 2020-12 when omitted; declaring it removes the ambiguity.
JSON_SCHEMA_DIALECT = "https://json-schema.org/draft/2020-12/schema"


# ── Schema generation ─────────────────────────────────────
def build_input_schema(
    plan: HandlerPlan,
    schemas_registry: dict[str, dict[str, Any]],
    path_template: str | None = None,
    *,
    dependency_inputs: dict[str, _Slot] | None = None,
) -> dict[str, Any]:
    """Build the MCP tool input JSON Schema from a handler plan.

    Each handler parameter an agent supplies becomes a property; one with no
    default is required. `Depends`, `MCPContext` and `Request`/`Response` slots
    are not agent inputs. Body models are referenced by `$ref` and inlined under
    `$defs`, so each tool's schema is standalone.

    A client-supplied parameter declared inside a `Depends` sub-dependency is
    sourced from the same `arguments` mapping, so it is advertised too, and
    `dependency_inputs`, when given, is filled with those inputs keyed by name.

    `path_template` is the backing route's path; its parameters are part of the
    call's contract whether or not a signature names them, so a placeholder no
    slot declares is advertised from the route itself.
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

    Mirrors `build_input_schema`, returning the model's own object schema at the top
    level: an MCP `outputSchema` describes the structured tool result, which is
    always a JSON object. `None` if the model's schema could not be produced.

    Rendered in serialization mode, and keyed `by_alias`, so the advertised schema
    matches what `model_dump(mode="json")` emits and the emitted
    `structuredContent` conforms to it.
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
    # So a strict client validates `structuredContent` under 2020-12.
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

    Recurses into every `Depends` sub-plan so a parameter declared on a
    sub-dependency is advertised exactly as a top-level input of that kind. Merge is
    by name, first declaration winning: a later identical name is the same cached
    dependency consuming the same argument. `seen_plans` guards the walk because a
    diamond-shaped graph can reach one sub-plan twice.
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

        # A body model validates against the whole mapping, so its fields are
        # the tool's inputs rather than the parameter name.
        if in_depends and slot.kind == K_BODY_MODEL and is_pydantic_model(slot.model):
            _spread_model_fields(slot.model, properties, required, schemas_registry)
            continue

        # A `Query(group=True)` model reads each field off its own wire key.
        if slot.kind == K_MODEL_GROUP and is_pydantic_model(slot.model):
            _spread_model_fields(slot.model, properties, required, schemas_registry)
            continue

        # An already-declared name is the same client-supplied value.
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

    Nested refs are followed transitively and every ref is rewritten in place to
    the local `#/$defs/<Name>` form, inside the collected defs included.
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

    Pydantic leaves a nested model's inner refs in their native `#/$defs/<Name>`
    form - only the top-level wrapper carries the OpenAPI prefix. Those need no
    rewrite, but the name must still be collected so the component is inlined.
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
    """Copy a JSON-Schema fragment before any in-place ref rewriting.

    Keeps the shared component registry from being mutated.
    """
    if isinstance(node, dict):
        return {key: _deepcopy_schema(value) for key, value in node.items()}
    if isinstance(node, list):
        return [_deepcopy_schema(item) for item in node]
    return node


def _item_schema(inner: Any, schemas_registry: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Return the schema for one element of a list parameter.

    `_python_type_to_schema` describes a value that arrived as text, so it calls a
    model `{"type": "string"}` - right for `?tag={"name":"x"}` over HTTP, wrong
    here: an MCP argument is JSON and the binder builds the model from a real
    object.
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
        # The same keywords the OpenAPI lowering publishes, so the tool schema
        # cannot advertise a wider contract than the server accepts.
        if d.model is None:
            prop = dict(prop)
            _apply_marker_constraints(prop, d.marker)
        # `publish_null` is on because a marker's `None` on a nullable parameter
        # is valid against the `anyOf` this schema publishes.
        marker_default = _publishable_default(
            getattr(d.marker, "default", ...), publish_null=d.is_optional
        )
        if marker_default is not _NO_DEFAULT:
            prop = {**prop, "default": marker_default}
    elif d.has_default:
        # A bare Python default is as much a part of the contract as a marker's.
        bare_default = _publishable_default(d.default, publish_null=False)
        if bare_default is not _NO_DEFAULT:
            prop = {**prop, "default": bare_default}

    # An optional parameter accepts an explicit null as well as omission.
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


# The names are JSON's, not Python's: the model wrote JSON and has to fix JSON.
_JSON_TYPE_NAMES = {
    bool: "a boolean",
    int: "a number",
    float: "a number",
    str: "a string",
    list: "an array",
    dict: "an object",
    type(None): "null",
}

# Scalar targets a JSON object or array can never fill: an array reaching an
# `int` must be refused or it lands in the handler as one.
_SCALAR_TARGETS = frozenset({str, int, float, bool})


# ── Argument coercion ─────────────────────────────────────
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
    mistake to correct, and passing it through instead would leave the handler
    holding a type it never declared.
    """
    if not target:
        return value
    if value is None:
        if slot.is_optional:
            return None
        raise _wrong_type(slot.name, _declared_type_name(target), value)
    if target is str:
        # Over HTTP the value is already text; a JSON argument need not be.
        if isinstance(value, str):
            return value
        raise _wrong_type(slot.name, "a string", value)
    if target is bool:
        # The HTTP coercer reads "yes" / "1" as true, making "maybe" false here.
        if value is True or value is False:
            return value
        raise _wrong_type(slot.name, "a boolean", value)
    if value is True or value is False:
        # `bool` subclasses `int`, so an unguarded number target would take `true`.
        if target is int or target is float:
            raise _wrong_type(slot.name, "a number", value)
    elif target is int and isinstance(value, float) and not value.is_integer():
        # JSON Schema's `integer` accepts only a zero fractional part.
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


def _coerce_list_item(
    slot: _Slot,
    value: Any,
    target: Any,
    nullable: bool,
    validate: Callable[[Any, Any], Any] | None,
) -> Any:
    """Coerce one array element, whose nullability is the inner type's own.

    `_coerce_json_scalar` reads `slot.is_optional`, which describes the whole
    parameter: `list[str] | None` may be omitted, but its members are strings, so
    the `None` case is settled here before delegating.

    `validate` is the model validator the element type needs, or `None` for a
    scalar - decided by the caller, since it cannot vary between members.
    """
    if value is None:
        if nullable:
            return None
        raise _wrong_type(slot.name, _declared_type_name(target), None)
    if target is Any:
        return value
    if validate is not None:
        return validate(value, target)
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


#: `int | str` and `Union[int, str]` do not share an origin object.
_UNION_ORIGINS = frozenset({Union, UnionType})


def _union_members(target: Any) -> tuple[Any, ...]:
    """Return a union's member types, or `()` when `target` is not a union."""
    if get_origin(target) in _UNION_ORIGINS:
        return tuple(member for member in get_args(target) if member is not type(None))
    return ()


def _coerce_union_member(slot: _Slot, value: Any, target: Any) -> Any:
    """Accept `value` if it satisfies any member of the union, else refuse.

    Each member is tried through the same strict scalar check a bare parameter of
    that type gets, so a union is as strict as its members rather than as permissive
    as an annotation no branch recognised.
    """
    members = _union_members(target)
    for member in members:
        try:
            return _coerce_json_scalar(slot, value, member)
        except RequestValidationError:
            continue
    raise _wrong_type(
        slot.name, " or ".join(_declared_type_name(member) for member in members), value
    )


def _build_sequence(slot: _Slot, declared: Any, items: list[Any], raw: Any) -> Any:
    """Build the declared container, refusing members it cannot hold.

    A `set` or `frozenset` of an unhashable member type raises `TypeError` from the
    constructor, which unhandled reaches the agent as the interpreter's own
    "unhashable type" text where the HTTP door answers a validation error.
    """
    try:
        return _as_declared_sequence(declared, items)
    except TypeError as exc:
        raise _wrong_type(slot.name, "an array of unique, hashable items", raw) from exc


def _coerce_nested_sequence(slot: _Slot, value: Any, declared: Any) -> Any:
    """Coerce one sequence-typed member, recursing into its own members."""
    if not isinstance(value, list):
        raise _wrong_type(slot.name, "an array", value)
    positional = _fixed_shape_args(declared)
    if positional is not None:
        if len(value) != len(positional):
            raise _wrong_type(slot.name, f"an array of {len(positional)} items", value)
        items = [
            _coerce_list_item(slot, item, *_member_contract(slot, inner))
            for item, inner in zip(value, positional, strict=True)
        ]
    else:
        args = get_args(declared)
        target, nullable, validate = _member_contract(slot, args[0] if args else Any)
        items = [_coerce_list_item(slot, item, target, nullable, validate) for item in value]
    return _build_sequence(slot, declared, items, value)


def _member_contract(
    slot: _Slot, declared: Any
) -> tuple[Any, bool, Callable[[Any, Any], Any] | None]:
    """Return what one declared member type needs: `(type, nullable, validator)`.

    A member type that is a model is validated onto it, the way a model-typed
    parameter is; `_coerce_json_scalar` only settles scalars and would hand the
    handler the raw mapping. Resolved per declared type, not per member.
    """
    nullable, target = _unwrap_optional(declared)
    if not nullable and get_origin(target) in _UNION_ORIGINS:
        # `_unwrap_optional` reads only the two-member `T | None` spelling.
        nullable = type(None) in get_args(target)
    if target is Any or target is object:
        # Neither declares anything to check, `None` included.
        return target, True, None
    if get_origin(target) in _SEQUENCE_ORIGINS:
        # A sequence member has its own members and container to rebuild.
        def _nested(value: Any, nested_target: Any) -> Any:
            return _coerce_nested_sequence(slot, value, nested_target)

        return target, nullable, _nested
    if _union_members(target):
        # A union declares several acceptable JSON types, so a member is held to all.
        def _union(value: Any, union_target: Any) -> Any:
            return _coerce_union_member(slot, value, union_target)

        return target, nullable, _union
    if is_pydantic_model(target):
        return target, nullable, _validate_model
    if is_adaptable_model(target):
        return target, nullable, _validate_adapted
    return target, nullable, None


def _coerce_list_members(slot: _Slot, value: list[Any]) -> list[Any]:
    """Coerce each member onto its declared element type, then bound the list.

    One rule for every spelling of a list marker, so no two of them accept what
    another refuses. Coercion comes first because `ParamBase.validate` dispatches on
    the Python type: a raw `["0"]` would take the string branch, skip the numeric
    bound the schema publishes as `items.minimum`, and hand the handler a `str` for
    a declared `list[int]`. Bounding is conditional, coercion is not.

    This door is stricter than HTTP's, which validates an array body through
    Pydantic's lax mode and so converts a `True` into an `int` where this refuses
    it: a model that sent the wrong JSON type is better told so than corrected.
    """
    positional = _fixed_shape_args(slot.target_type)
    if positional is not None:
        # A fixed-shape `tuple[int, str]` declares a type per position.
        if len(value) != len(positional):
            raise _wrong_type(slot.name, f"an array of {len(positional)} items", value)
        items = [
            _coerce_list_item(slot, item, *_member_contract(slot, declared))
            for item, declared in zip(value, positional, strict=True)
        ]
    else:
        # Resolved once: the contract cannot vary between members of one array.
        target, nullable, validate = _member_contract(slot, slot.list_inner)
        items = [_coerce_list_item(slot, item, target, nullable, validate) for item in value]
    # Built first, then bounded: a `set` drops duplicates, so `min_length=2` must
    # be checked against the built container.
    collected = _build_sequence(slot, slot.target_type, items, value)
    if slot.list_checks is not None:
        _validate_list_marker(collected, slot.list_checks, slot.name, MARKER_LOC[slot.marker_kind])
    return collected


def _validate_marker_bound(slot: _Slot, value: Any) -> Any:
    """Apply a marker's scalar bound on the agent door, as HTTP does.

    Reported as `RequestValidationError` rather than the bare `ValueError`
    `marker.validate` raises. `_invocation` refuses the call either way; the
    difference is that this type's message names the field and the bound.
    """
    marker = slot.marker
    if marker is None:
        return value
    try:
        return marker.validate(value, slot.name)
    except ValueError as exc:
        raise RequestValidationError(
            [
                {
                    "loc": [MARKER_LOC[slot.marker_kind], slot.name],
                    "msg": str(exc),
                    "type": "value_error",
                }
            ]
        ) from exc


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
        if marker is None:
            return value
        # `marker.validate` guards skip a list, so a published `minItems` /
        # `items.minimum` is enforced here instead.
        if slot.marker_reads_list and isinstance(value, list):
            return _coerce_list_members(slot, value)
        return marker.validate(value, slot.name)

    if kind == K_QUERY_LIST:
        # The tool published an array, so a scalar is the model's mistake to
        # correct; elements take the strict coercion, not the query-string one.
        if value is None and slot.is_optional:
            # The parameter itself is nullable, so the array is simply absent.
            return None
        if not isinstance(value, list):
            raise _wrong_type(slot.name, "an array", value)
        # A folded `Field` publishes these bounds, so this door enforces them.
        return _coerce_list_members(slot, value)

    if kind == K_QUERY:
        value = _coerce_json_scalar(slot, value, slot.target_type)
        if slot.marker_validates:
            return _validate_marker_bound(slot, value)
        return value

    return value


# ── Model binding ─────────────────────────────────────────
def _bind_model_group(slot: _Slot, arguments: dict[str, Any]) -> Any:
    """Rebuild a grouped model from the flat arguments its fields were published as.

    The field walk is `slot.group_fields`, precomputed at registration and shared
    with the HTTP resolver, so an agent and a browser get the same answer -
    constraint errors included.
    """
    raw = {
        validate_key: arguments[wire_key]
        for validate_key, wire_key, _is_list in slot.group_fields
        if wire_key in arguments
    }
    # A group slot is built from a marker, so the fallback is for the type checker.
    marker = slot.marker
    has_default = marker is not None and marker.has_default
    if not raw and (slot.is_optional or has_default):
        return marker.resolve_default() if marker is not None and has_default else None
    model = slot.model
    if is_pydantic_model(model):
        return _validate_model(raw, model)
    if slot.backend == ModelBackend.ADAPTED:
        return _validate_adapted(raw, model)
    return raw


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

    A tool argument may already be typed, so it is rendered back to the wire string
    the HTTP coercion expects: `True` -> `"true"`, lower-cased to satisfy the bool
    coercion's truthy set. A non-scalar is not a query-style value and is skipped so
    it cannot poison the source; such values are read from the JSON body.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return value
    return None


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
        elif slot.is_optional:
            kwargs[name] = None
        elif kind in _INPUT_KINDS:
            # At the binding boundary, so the server maps it to invalid-params.
            raise TypeError(f"missing required argument: {name!r}")

    return kwargs, request
