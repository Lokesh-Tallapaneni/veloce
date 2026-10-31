"""JSON encoding — convert arbitrary Python objects to JSON-compatible types."""

from __future__ import annotations

import base64
import dataclasses
import datetime
import decimal
import enum
import ipaddress
import math
import re
import uuid
from collections import deque
from collections.abc import Callable
from pathlib import Path
from types import GeneratorType
from typing import Any

from pydantic import BaseModel

from veloce._model_backend import is_msgspec_struct, struct_to_dict
from veloce.secret import Secret

# orjson raises outside this window; a Decimal beyond it is emitted as a string.
_ORJSON_INT_MIN = -(2**63)
_ORJSON_INT_MAX = 2**64

# One literal, so the two guard sites cannot drift.
_SECRET_REJECTED = "Secret must not be serialized to JSON; call .reveal() explicitly"


def _reject_secret() -> None:
    """Raise the shared `TypeError` for a Secret that reached the serialiser."""
    raise TypeError(_SECRET_REJECTED)


def _decimal_to_json(obj: decimal.Decimal) -> int | float | str:
    """Encode a Decimal preserving integer-valued ones as JSON integers.

    `Decimal('1')` is `1` not `1.0`, so a large whole number keeps its exact
    digits rather than losing precision through IEEE-754. Integers outside
    orjson's 64-bit window, `NaN`/`Infinity`, and fractional magnitudes whose
    `float` is non-finite all fall back to `str`.
    """
    parts = obj.as_tuple()
    exponent = parts.exponent
    # `exponent` is an int for finite decimals, or 'n'/'N'/'F' for NaN/sNaN/Inf.
    if isinstance(exponent, int) and exponent >= 0:
        # Returned as `str` WITHOUT materializing the int: an attacker-supplied
        # `1E1000000` must not force a million-digit allocation.
        if len(parts.digits) + exponent > 20:
            return str(obj)
        i = int(obj)
        if _ORJSON_INT_MIN <= i < _ORJSON_INT_MAX:
            return i
        return str(obj)
    try:
        f = float(obj)
    except (ValueError, OverflowError):
        return str(obj)
    return f if math.isfinite(f) else str(obj)


# One hash lookup where an `isinstance` cascade is linear; a subclass misses it.
_LEAF_TYPE_ENCODERS: dict[type[Any], Callable[[Any], Any]] = {
    type(None): lambda _v: None,
    str: lambda v: v,
    int: lambda v: v,
    float: lambda v: v,
    bool: lambda v: v,
    uuid.UUID: str,
    decimal.Decimal: _decimal_to_json,
    datetime.datetime: lambda v: v.isoformat(),
    datetime.date: lambda v: v.isoformat(),
    datetime.time: lambda v: v.isoformat(),
    datetime.timedelta: lambda v: v.total_seconds(),
}


# Effectively-final scalars `vars(obj)` would leak internals for.
_SCALAR_TYPE_ENCODERS: dict[type[Any], Callable[[Any], Any]] = {
    re.Pattern: lambda p: p.pattern,
    ipaddress.IPv4Address: str,
    ipaddress.IPv6Address: str,
    ipaddress.IPv4Interface: str,
    ipaddress.IPv6Interface: str,
    ipaddress.IPv4Network: str,
    ipaddress.IPv6Network: str,
}


# Walked before the built-in tables, so a registered type wins for the same class.
_REGISTERED_ENCODERS: dict[type[Any], Callable[[Any], Any]] = {}

# Disjoint keys, so the MRO walk probes one dict per base rather than two.
_BUILTIN_TYPE_ENCODERS: dict[type[Any], Callable[[Any], Any]] = {
    **_LEAF_TYPE_ENCODERS,
    **_SCALAR_TYPE_ENCODERS,
}

# Distinguishes "not cached" from a cached `None`, so one `get` probes the memo.
_MISSING = object()

# Cleared on a registry change, so `register_encoder` is not shadowed by a miss.
_RESOLVED_ENCODERS: dict[type[Any], Callable[[Any], Any] | None] = {}

