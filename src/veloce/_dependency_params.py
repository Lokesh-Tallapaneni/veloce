"""Scalar and list parameter resolution — the per-slot readers the resolver shares."""

from __future__ import annotations

from typing import Any

from veloce._constants import MSG_FIELD_REQUIRED
from veloce._dependency_coerce import _coerce_value, _validate_list_marker
from veloce.exceptions import RequestValidationError
from veloce.http.request import Request


def _validated(marker: Any, value: Any, name: str, loc: str) -> Any:
    """Apply a source-less marker's bounds to a value the caller already found."""
    try:
        return marker.validate(value, name)
    except ValueError as e:
        raise RequestValidationError(
            [{"loc": [loc, name], "msg": str(e), "type": "value_error"}]
        ) from e


def _resolve_scalar_param(slot: Any, request: Request, path_params: dict[str, str]) -> Any:
    """Resolve a scalar path-or-query parameter to its coerced value.

    A path binding wins when the matched params include the name, then the
    query string. With no value, a default or optional yields that; otherwise
    the parameter is reported missing.
    """
    name = slot.name
    marker = slot.marker if slot.marker_validates else None
    if name in path_params:
        value = _coerce_value(path_params[name], slot.target_type or str, name, "path")
        return value if marker is None else _validated(marker, value, name, "path")
    # The alias names the query key; the path keys on the declared name.
    lookup = slot.lookup_name
    if lookup in request.query_params:
        # Under the key the schema published, not the parameter name.
        value = _coerce_value(
            request.query_params[lookup], slot.target_type or str, lookup, "query"
        )
        return value if marker is None else _validated(marker, value, lookup, "query")
    if slot.has_default:
        # A plain mutable default is wrapped in a copying factory at registration
        # (see `_guard_plain_mutable_default`); immutable defaults read inline.
        if slot.default_factory is not None:
            return slot.default_factory()
        return slot._static_default
    if slot.is_optional:
        return None
    raise RequestValidationError(
        [{"loc": ("query", lookup), "msg": MSG_FIELD_REQUIRED, "type": "missing"}]
    )


def _resolve_list_param(slot: Any, request: Request, path_params: dict[str, str]) -> Any:
    """Resolve a list-typed path-or-query parameter to a coerced list.

    A single path value becomes a one-element list; a query key yields every
    value the URL carried (`?tag=a&tag=b` -> ["a", "b"]). Falls back to the
    default / optional, else raises `missing`.
    """
    name = slot.name
    checks = slot.list_checks
    if name in path_params:
        values = [_coerce_value(path_params[name], slot.list_inner, name, "path")]
        if checks is not None:
            _validate_list_marker(values, checks, name, "path")
        return values
    lookup = slot.lookup_name
    if lookup in request.query_params:
        values = [
            _coerce_value(v, slot.list_inner, lookup, "query")
            for v in request.query_params.getall(lookup)
        ]
        if checks is not None:
            _validate_list_marker(values, checks, lookup, "query")
        return values
    if slot.has_default:
        # A plain mutable default is wrapped in a copying factory at registration
        # (see `_guard_plain_mutable_default`); immutable defaults read inline.
        if slot.default_factory is not None:
            return slot.default_factory()
        return slot._static_default
    if slot.is_optional:
        return None
    raise RequestValidationError(
        [{"loc": ("query", lookup), "msg": MSG_FIELD_REQUIRED, "type": "missing"}]
    )


def _every_value(source: Any, key: str) -> list[str]:
    """Every value stored under `key`, from a multi-dict or a plain mapping.

    A `Request` carries multi-value headers and cookies; a `WebSocket` carries
    plain dicts built from the handshake, with no `.getlist`.
    """
    getlist = getattr(source, "getlist", None)
    if getlist is not None:
        values: list[str] = getlist(key)
        return values
    value = source.get(key)
    return [] if value is None else [value]
