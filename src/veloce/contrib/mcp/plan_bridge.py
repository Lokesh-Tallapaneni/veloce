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

from typing import TYPE_CHECKING, Any, cast

from pydantic import BaseModel
from pydantic import ValidationError as PydanticValidationError

from veloce._handler_plan import (
    K_BODY_MODEL,
    K_DEPENDS,
    K_PARAM_MARKER,
    K_QUERY,
    K_QUERY_LIST,
    K_REQUEST,
    MK_BODY,
)
from veloce.contrib.mcp.context import MCPContext
from veloce.contrib.openapi import _pydantic_to_schema, _python_type_to_schema
from veloce.dependency import _coerce_value

if TYPE_CHECKING:  # pragma: no cover
    from veloce._handler_plan import HandlerPlan, _Slot
    from veloce.dependency import DependencyResolver


# Slot kinds an MCP tool input cannot source from the JSON arguments (they
# belong to the HTTP request/response cycle or the DI graph). Skipped during
# schema build; bound to the MCPContext or resolved at call time.
_SKIP_INPUT_KINDS = frozenset({K_REQUEST, K_DEPENDS})


def _is_context_slot(slot: _Slot) -> bool:
    """Whether `slot` binds the MCPContext rather than an agent input.

    A parameter typed `MCPContext` lands on a K_QUERY slot (it is neither
    `Request` nor a model), so it is detected by its target type; the `ctx` /
    `context` parameter names are honoured the same way the WS path honours
    `ws` / `websocket`.
    """
    if slot.kind != K_QUERY:
        return False
    return slot.target_type is MCPContext or slot.name in ("ctx", "context")


def build_input_schema(plan: HandlerPlan, schemas_registry: dict[str, dict]) -> dict[str, Any]:
    """Build the MCP tool input JSON Schema from a handler plan.

    Each handler parameter an agent supplies becomes a property; a parameter
    with no default is required. `Depends`, the `MCPContext`, and
    `Request`/`Response` slots are not agent inputs and are omitted. Nested
    Pydantic body models are emitted as `$ref`s into `schemas_registry`,
    matching the OpenAPI component layout.
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
    return schema


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


async def bind_arguments(
    plan: HandlerPlan,
    arguments: dict[str, Any],
    context: MCPContext,
    resolver: DependencyResolver,
) -> dict[str, Any]:
    """Resolve handler kwargs for a tool call from the JSON `arguments`.

    Scalar / model parameters are read from `arguments` and coerced through
    Pydantic; `Depends` graphs resolve through `resolver` (the `MCPContext`
    occupies the `Request` slot, as a WebSocket does on the WS path); an
    `MCPContext`-typed parameter receives `context`. Run
    `resolver.run_teardowns()` after the handler returns to drain any
    `yield`-style dependency teardowns.
    """
    resolver.reset()
    kwargs: dict[str, Any] = {}

    for slot in plan.slots:
        kind = slot.kind
        name = slot.name

        if kind == K_REQUEST or _is_context_slot(slot):
            kwargs[name] = context
            continue

        if kind == K_DEPENDS:
            # The MCPContext stands in for the Request (mirroring WS DI, which
            # passes a WebSocket where the resolver expects a Request); an
            # empty path-params map - tools have no URL path.
            kwargs[name] = await resolver._exec_depends(slot, cast("Any", context), {})
            continue

        if name in arguments:
            kwargs[name] = _coerce_argument(slot, arguments[name])
        elif slot.has_default:
            kwargs[name] = slot.default
        elif slot.is_optional:
            kwargs[name] = None
        # A required input that is absent is left unset so the handler raises
        # a clear TypeError; the server surfaces it as an error result.

    return kwargs
