"""JSON encoding - convert arbitrary Python objects to JSON-compatible types."""

from __future__ import annotations

import dataclasses
import datetime
import decimal
import enum
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from pydantic import BaseModel

# Exact-type fast-path for leaf scalars. `dict.get(type(obj))` is a
# single hash lookup; an `isinstance` cascade on the same cases is
# linear in cascade length. Subclasses (`pathlib.PosixPath`, custom
# `IntEnum`, third-party `datetime` subclasses) intentionally miss the
# fast-path and fall through to the isinstance chain below.
_LEAF_TYPE_ENCODERS: dict[type, Callable[[Any], Any]] = {
    type(None): lambda _v: None,
    str: lambda v: v,
    int: lambda v: v,
    float: lambda v: v,
    bool: lambda v: v,
    uuid.UUID: str,
    decimal.Decimal: float,
    datetime.datetime: lambda v: v.isoformat(),
    datetime.date: lambda v: v.isoformat(),
    datetime.time: lambda v: v.isoformat(),
    datetime.timedelta: lambda v: v.total_seconds(),
}


def jsonable_encoder(
    obj: Any,
    include: set[str] | None = None,
    exclude: set[str] | None = None,
    exclude_unset: bool = False,
    exclude_defaults: bool = False,
    exclude_none: bool = False,
    *,
    _seen: set[int] | None = None,
) -> Any:
    """Convert complex objects to JSON-serializable types.

    Handles Pydantic models, dataclasses, datetime, Decimal, UUID, Enum, Path,
    sets, frozensets, and nested structures.

    `include` / `exclude` apply to dict keys at **every depth** - passing
    `exclude={"password"}` strips a `password` key wherever it appears
    in the structure, not only at the top level. `exclude_none` likewise
    drops `None`-valued keys from plain dicts at every depth, not only from
    a top-level model's own fields.

    Raises `ValueError` on a self-referential object graph (a container
    that transitively contains itself) instead of recursing until the
    stack overflows. Detection is by `id()`; the per-call `_seen` set
    is internal and should not be passed by callers.

    Usage:
        data = jsonable_encoder(my_pydantic_model, exclude={"password"})
    """
    # Exact-type leaf encoder (covers None, str, int, float, bool, UUID,
    # Decimal, datetime/date/time/timedelta in one hash lookup).
    encoder = _LEAF_TYPE_ENCODERS.get(type(obj))
    if encoder is not None:
        return encoder(obj)

    # Subclass-tolerant scalar fallthroughs (Path -> PosixPath/WindowsPath,
    # subclassed Enum / datetime / Decimal / bytes / bytearray).
    if isinstance(obj, enum.Enum):
        return obj.value
    if isinstance(obj, (bytes, bytearray)):
        return bytes(obj).decode("utf-8", errors="replace")
    if isinstance(obj, Path):
        return str(obj)
    if isinstance(obj, (datetime.datetime, datetime.date, datetime.time)):
        return obj.isoformat()
    if isinstance(obj, datetime.timedelta):
        return obj.total_seconds()
    if isinstance(obj, decimal.Decimal):
        return float(obj)
    if isinstance(obj, uuid.UUID):
        return str(obj)

    # Cycle detection - only matters for container types that recurse
    # back through `jsonable_encoder`. Allocate the seen-set lazily so
    # leaf-only call graphs pay zero.
    if _seen is None:
        _seen = set()
    obj_id = id(obj)
    if obj_id in _seen:
        raise ValueError(f"Circular reference detected while encoding {type(obj).__name__}")
    _seen.add(obj_id)
    try:
        if isinstance(obj, BaseModel):
            kwargs: dict[str, Any] = {}
            if include:
                kwargs["include"] = include
            if exclude:
                kwargs["exclude"] = exclude
            if exclude_unset:
                kwargs["exclude_unset"] = True
            if exclude_defaults:
                kwargs["exclude_defaults"] = True
            if exclude_none:
                kwargs["exclude_none"] = True
            # model_dump already honours the filters for the model's own
            # fields, but a field whose value is itself a plain dict must
            # still have `exclude_none` applied during re-encoding, so the
            # filter is forwarded rather than dropped.
            return jsonable_encoder(
                obj.model_dump(**kwargs), exclude_none=exclude_none, _seen=_seen
            )

        if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
            return jsonable_encoder(
                dataclasses.asdict(obj),
                include=include,
                exclude=exclude,
                exclude_none=exclude_none,
                _seen=_seen,
            )

        if isinstance(obj, dict):
            result = {}
            for key, value in obj.items():
                if exclude_none and value is None:
                    continue
                str_key = str(key)
                if include and str_key not in include:
                    continue
                if exclude and str_key in exclude:
                    continue
                # Forward the filters into the recursion so nested dicts
                # honour them too - matches the dataclass branch above.
                result[str_key] = jsonable_encoder(
                    value, include=include, exclude=exclude, exclude_none=exclude_none, _seen=_seen
                )
            return result

        if isinstance(obj, (list, tuple)):
            return [
                jsonable_encoder(
                    item, include=include, exclude=exclude, exclude_none=exclude_none, _seen=_seen
                )
                for item in obj
            ]

        if isinstance(obj, (set, frozenset)):
            return [
                jsonable_encoder(
                    item, include=include, exclude=exclude, exclude_none=exclude_none, _seen=_seen
                )
                for item in sorted(obj, key=str)
            ]

        # Fallback: try to convert to dict
        try:
            return jsonable_encoder(vars(obj), include=include, exclude=exclude, _seen=_seen)
        except TypeError:
            return str(obj)
    finally:
        _seen.discard(obj_id)


def orjson_default(obj: Any) -> Any:
    """Single-object fallback for orjson's `default=` hook.

    orjson natively encodes the common leaf types (str/int/float/bool/None,
    dict/list, datetime/date/time, UUID, Enum, dataclass) at C speed and only
    calls this hook for a leaf it cannot handle itself. So this converts ONE
    object and lets orjson recurse into whatever it returns - it deliberately
    does NOT walk the whole graph the way `jsonable_encoder` does, keeping the
    fast path untouched.

    Containers (set/frozenset) become a list of their raw items; orjson then
    re-enters this hook for any non-native members. Path/Decimal/timedelta map
    to their scalar form. bytes/bytearray decode UTF-8 with replacement to stay
    consistent with `jsonable_encoder`. Anything else falls back to `vars(obj)`,
    then `str(obj)` - matching `jsonable_encoder`'s last-resort behaviour so the
    two paths agree on the same conversions.
    """
    if isinstance(obj, (set, frozenset)):
        return sorted(obj, key=str)
    if isinstance(obj, Path):
        return str(obj)
    if isinstance(obj, decimal.Decimal):
        return float(obj)
    if isinstance(obj, datetime.timedelta):
        return obj.total_seconds()
    if isinstance(obj, (bytes, bytearray)):
        return bytes(obj).decode("utf-8", errors="replace")
    try:
        return vars(obj)
    except TypeError:
        pass
    return str(obj)
