"""Annotation helpers and registration-time warnings for the handler plan."""

from __future__ import annotations

import copy
import types
import warnings
from collections.abc import Callable
from typing import Any, Union, get_args, get_origin, get_type_hints

from veloce._model_backend import ModelBackend, _msgspec
from veloce._params import (
    Body,
    Cookie,
    File,
    Form,
    Header,
    ParamBase,
    Path,
    deepcopyable,
    find_field_info,
    needs_per_request_copy,
)
from veloce._plan_kinds import (
    _SEQUENCE_ORIGINS,
    MK_BODY,
    MK_COOKIE,
    MK_FILE,
    MK_FORM,
    MK_HEADER,
    MK_PATH,
    MK_QUERY,
)
from veloce._plan_slot import _Slot
from veloce._warnings import _RegistrationWarning


# ── Annotation helpers ────────────────────────────────────
def _unwrap_optional(annotation: Any) -> tuple[bool, Any]:
    """Detect `Optional[T]` / `Union[T, None]` / `T | None` and unwrap T."""
    origin = get_origin(annotation)
    # `Union[X, None]` has origin typing.Union, `X | None` types.UnionType.
    if origin is Union or origin is types.UnionType:
        args = get_args(annotation)
        non_none = [a for a in args if a is not type(None)]
        if len(non_none) == 1 and type(None) in args:
            return True, non_none[0]
    return False, annotation


def extract_annotated_marker(annotation: Any) -> tuple[Any, Any]:
    """Split `Annotated[T, Depends()/Query()/...]` into `(marker, T)`.

    `(None, annotation)` when there is no Veloce marker. The one marker
    resolver, shared with the OpenAPI walkers, so what runs and what is
    published cannot disagree. Python 3.10's `get_type_hints` still applies the
    implicit-Optional rule PEP 484 dropped in 3.11, wrapping `Annotated` in
    `Optional[...]` and hiding the marker; that wrapper is peeled here.
    """
    if annotation is not None and not hasattr(annotation, "__metadata__"):
        was_optional, inner = _unwrap_optional(annotation)
        if was_optional and hasattr(inner, "__metadata__"):
            annotation = inner

    if get_origin(annotation) is None or not hasattr(annotation, "__metadata__"):
        return None, annotation

    from veloce.dependency import Depends  # local import breaks the import cycle

    meta_args = get_args(annotation)
    base_type = meta_args[0] if meta_args else annotation
    for meta in getattr(annotation, "__metadata__", ()):
        if isinstance(meta, (Depends, ParamBase)):
            return meta, base_type
    return None, base_type


def _annotated_field_info(annotation: Any) -> Any:
    """Return the pydantic `FieldInfo` an `Annotated` parameter carries, or `None`.

    Python 3.10's implicit-`Optional` wrapper is peeled first, as
    `extract_annotated_marker` peels it.
    """
    if annotation is not None and not hasattr(annotation, "__metadata__"):
        was_optional, inner = _unwrap_optional(annotation)
        if was_optional:
            annotation = inner
    return find_field_info(annotation)


def _unwrap_list(annotation: Any) -> tuple[bool, Any]:
    """Detect `list[T]` and unwrap T."""
    origin = get_origin(annotation)
    if origin is list:
        args = get_args(annotation)
        return True, (args[0] if args else str)
    return False, annotation


def _group_field_specs(
    model: Any, backend: ModelBackend, mk: int
) -> tuple[tuple[str, str, bool], ...]:
    """Resolve a grouped model's fields to `(validate_key, lookup_key, is_list)`.

    Two names because they differ: the value is read from the source under
    `lookup_key` (an alias, or a `Header` field's hyphenated spelling) and
    validated under `validate_key`. Decided here, so the request path is a
    plain `dict` build.
    """
    specs: list[tuple[str, str, bool]] = []
    if backend is ModelBackend.PYDANTIC:
        items = [
            (name, getattr(f, "alias", None), getattr(f, "annotation", None))
            for name, f in model.model_fields.items()
        ]
    elif backend is ModelBackend.ADAPTED:
        # A dataclass or TypedDict declares no aliases, so the wire name is the name.
        items = [(name, None, tp) for name, tp in get_type_hints(model).items()]
    else:
        items = [(f.name, f.encode_name, f.type) for f in _msgspec.structs.fields(model)]
    for name, alias, annotation in items:
        validate_key = alias or name
        key = validate_key
        # As a scalar `Header()` does: `x_token` spells `x-token` on the wire.
        if mk == MK_HEADER and not alias:
            key = key.replace("_", "-")
        origin = get_origin(annotation)
        specs.append((validate_key, key, origin in _SEQUENCE_ORIGINS))
    return tuple(specs)


def _marker_kind(marker: ParamBase) -> int:
    if isinstance(marker, Path):
        return MK_PATH
    if isinstance(marker, Header):
        return MK_HEADER
    if isinstance(marker, Cookie):
        return MK_COOKIE
    if isinstance(marker, Body):
        return MK_BODY
    if isinstance(marker, Form):
        return MK_FORM
    if isinstance(marker, File):
        return MK_FILE
    # Query is the default and a bare ParamBase falls here too.
    return MK_QUERY


