"""Handler and model introspection helpers — the memoised signature and hint reads the lowering shares."""

from __future__ import annotations

import contextlib
import functools
import inspect
import weakref
from typing import TYPE_CHECKING, Any, get_type_hints

from veloce._handler_plan import extract_annotated_marker
from veloce._internal import resolve_hint_target
from veloce._params import ParamBase

# Deliberate re-exports (`X as X`); the schema layer itself lives in `_jsonschema`.
from veloce.contrib._jsonschema import _local_def_refs as _local_def_refs
from veloce.contrib._jsonschema import _rewrite_byte_format as _rewrite_byte_format
from veloce.contrib._jsonschema import (
    _warn_schema_fallback,
)
from veloce.dependency import Depends

if TYPE_CHECKING:  # pragma: no cover
    from veloce.app import Veloce


# Per-handler, weak-keyed memo of `inspect.signature` + `get_type_hints`.
_HANDLER_INTRO_CACHE: weakref.WeakKeyDictionary[Any, tuple[Any, dict[str, Any]]] = (
    weakref.WeakKeyDictionary()
)

# ── Introspection / merge helpers ──────────────────────────


def _group_field_schema(model: Any, wire_name: str) -> dict[str, Any] | None:
    """Return one field's declared schema from a grouped model, or `None`.

    `None` when the field resolves to a `$ref` (a nested model), which a
    parameter schema cannot carry - the caller falls back to the annotation.
    That fallback is lossy: the caller rebuilds the schema from the annotation
    alone, so the field's own `ge` / `le` / `title` do not reach the document
    while the resolver goes on enforcing them. A `$ref` is the expected reason
    to take it; an introspection failure is not, and is reported rather than
    left to look like the ordinary case.
    """
    try:
        properties = _grouped_model_properties(model)
    except Exception as exc:  # noqa: BLE001 - schema generation degrades to no properties with a warning
        _warn_schema_fallback(f"grouped field {wire_name!r} on {model!r}", exc)
        return None
    prop = properties.get(wire_name)
    if prop is None or "$ref" in prop:
        return None
    return dict(prop)


@functools.lru_cache(maxsize=256)
def _grouped_model_properties(model: Any) -> dict[str, Any]:
    """`{wire name: schema}` for a grouped model, by alias where one is set."""
    schema = model.model_json_schema(by_alias=True)
    return schema.get("properties") or {}


def _handler_intro(handler: Any) -> tuple[Any, dict[str, Any]]:
    """Return `(signature, hints)` for `handler`, memoized per callable.

    `signature` is `None` when `inspect.signature` cannot introspect
    the handler (built-ins, `functools.partial` chains with non-callable
    `func`). `hints` falls back to `{}` on the same failure modes.
    """
    try:
        cached = _HANDLER_INTRO_CACHE.get(handler)
    except TypeError:
        cached = None
    if cached is not None:
        return cached
    try:
        sig = inspect.signature(handler)
    except (TypeError, ValueError):
        sig = None
    hints: dict[str, Any] = {}
    # The same target the resolver reads; `handler` itself misses a class handler.
    hint_target = resolve_hint_target(handler)
    if hasattr(hint_target, "__annotations__"):
        try:
            # `include_extras=True` keeps the PEP 593 metadata a marker lives in.
            hints = get_type_hints(hint_target, include_extras=True)
        except Exception:  # noqa: BLE001 - unresolvable hints degrade to none rather than failing /docs
            # Degrades to no hints rather than failing the whole `/docs` build.
            hints = {}
    result = (sig, hints)
    with contextlib.suppress(TypeError):
        _HANDLER_INTRO_CACHE[handler] = result
    return result


def _param_marker(param: Any, hints: dict[str, Any]) -> Any:
    """Find the `Depends` / parameter marker for `param`, however it was spelled.

    A marker reaches a handler as the parameter default or as PEP 593 metadata.
    Resolution is delegated to the same helper the handler plan uses, so the
    published contract and the enforced one cannot drift apart.
    """
    default = param.default
    if isinstance(default, (Depends, ParamBase)):
        return default
    marker, _base = extract_annotated_marker(hints.get(param.name, param.annotation))
    return marker if marker is not None else default


def _deep_merge(target: dict[str, Any], overlay: dict[str, Any]) -> None:
    """Recursively merge `overlay` into `target` in place.

    Nested dicts merge key-by-key; any non-dict value (scalar, list)
    in `overlay` replaces the value in `target`.
    """
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _deep_merge(target[key], value)
        else:
            target[key] = value


# ── Python type → JSON Schema helpers ──────────────────────


# ── Info / parameter / body / response builders ────────────


def _build_info_object(app: Veloce) -> dict[str, Any]:
    """Return the OpenAPI `info` object assembled from app metadata."""
    # Read directly: `Veloce.__init__` requires both to be non-empty strings.
    info_obj: dict[str, Any] = {"title": app.title, "version": app.version}
    if app.summary:
        info_obj["summary"] = app.summary
    if app.description:
        info_obj["description"] = app.description
    if app.terms_of_service:
        info_obj["termsOfService"] = app.terms_of_service
    if app.contact:
        info_obj["contact"] = app.contact
    if app.license_info:
        info_obj["license"] = app.license_info
    return info_obj