# Bounds the cache for an app minting many runtime classes; past it, FIFO.
_MAX_RESOLVED_ENCODERS = 4096


def _resolve_encoder(cls: type[Any]) -> Callable[[Any], Any] | None:
    """Find the encoder for `cls` by walking its MRO, memoizing the result.

    Returns the first base present in the user registry, then the built-in
    tables, so a registered base wins and a subclass of `int`/`str`/`Decimal`
    encodes via its base rather than falling through to `vars(obj)`. A `None`
    result is cached too, so an unknown type does not re-walk the MRO.
    """
    cached = _RESOLVED_ENCODERS.get(cls, _MISSING)
    if cached is not _MISSING:
        return cached  # type: ignore[return-value]
    resolved: Callable[[Any], Any] | None = None
    # The registry must win for the same base, so it is probed first.
    registry = _REGISTERED_ENCODERS
    for base in cls.__mro__:
        encoder = registry.get(base) if registry else None
        if encoder is None:
            encoder = _BUILTIN_TYPE_ENCODERS.get(base)
        if encoder is not None:
            resolved = encoder
            break
    if len(_RESOLVED_ENCODERS) >= _MAX_RESOLVED_ENCODERS and cls not in _RESOLVED_ENCODERS:
        del _RESOLVED_ENCODERS[next(iter(_RESOLVED_ENCODERS))]
    _RESOLVED_ENCODERS[cls] = resolved
    return resolved


def register_encoder(type_: type[Any], encoder: Callable[[Any], Any]) -> None:
    """Register a process-level JSON encoder for `type_` and its subclasses.

    `encoder` receives one instance and must return a JSON-able value. It is
    consulted after the exact-type fast paths, resolved via an MRO walk so
    subclasses of `type_` are covered, and overrides any built-in handler for
    the same type.

    Usage::

        register_encoder(MyId, lambda v: v.hex)
    """
    if not isinstance(type_, type):
        raise TypeError("register_encoder requires a type as its first argument")
    if not callable(encoder):
        raise TypeError("register_encoder requires a callable encoder")
    _REGISTERED_ENCODERS[type_] = encoder
    _RESOLVED_ENCODERS.clear()


def unregister_encoder(type_: type[Any]) -> None:
    """Remove a previously registered encoder for `type_`.

    No-op if `type_` was never registered.
    """
    _REGISTERED_ENCODERS.pop(type_, None)
    _RESOLVED_ENCODERS.clear()


def _resolve_custom(
    obj: Any, custom_encoder: dict[type[Any], Callable[[Any], Any]]
) -> Callable[[Any], Any] | None:
    """Resolve a per-call `custom_encoder` entry for `obj`.

    Exact `type(obj)` first, then an `isinstance` scan whose insertion order is
    the documented tie-break when two registered bases both match.
    """
    encoder = custom_encoder.get(type(obj))
    if encoder is not None:
        return encoder
    for encoder_type, fn in custom_encoder.items():
        if isinstance(obj, encoder_type):
            return fn
    return None


def _encode_shared_scalar(obj: Any) -> Any:
    """Encode the four scalars both JSON paths convert identically, else `_MISSING`.

    Shared by `jsonable_encoder` and `orjson_default` so the Path/Decimal/
    timedelta/bytes conversions stay in lockstep. The four types are disjoint,
    so the probe order is immaterial.
    """
    if isinstance(obj, Path):
        return str(obj)
    if isinstance(obj, decimal.Decimal):
        return _decimal_to_json(obj)
    if isinstance(obj, datetime.timedelta):
        return obj.total_seconds()
    if isinstance(obj, (bytes, bytearray)):
        # OpenAPI `format: byte`: a UTF-8 `.decode()` would substitute U+FFFD.
        return base64.b64encode(obj).decode("ascii")
    return _MISSING