def _raise_kwarg_ambiguity(
    handler: Callable[..., Any], param_name: str, marker: Any, seen: list[Any] | None
) -> None:
    """Raise on a by-name magic parameter that also carries a value marker."""
    # local: breaks the exceptions <-> http.response cycle (isort would reorder it)
    from veloce.exceptions import ConfigurationError

    marker_name = type(marker).__name__
    chain = ""
    # A top-level handler already names itself below.
    if seen and len(seen) > 1:
        names = [_callable_name(c) for c in seen]
        chain = f" (in dependency chain {' -> '.join(names)})"
    where = _callable_name(handler)
    raise ConfigurationError(
        f"parameter {param_name!r} on {where}{chain} is reserved for the injected "
        f"{param_name!r} object but also declares a {marker_name}() marker; the marker "
        f"would be silently ignored. Rename the parameter or drop the {marker_name}() "
        "marker to resolve the ambiguity."
    )


def _warn_shared_mutable_default(name: str, default: Any, *, stacklevel: int = 3) -> None:
    """Warn (once at registration) about a marker's shared mutable default.

    `stacklevel` counts the frames between here and the registration call the
    author wrote.
    """
    kind = type(default).__name__
    # Such a container is shared through the member, and `tuple` is not the fix.
    what = (
        f"a {kind} default holding a mutable member"
        if isinstance(default, (tuple, frozenset))
        else f"a mutable {kind} default"
    )
    fix = (
        "default_factory" if isinstance(default, (tuple, frozenset)) else f"default_factory={kind}"
    )
    warnings.warn(
        f"parameter {name!r} has {what} that is shared across requests; "
        f"pass {fix} so each request gets its own value",
        _RegistrationWarning,
        stacklevel=stacklevel,
    )


def _owned(name: str, owner: str) -> str:
    """Name a parameter, and the handler it belongs to when the caller knows."""
    return f"{owner}: parameter {name!r}" if owner else f"parameter {name!r}"


def _warn_field_info_overridden(
    name: str, marker: Any, owner: str = "", *, stacklevel: int = 3
) -> None:
    """Warn that a parameter's `Field` lost to the marker declared beside it.

    Declaring both says two things about one value, and the marker is the one
    Veloce acts on.
    """
    from veloce.dependency import Depends  # local import breaks the import cycle

    kind = type(marker).__name__
    # A `Depends()` / `Security()` carries no constraints, so it is not advice.
    remedy = "" if isinstance(marker, Depends) else f" - declare them on the {kind}() instead"
    warnings.warn(
        f"{_owned(name, owner)} declares both a pydantic Field() and a "
        f"{kind}() marker; the marker wins and the Field's "
        f"constraints are ignored{remedy}",
        _RegistrationWarning,
        stacklevel=stacklevel,
    )


def _warn_field_constraint_uncarried(name: str, keywords: tuple[str, ...], owner: str = "") -> None:
    """Warn that a `Field` declares a bound no parameter marker can carry.

    pydantic keeps `max_digits` / `decimal_places` / `allow_inf_nan` on its own
    metadata object, so the fold cannot see them to carry.
    """
    warnings.warn(
        f"{_owned(name, owner)} declares {', '.join(keywords)} on its pydantic Field(), which a "
        "parameter marker cannot carry, so it is neither enforced nor published; validate it "
        "inside the handler or declare the parameter with a model",
        _RegistrationWarning,
        stacklevel=4,
    )


def _warn_field_info_unused(
    name: str, binding: str, owner: str = "", *, stacklevel: int = 4
) -> None:
    """Warn that a `Field` cannot be carried by the binding its annotation chose.

    A body is validated by the model or read as an upload, neither of which
    consults a parameter marker.
    """
    warnings.warn(
        f"{_owned(name, owner)} carries a pydantic Field(), but its annotation binds it to "
        f"{binding}, where a parameter marker's constraints have no effect; they are "
        "ignored",
        _RegistrationWarning,
        stacklevel=stacklevel,
    )


def _guard_plain_mutable_default(slot: _Slot, name: str, *, stacklevel: int = 3) -> None:
    """Stop a plain `param: list = []` default from being shared across requests.

    A bare mutable default is one object on the signature, so returning it by
    identity lets one request's in-place mutation leak into the next.
    """
    if slot.has_default and needs_per_request_copy(slot._static_default):
        original = slot._static_default
        if deepcopyable(original):
            slot.default_factory = lambda: copy.deepcopy(original)
        _warn_shared_mutable_default(name, original, stacklevel=stacklevel)


def _callable_name(obj: Any) -> str:
    """Name `obj` for an error or warning the user has to act on.

    `__qualname__` first, so a handler defined as a method or inside a factory
    keeps its enclosing context; then `__name__`, then `repr`.
    """
    return getattr(obj, "__qualname__", None) or getattr(obj, "__name__", None) or repr(obj)
