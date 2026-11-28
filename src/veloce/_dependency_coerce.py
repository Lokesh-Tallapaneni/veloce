"""Parameter coercion and validation — turning request strings into declared types."""

from __future__ import annotations

import contextlib
import copy
import functools
from collections.abc import Callable
from enum import Enum
from typing import Any, Literal, get_args, get_origin

from pydantic import TypeAdapter
from pydantic import ValidationError as PydanticValidationError

from veloce._constants import (
    MSG_MISSING_PARAMETER,
)
from veloce._handler_plan import (
    _fixed_shape_args,
)
from veloce._model_backend import _msgspec, is_pydantic_model
from veloce.exceptions import RequestValidationError

# ── Helpers ───────────────────────────────────────────────


@functools.lru_cache(maxsize=512)
def _type_adapter(target_type: Any) -> TypeAdapter[Any] | None:
    """Build (once) and cache a Pydantic ``TypeAdapter`` for a parameter type.

    ``None``, the result for an annotation Pydantic cannot adapt, is cached too.
    """
    try:
        return TypeAdapter(target_type)
    except Exception:  # noqa: BLE001 - a type with no adapter is simply not validated
        return None


# What Pydantic accepts, so `?errors_only=ture` cannot mean "no filter".
_TRUE_VALUES = frozenset({"1", "on", "t", "true", "y", "yes"})
_FALSE_VALUES = frozenset({"0", "off", "f", "false", "n", "no"})


def _coerce_bool_value(value: str) -> bool:
    """Read a request string as a boolean, refusing anything that is not one."""
    lowered = value.lower()
    if lowered in _TRUE_VALUES:
        return True
    if lowered in _FALSE_VALUES:
        return False
    raise ValueError(f"not a boolean: {value!r}")


def _coerce_literal(value: Any, target_type: Any, param_name: str, loc: str) -> Any:
    """Match a request value against a `Literal[...]` parameter.

    Request values arrive as strings, which Pydantic's strict literal check
    rejects for an `int` / `bool` literal. Candidates are compared by *exact*
    runtime type, sidestepping Python's loose `1 == True` equivalence.
    """
    members = get_args(target_type)
    candidates: list[Any] = [value]
    if isinstance(value, str):
        with contextlib.suppress(ValueError):
            candidates.append(_coerce_bool_value(value))
        with contextlib.suppress(ValueError):
            candidates.append(int(value))
        with contextlib.suppress(ValueError):
            candidates.append(float(value))
    for member in members:
        # Matched on the member's value, but the member itself is returned.
        comparand = member.value if isinstance(member, Enum) else member
        for candidate in candidates:
            if type(candidate) is type(comparand) and candidate == comparand:
                return member
    raise RequestValidationError(
        [
            {
                "loc": [loc, param_name],
                "msg": f"Input should be one of: {', '.join(repr(m) for m in members)}",
                "type": "literal_error",
            }
        ]
    )


def _is_model_typed(target_type: Any) -> bool:
    """Return True for a Pydantic ``BaseModel`` subclass annotation."""
    return is_pydantic_model(target_type)


def _coerce_via_pydantic(value: Any, target_type: Any, param_name: str, loc: str) -> Any:
    """Validate and coerce a request value through Pydantic.

    Handles every annotation the fast scalar branches of `_coerce_value` do not
    (`datetime`, `UUID`, `Decimal`, constrained generics). An annotation Pydantic
    cannot adapt passes through unchanged.
    """
    try:
        adapter = _type_adapter(target_type)
    except TypeError:
        # Unhashable annotation - `lru_cache` cannot key it.
        return value
    if adapter is None:
        return value
    try:
        # A model-typed query param arrives as serialised JSON (`?tag={"a":1}`).
        if isinstance(value, str) and _is_model_typed(target_type):
            return adapter.validate_json(value)
        return adapter.validate_python(value)
    except PydanticValidationError as err:
        raise RequestValidationError(
            [
                {
                    "loc": [loc, param_name],
                    "msg": e["msg"],
                    "type": e["type"],
                }
                for e in err.errors()
            ]
        ) from err