def _public_vars(obj: Any) -> dict[str, Any]:
    """Return an object's `__dict__` minus private (underscore) attributes.

    Keeps the unknown-object fallback from leaking library bookkeeping such as
    `_sa_instance_state`; a class opts back in with
    `__json_include_private__ = True`. `TypeError` propagates for a slots-only
    object so the caller's `str(obj)` fallback still fires.
    """
    ns = vars(obj)
    if getattr(obj, "__json_include_private__", False):
        return ns
    return {k: v for k, v in ns.items() if not (isinstance(k, str) and k.startswith("_"))}


def _encode_seq(
    items: Any,
    *,
    include: Any,
    exclude: Any,
    exclude_unset: bool,
    exclude_defaults: bool,
    exclude_none: bool,
    custom_encoder: Any,
    _seen: set[int] | None,
) -> list[Any]:
    """Recurse `jsonable_encoder` over each element of a sequence.

    Shared by the list/tuple/set/deque/generator branches. Every recursion site
    passes the six filters positionally to skip CPython's per-argument keyword
    match - worth 1-3% on encode-heavy payloads, so the spelling is not cosmetic.
    """
    return [
        jsonable_encoder(
            item,
            include,
            exclude,
            exclude_unset,
            exclude_defaults,
            exclude_none,
            custom_encoder,
            _seen=_seen,
        )
        for item in items
    ]


