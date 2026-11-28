"""Argument coercion — wire JSON values to the types a slot declares."""

from __future__ import annotations

from collections.abc import Callable
from types import UnionType
from typing import TYPE_CHECKING, Any, Literal, Union, get_args, get_origin

from pydantic import BaseModel
from pydantic import ValidationError as PydanticValidationError

from veloce._handler_plan import (
    _SEQUENCE_ORIGINS,
    K_BODY_MODEL,
    K_PARAM_MARKER,
    K_QUERY,
    K_QUERY_LIST,
    MARKER_LOC,
    MK_BODY,
    _unwrap_optional,
)
from veloce._model_backend import (
    ModelBackend,
    adapter_for,
    is_adaptable_model,
    is_pydantic_model,
)
from veloce.dependency import (
    _as_declared_sequence,
    _coerce_value,
    _fixed_shape_args,
    _is_non_integer_float,
    _validate_list_marker,
)
from veloce.exceptions import RequestValidationError

if TYPE_CHECKING:  # pragma: no cover
    from veloce._handler_plan import _Slot


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
    elif target is int and _is_non_integer_float(value):
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