def _is_non_integer_float(value: Any) -> bool:
    """Whether `value` is a `float` with no exact integer reading."""
    return isinstance(value, float) and not value.is_integer()


def _coerce_value(value: Any, target_type: Any, param_name: str, loc: str) -> Any:
    """Coerce a string value to the target type."""
    if target_type is str or target_type is Any or target_type is None:
        return value
    try:
        if target_type is int:
            # Pydantic's lax mode: whole-number float is fine, fractional isn't.
            if _is_non_integer_float(value):
                raise ValueError(f"{value} is not an integer")
            return int(value)
        if target_type is float:
            return float(value)
        if target_type is bool:
            if isinstance(value, str):
                return _coerce_bool_value(value)
            if isinstance(value, bool):
                return value
            # Pydantic accepts only 0/1 (int or float) for bool, not any
            # truthy value - `bool(value)` would accept anything.
            if isinstance(value, (int, float)) and value in (0, 1):
                return bool(value)
            raise ValueError(f"{value!r} is not a boolean")
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

        # `Literal[...]` needs string-aware member matching; the rest Pydantic.
    if get_origin(target_type) is Literal:
        return _coerce_literal(value, target_type, param_name, loc)
    return _coerce_via_pydantic(value, target_type, param_name, loc)


def _coerce_scalar(value: Any, target_type: Any, param_name: str, loc: str) -> Any:
    """Coerce `value` to `target_type`.

    The tail shared by every marker kind's resolution. A query/path/header/
    cookie/form value is always a string off the wire; a body value is
    JSON-typed and may be anything - including a `dict`/`list` a scalar
    target never accepts - so it is checked against the real value rather
    than assumed to already satisfy the annotation.
    """
    if target_type is str:
        if isinstance(value, str):
            return value
        raise RequestValidationError(
            [
                {
                    "loc": [loc, param_name],
                    "msg": f"Invalid value for {param_name}: expected str",
                    "type": "type_error",
                }
            ]
        )
    if target_type:
        return _coerce_value(value, target_type, param_name, loc)
    return value


def _err_body_validation(
    exc: PydanticValidationError, root: str | list[Any] = "body"
) -> RequestValidationError:
    """Rebuild a Pydantic validation failure as this framework's 422 shape.

    Pydantic's per-field path is appended to `root`, so every way of declaring a
    body reports the same `["body", ...]` location; a marker reading from
    elsewhere passes its own `MARKER_LOC` root. `root` is a list rather than a
    single string when the failure is a list-typed field's own elements - the
    field name is a path segment to prepend, not the whole location.
    """
    prefix = root if isinstance(root, list) else [root]
    return RequestValidationError(
        [
            {"loc": [*prefix, *err["loc"]], "msg": err["msg"], "type": err["type"]}
            for err in exc.errors()
        ]
    )


def _err_missing_marker(loc: str, name: str) -> RequestValidationError:
    """Build the missing-required error for a parameter marker.

    Covers `Query`, `Header`, `Cookie` and `Form`, which share the
    `value_error.missing` shape. A bare `K_QUERY` slot deliberately uses the
    distinct `'missing'` / `MSG_FIELD_REQUIRED` contract instead.
    """
    return RequestValidationError(
        [
            {
                "loc": [loc, name],
                "msg": MSG_MISSING_PARAMETER.format(name=name),
                "type": "value_error.missing",
            }
        ]
    )


def _list_marker_checks(
    marker: Any,
) -> tuple[int | None, int | None, Callable[[Any, str], Any] | None] | None:
    """Return what a list-typed marker enforces, or `None` when it declares nothing.

    `min_length` / `max_length` bound the item count on a repeated parameter
    (published as `minItems` / `maxItems`), so the per-element check is the same
    marker with those two cleared. Not memoized: `ParamBase` hashes by identity
    and a folded `Field` builds a fresh marker per plan.
    """
    per_item = (
        marker.ge is not None
        or marker.le is not None
        or marker.gt is not None
        or marker.lt is not None
        or marker.multiple_of is not None
        or marker.regex is not None
    )
    if not per_item and marker.min_length is None and marker.max_length is None:
        return None
    item_validate = None
    if per_item:
        item_marker = copy.copy(marker)
        item_marker.min_length = None
        item_marker.max_length = None
        item_validate = item_marker.validate
    return (marker.min_length, marker.max_length, item_validate)


