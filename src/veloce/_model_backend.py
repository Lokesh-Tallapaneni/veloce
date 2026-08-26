"""Model-backend detection — Pydantic, msgspec, and Pydantic-adapted types.

Detection is registration-time on the request side (a plan slot is tagged with
the backend once) and a single `isinstance` on the response side. msgspec is an
optional `[fast]` extra: it is probed once at import and never required, so with
it absent every msgspec branch is dead and behaviour is identical to today.
"""

from __future__ import annotations

import contextlib
import dataclasses
import types
import typing
import weakref
from collections.abc import Callable
from enum import IntEnum
from typing import Any, get_args, get_origin

from pydantic import BaseModel as _PydanticModel
from pydantic import TypeAdapter

# msgspec is optional. Probe once at import; never required. `_MSGSPEC_STRUCT` is
# typed `Any` (not `type | None`) so `issubclass(tp, _MSGSPEC_STRUCT)` type-checks
# without a per-call `assert ... is not None`; `_HAS_MSGSPEC` gates every use.
try:
    import msgspec as _msgspec

    _MSGSPEC_STRUCT: Any = _msgspec.Struct
    _HAS_MSGSPEC = True
except ImportError:  # pragma: no cover - exercised in the no-msgspec CI leg
    _msgspec = None  # type: ignore[assignment]
    _MSGSPEC_STRUCT = None
    _HAS_MSGSPEC = False


class ModelBackend(IntEnum):
    """Which validation/serialization backend owns a model type."""

    NONE = 0  # not a model - orjson / JSONResponse straight through
    PYDANTIC = 1  # subclass of pydantic.BaseModel
    MSGSPEC = 2  # subclass of msgspec.Struct
    ADAPTED = 3  # stdlib object type Pydantic validates through a TypeAdapter


def is_pydantic_model(tp: Any) -> bool:
    """Return True when `tp` is a `pydantic.BaseModel` subclass."""
    return isinstance(tp, type) and issubclass(tp, _PydanticModel)


def resolve_return_model(handler: Callable[..., Any]) -> Any:
    """Return the model type a handler declares as its return annotation, or `None`.

    Registration-time only. Resolved through `get_type_hints` so a
    `from __future__ import annotations` string annotation still yields the real
    class. Only a model type is returned: a transport shape (`Response` and its
    subclasses), `Any`, a bare `dict`, `None`, or any annotation that cannot be
    resolved degrades to `None`, so an unrepresentable return type simply
    declares no contract instead of needing an explicit opt-out.

    Single source of the return-annotation contract, so the HTTP door
    (`response_model`, OpenAPI) and the MCP door (`outputSchema`) derive the
    same model from the same handler.
    """
    try:
        hints = typing.get_type_hints(handler)
    except Exception:
        return None
    annotation = hints.get("return")
    if is_pydantic_model(annotation) or is_msgspec_struct(annotation):
        return annotation
    # A dataclass or `TypedDict` return declares an object shape as much as a
    # model does, so it is the same contract on both doors.
    if is_adaptable_model(annotation):
        return annotation
    return None


def _is_model_union(tp: Any) -> bool:
    """True when `tp` is a union whose members are all models or `None`."""
    if get_origin(tp) not in (typing.Union, types.UnionType):
        return False
    args = get_args(tp)
    if not args:
        return False
    members = [a for a in args if a is not type(None)]
    return bool(members) and all(is_pydantic_model(a) or is_msgspec_struct(a) for a in members)


def resolve_response_contract(handler: Callable[..., Any]) -> Any:
    """Return the response contract a handler declares in its return annotation.

    Registration-time only. Widens `resolve_return_model` to the shapes a route
    can document and serialize: a model, `list[Model]`, and a union of models
    (including `Model | None`). A union documents its alternatives but is not
    used to filter - which member a value should be re-shaped through is
    ambiguous - so only a model and `list[Model]` reach the response filter.

    Anything else - a transport class, `Any`, a bare `dict`, an unresolvable
    annotation - returns `None`, declaring no contract without needing an
    explicit opt-out.
    """
    try:
        hints = typing.get_type_hints(handler)
    except Exception:
        return None
    annotation = hints.get("return")
    if annotation is None:
        return None
    if is_pydantic_model(annotation) or is_msgspec_struct(annotation):
        return annotation
    # A dataclass or `TypedDict` declares an object shape as much as a model
    # does. `resolve_return_model` accepts both, and this function documents
    # itself as a *widening* of that one - dropping a shape the narrower resolver
    # accepts meant `-> SomeDataclass` produced an MCP `outputSchema` and no HTTP
    # response contract, so the return went unfiltered.
    if is_adaptable_model(annotation):
        return annotation
    origin = get_origin(annotation)
    if origin is list:
        args = get_args(annotation)
        if args and (is_pydantic_model(args[0]) or is_msgspec_struct(args[0])):
            return annotation
        return None
    if _is_model_union(annotation):
        return annotation
    return None


def is_msgspec_struct(tp: Any) -> bool:
    """Return True when `tp` is a `msgspec.Struct` subclass (and msgspec is installed)."""
    # `_HAS_MSGSPEC` short-circuits first, so `_MSGSPEC_STRUCT` is never None here.
    return _HAS_MSGSPEC and isinstance(tp, type) and issubclass(tp, _MSGSPEC_STRUCT)


def struct_to_dict(obj: Any) -> dict[str, Any]:
    """Return a `msgspec.Struct` instance's fields as a dict.

    Shallow by design: the caller is a JSON encoder that recurses into what it
    is handed, so nested values stay as they are and are converted by whatever
    rule already covers them.
    """
    return dict(_msgspec.structs.asdict(obj))


