"""Input and output schemas — the JSON Schema an MCP client sees for a tool."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from veloce._handler_plan import (
    K_BODY_MODEL,
    K_DEPENDS,
    K_MODEL_GROUP,
    K_REQUEST,
    _callable_name,
    _variadic_param_error,
    get_plan_extension,
)
from veloce._model_backend import (
    ModelBackend,
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
from veloce.routing.converters import path_param_schemas

if TYPE_CHECKING:  # pragma: no cover
    from veloce._handler_plan import HandlerPlan, _Slot
from veloce.contrib.mcp.plan_bridge._slots import _ANNOTATION_KEYWORDS, _is_context_slot

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
    facts = get_plan_extension(plan, "mcp")
    if facts is not None and facts.variadic_param is not None:
        name, kind = facts.variadic_param
        raise TypeError(_variadic_param_error(_callable_name(plan.handler), name, kind))

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
    *,
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

    # A body still demands a JSON document unless a default also makes the
    # parameter itself omittable; every other kind treats either alone as enough.
    if slot.kind == K_BODY_MODEL:
        required = not (d.has_default and d.is_optional)
    else:
        required = not (d.has_default or d.is_optional)
    return prop, required


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