def jsonable_encoder(
    obj: Any,
    include: set[str] | None = None,
    exclude: set[str] | None = None,
    exclude_unset: bool = False,
    exclude_defaults: bool = False,
    exclude_none: bool = False,
    custom_encoder: dict[type[Any], Callable[[Any], Any]] | None = None,
    *,
    _seen: set[int] | None = None,
) -> Any:
    """Convert complex objects to JSON-serializable types.

    Handles Pydantic models, dataclasses, datetime, Decimal, UUID, Enum, Path,
    sets, frozensets, and nested structures.

    Every filter applies at **every depth**, not only to the top-level object:
    `exclude={"password"}` strips that key wherever it appears, and
    `exclude_unset` / `exclude_defaults` reach a model nested in a dict or list.

    `include` is the same rule read the other way - a whitelist of key names
    applied at every depth, not a selection of top-level fields. A nesting key
    must be listed too, or the branch holding the value is dropped:
    `include={"a"}` gives `{"a": {}}` where `include={"a", "b"}` keeps `a`'s
    `b`. Pydantic's `model_dump(include=...)` selects at one level instead.

    Raises `ValueError` on a self-referential object graph rather than recursing
    until the stack overflows. The per-call `_seen` set is internal.

    `custom_encoder` is an optional `{type: fn}` mapping consulted before every
    built-in rule at every depth - exact `type(obj)` first, else the first
    `isinstance` match in insertion order - so it can override container and
    model handling too. `register_encoder` types are consulted later and cover
    subclasses through an MRO walk.

    Usage::

        data = jsonable_encoder(my_pydantic_model, exclude={"password"})
    """
    # Checked before `custom_encoder`, so a caller cannot register the guard away.
    if isinstance(obj, Secret):
        _reject_secret()

    # Win over every built-in rule, at every depth; guarded to cost nothing.
    if custom_encoder is not None:
        fn = _resolve_custom(obj, custom_encoder)
        if fn is not None:
            return fn(obj)

    # Probed first so a registered type overrides a default handler, and reused
    # below so the walk runs once; `_MISSING` means "registry empty, not walked".
    resolved_encoder: Callable[[Any], Any] | None | object = _MISSING
    if _REGISTERED_ENCODERS:
        resolved_encoder = _resolve_encoder(type(obj))
        if resolved_encoder is not None:
            return resolved_encoder(obj)

    # One hash lookup for None, str, int, float, bool, UUID, Decimal and dates.
    encoder = _LEAF_TYPE_ENCODERS.get(type(obj))
    if encoder is not None:
        return encoder(obj)

    # Subclass-tolerant scalar fallthroughs.
    if isinstance(obj, enum.Enum):
        return obj.value
    scalar = _encode_shared_scalar(obj)
    if scalar is not _MISSING:
        return scalar
    if isinstance(obj, (datetime.datetime, datetime.date, datetime.time)):
        return obj.isoformat()
    if isinstance(obj, uuid.UUID):
        return str(obj)

    # Catches the scalar table and subclasses the fast paths above miss.
    if resolved_encoder is _MISSING:
        resolved_encoder = _resolve_encoder(type(obj))
    if resolved_encoder is not None:
        return resolved_encoder(obj)  # type: ignore[operator]

    # Only containers can cycle, so the seen-set is allocated lazily.
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
            # `model_dump` filters the model's own fields; a nested dict or model
            # is re-encoded here, and "at every depth" must reach it too.
            return jsonable_encoder(
                obj.model_dump(**kwargs),
                include,
                exclude,
                exclude_unset,
                exclude_defaults,
                exclude_none,
                custom_encoder,
                _seen=_seen,
            )

        if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
            return jsonable_encoder(
                dataclasses.asdict(obj),
                include,
                exclude,
                exclude_unset,
                exclude_defaults,
                exclude_none,
                custom_encoder,
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
                # Forwarded, so a nested dict honours the filters too.
                result[str_key] = jsonable_encoder(
                    value,
                    include,
                    exclude,
                    exclude_unset,
                    exclude_defaults,
                    exclude_none,
                    custom_encoder,
                    _seen=_seen,
                )
            return result

        # Only a set needs ordering, which is what makes its output deterministic.
        if isinstance(obj, (list, tuple, deque, GeneratorType)):
            items: Any = obj
        elif isinstance(obj, (set, frozenset)):
            items = sorted(obj, key=str)
        else:
            items = None
        if items is not None:
            return _encode_seq(
                items,
                include=include,
                exclude=exclude,
                exclude_unset=exclude_unset,
                exclude_defaults=exclude_defaults,
                exclude_none=exclude_none,
                custom_encoder=custom_encoder,
                _seen=_seen,
            )

        try:
            return jsonable_encoder(
                _public_vars(obj),
                include,
                exclude,
                exclude_unset,
                exclude_defaults,
                exclude_none,
                custom_encoder,
                _seen=_seen,
            )
        except TypeError:
            return str(obj)
    finally:
        _seen.discard(obj_id)


def orjson_default(obj: Any) -> Any:
    """Single-object fallback for the JSON backend's `default=` hook.

    The backend encodes the common leaf types at C speed and calls this only
    for a leaf it cannot handle, so this converts ONE object and lets the
    backend recurse into the result; it does not walk the whole graph.

    A set becomes a list of raw items the backend re-enters this hook for;
    Path/Decimal/timedelta map to their scalar form; bytes encode as base64; a
    model is dumped through `model_dump` so its computed fields survive. Anything
    else falls back to `vars(obj)` then `str(obj)`, as `jsonable_encoder` does.
    """
    if isinstance(obj, Secret):
        _reject_secret()
    # First, so a registered type is honoured on this path too.
    if _REGISTERED_ENCODERS:
        encoder = _resolve_encoder(type(obj))
        if encoder is not None:
            return encoder(obj)
    if isinstance(obj, (set, frozenset)):
        return sorted(obj, key=str)
    scalar = _encode_shared_scalar(obj)
    if scalar is not _MISSING:
        return scalar
    # A subclass the backend cannot encode resolves via its base, not `vars()` (`{}`).
    encoder = _resolve_encoder(type(obj))
    if encoder is not None:
        return encoder(obj)
    if isinstance(obj, deque):
        return list(obj)
    if isinstance(obj, GeneratorType):
        return list(obj)
    # Before `vars()`, which drops computed fields. Python mode, not json mode.
    if isinstance(obj, BaseModel):
        return obj.model_dump()
    # `vars()` on a slotted `Struct` raises, so it would arrive as a repr.
    if is_msgspec_struct(type(obj)):
        return struct_to_dict(obj)
    try:
        return _public_vars(obj)
    except TypeError:
        pass
    return str(obj)