# `typing.is_typeddict` answers False for a class built from
# `typing_extensions.TypedDict` - which is the form Pydantic *requires* below
# Python 3.12 - so using it alone made those types invisible here and they fell
# to the scalar path. The typing_extensions predicate recognises both spellings.
# It ships with Pydantic, so it is always present in practice; the fallback
# keeps this importable if that ever stops being true.
try:
    from typing_extensions import is_typeddict as _is_typeddict
except ImportError:  # pragma: no cover - typing_extensions comes with pydantic
    from typing import is_typeddict as _is_typeddict


def is_adaptable_model(tp: Any) -> bool:
    """Return True for a dataclass or `TypedDict` Pydantic can validate.

    These describe an object shape without being a `BaseModel`, so without this
    they fall to the scalar path and are advertised as a string while the
    handler is handed the raw mapping.
    """
    if _is_typeddict(tp):
        return _typeddict_is_adaptable(tp)
    # `is_dataclass` answers True for an instance too; a slot annotation is a
    # type. A Pydantic dataclass is still a dataclass, and the adapter handles
    # it, so it needs no separate branch.
    return isinstance(tp, type) and dataclasses.is_dataclass(tp)


# Whether a given `TypedDict` can actually be adapted, answered once per type.
# Weak keys so a type defined inside a test or a factory is collected with it.
_typeddict_support: weakref.WeakKeyDictionary[Any, bool] = weakref.WeakKeyDictionary()


def _typeddict_is_adaptable(tp: Any) -> bool:
    """Whether Pydantic will build an adapter for this `TypedDict`.

    Below Python 3.12 Pydantic refuses a `typing.TypedDict` outright and asks
    for `typing_extensions.TypedDict` instead, because only the latter records
    the required/optional keys it needs. Claiming such a type as adaptable made
    every use of it fail at request time - a 500 on the HTTP door, an internal
    error on the MCP one - so it is probed once and, when unsupported, treated
    as the plain mapping it is rather than promised as a validated object.
    """
    cached = _typeddict_support.get(tp)
    if cached is not None:
        return cached
    try:
        TypeAdapter(tp)
        supported = True
    except Exception:
        supported = False
    with contextlib.suppress(TypeError):
        _typeddict_support[tp] = supported
    return supported


# One adapter per type, built on first use at registration. Construction runs a
# schema build (hundreds of microseconds), so it must never happen per request.
# Weak keys let a type defined inside a test or a factory be collected with it.
_adapters: weakref.WeakKeyDictionary[Any, TypeAdapter[Any]] = weakref.WeakKeyDictionary()


def adapter_for(tp: Any) -> TypeAdapter[Any]:
    """Return the memoised `TypeAdapter` validating `tp`."""
    try:
        cached = _adapters.get(tp)
    except TypeError:  # pragma: no cover - unhashable annotation
        return TypeAdapter(tp)
    if cached is not None:
        return cached
    built = TypeAdapter(tp)
    # A type that does not support weak references simply goes uncached.
    with contextlib.suppress(TypeError):
        _adapters[tp] = built
    return built


def shape_through_model(value: Any, model: Any) -> Any:
    """Validate `value` against `model` and dump it as JSON-compatible data.

    One shaper for every backend, so a declared output contract is enforced the
    same way whichever kind of type declared it. Raises when the value does not
    conform; the caller decides how to report that.
    """
    if is_pydantic_model(model):
        # Dump a model instance to a mapping *before* validating it. Handing
        # `model_validate` a subclass instance returns that subclass, and the
        # dump then carries the subclass's own fields - so a richer object
        # returned under a base-model contract leaks exactly the fields the
        # contract excludes. Going through a mapping is what drops them.
        payload = value.model_dump() if is_pydantic_model(type(value)) else value
        return model.model_validate(payload).model_dump(mode="json")
    if is_msgspec_struct(model):
        # `adapter_for` is a Pydantic `TypeAdapter` and raises
        # `PydanticSchemaGenerationError` on a Struct, which is why this branch
        # exists rather than falling through. `convert` from builtins drops any
        # field the target does not declare and still refuses a value that does
        # not conform.
        return _msgspec.to_builtins(_msgspec.convert(_msgspec.to_builtins(value), model))
    adapter = adapter_for(model)
    # A dataclass adapter refuses an instance of a *different* dataclass
    # outright ("Input should be a dictionary or an instance of X"), so an
    # unrelated richer object under a narrower contract was a 500 rather than a
    # filtered body. Dumping to a mapping first both fixes that and drops the
    # extra fields, the same way the Pydantic branch above does.
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        value = dataclasses.asdict(value)
    return adapter.dump_python(adapter.validate_python(value), mode="json")


def backend_of(tp: Any) -> ModelBackend:
    """Classify a single, already-unwrapped type. Registration-time only.

    A class cannot subclass both `BaseModel` and `msgspec.Struct` (their
    metaclasses conflict), so the Pydantic-first order is for clarity, not
    correctness. `ADAPTED` is tested last: a Pydantic dataclass is a dataclass
    too, and the dedicated backends describe such a type more precisely.
    """
    if is_pydantic_model(tp):
        return ModelBackend.PYDANTIC
    if is_msgspec_struct(tp):
        return ModelBackend.MSGSPEC
    if is_adaptable_model(tp):
        return ModelBackend.ADAPTED
    return ModelBackend.NONE