def _as_declared_sequence(annotation: Any, items: list[Any]) -> Any:
    """Build the sequence type the annotation declares from validated members.

    A `set[int]` annotation must not hand the handler the list its members were
    collected in, or it receives duplicates its own annotation forbids.
    """
    origin = get_origin(annotation)
    if origin is set:
        return set(items)
    if origin is frozenset:
        return frozenset(items)
    if origin is tuple:
        return tuple(items)
    return items


def _collect_sequence_marker(values: list[str], target_type: Any, name: str, loc: str) -> Any:
    """Coerce repeated source values onto a declared sequence type.

    Called by the interpreted resolver and bound into the compiled one, so both
    agree about a fixed-shape `tuple[int, str]` and about a `set[str]`.
    """
    positional = _fixed_shape_args(target_type)
    if positional is not None:
        if len(values) != len(positional):
            # Also covers `tuple[()]`, which declares no positions at all.
            raise RequestValidationError(
                [
                    {
                        "loc": [loc, name],
                        "msg": f"{name} must have exactly {len(positional)} values",
                        "type": "value_error",
                    }
                ]
            )
        collected = [
            value if declared is str else _coerce_value(value, declared, name, loc)
            for value, declared in zip(values, positional, strict=True)
        ]
    else:
        args = get_args(target_type)
        inner = args[0] if args else str
        collected = [
            value if inner is str else _coerce_value(value, inner, name, loc) for value in values
        ]
    return _as_declared_sequence(target_type, collected)


def _validate_list_marker(
    values: list[Any],
    checks: tuple[int | None, int | None, Callable[[Any, str], Any] | None],
    name: str,
    loc: str,
) -> None:
    """Enforce a list-typed marker's resolved bounds on the collected values.

    Takes what `_list_marker_checks` returned rather than the marker, so the
    compiled resolver binds the tuple at registration.
    """
    min_items, max_items, item_validate = checks
    if min_items is not None and len(values) < min_items:
        raise RequestValidationError(
            [
                {
                    "loc": [loc, name],
                    "msg": f"{name} must have at least {min_items} items",
                    "type": "value_error",
                }
            ]
        )
    if max_items is not None and len(values) > max_items:
        raise RequestValidationError(
            [
                {
                    "loc": [loc, name],
                    "msg": f"{name} must have at most {max_items} items",
                    "type": "value_error",
                }
            ]
        )
    if item_validate is None:
        return
    try:
        for value in values:
            item_validate(value, name)
    except ValueError as e:
        raise RequestValidationError(
            [{"loc": [loc, name], "msg": str(e), "type": "value_error"}]
        ) from e


def _validate_body_list(raw: list[Any], slot: Any, embed: bool) -> Any:
    """Validate a JSON array body against the sequence type it declares.

    The members arrive already typed by the JSON decode, so the declared type is
    a contract to check rather than a string to parse. Checked through the same
    adapter a bare body model would use, which is Pydantic's lax answer: `"1"`
    is accepted for an `int`, `1.5` is not.

    `embed` decides the error root: an embedded list lives under its own key
    (`{"tags": [...]}`), so a member error's `loc` must name that key; a
    non-embedded list is the whole body, where the field name is only the
    Python parameter, not a key the client's wire data actually has.
    """
    adapter = slot.list_adapter
    if adapter is None:
        # The declared container itself, `tuple[int, str]` being heterogeneous.
        adapter = slot.list_adapter = _type_adapter(slot.target_type)
    if adapter is None:
        # An unadaptable element type leaves the members as decoded.
        return raw
    try:
        validated: Any = adapter.validate_python(raw)
    except PydanticValidationError as e:
        root = ["body", slot.name] if embed else "body"
        raise _err_body_validation(e, root) from e
    return validated


#: Empty when msgspec is absent, making the `except` a no-op not a NameError.
_msgspec_validation_error: tuple[type[BaseException], ...] = (
    (_msgspec.ValidationError,) if _msgspec is not None else ()
)
