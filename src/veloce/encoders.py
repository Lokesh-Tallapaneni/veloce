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

# orjson serializes a Python int only within the signed/unsigned 64-bit
# window; outside `-(2**63) <= i < 2**64` it raises "Integer exceeds 64-bit
# range". An integer-valued Decimal beyond this is emitted as a string.
_ORJSON_INT_MIN = -(2**63)
_ORJSON_INT_MAX = 2**64

# Raised by both JSON entry points when a Secret reaches the serialiser; kept as
# one literal so the two guard sites cannot drift.
_SECRET_REJECTED = "Secret must not be serialized to JSON; call .reveal() explicitly"


def _reject_secret() -> None:
    """Raise the shared `TypeError` for a Secret that reached the serialiser."""
    raise TypeError(_SECRET_REJECTED)


def _decimal_to_json(obj: decimal.Decimal) -> int | float | str:
    """Encode a Decimal preserving integer-valued ones as JSON integers.

    An integer-valued Decimal (`as_tuple().exponent >= 0`) is emitted as an
    `int`, so `Decimal('1')` is `1` not `1.0` and a large whole number keeps
    its exact digits instead of losing precision through IEEE-754. Integers
    outside orjson's 64-bit window fall back to `str` (lossless and dumpable).
    Fractional decimals encode as `float`; `NaN`/`Infinity` and out-of-range
    fractional magnitudes (whose `float` is non-finite) fall back to `str`.
    """
    parts = obj.as_tuple()
    exponent = parts.exponent
    # `exponent` is an int for finite decimals, or 'n'/'N'/'F' for NaN/sNaN/Inf.
    if isinstance(exponent, int) and exponent >= 0:
        # The integer has `len(digits) + exponent` decimal digits. orjson's
        # 64-bit window tops out at 2**64 (20 digits), so anything wider is
        # certainly out of range - return `str` WITHOUT materializing the int,
        # so an attacker-supplied `1E1000000` can't force a million-digit alloc.
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
    decimal.Decimal: _decimal_to_json,
    datetime.datetime: lambda v: v.isoformat(),
    datetime.date: lambda v: v.isoformat(),
    datetime.time: lambda v: v.isoformat(),
    datetime.timedelta: lambda v: v.total_seconds(),
}


# Exact-type table for scalars that are effectively final (never subclassed
# in practice) and would otherwise leak internals via the `vars(obj)`
# fallback (e.g. `ipaddress` networks carry a `_prefixlen` dict). Consulted
# only after the leaf-table miss so the hot path stays zero-cost.
_SCALAR_TYPE_ENCODERS: dict[type, Callable[[Any], Any]] = {
    re.Pattern: lambda p: p.pattern,
    ipaddress.IPv4Address: str,
    ipaddress.IPv6Address: str,
    ipaddress.IPv4Interface: str,
    ipaddress.IPv6Interface: str,
    ipaddress.IPv4Network: str,
    ipaddress.IPv6Network: str,
}


# Process-level user registry, consulted by the MRO resolver before the
# built-in scalar tables so a registered type wins over a built-in handler
# for the same class. Empty by default, so the common path never touches it.
_REGISTERED_ENCODERS: dict[type, Callable[[Any], Any]] = {}

# Built-in leaf and scalar tables merged once at import. Their keys are
# disjoint, so the merge preserves every entry and lets the MRO walk probe a
# single dict per base instead of chaining two `.get(...)` calls. The user
# registry is probed separately (and only when non-empty) so it keeps winning
# over a built-in handler for the same class.
_BUILTIN_TYPE_ENCODERS: dict[type, Callable[[Any], Any]] = {
    **_LEAF_TYPE_ENCODERS,
    **_SCALAR_TYPE_ENCODERS,
}

# Distinguishes "no encoder cached" from a legitimately cached `None` (an
# unknown type that matched no base), so the memo can be probed with a single
# `get` instead of a membership test plus a subscript.
_MISSING = object()

# Memoizes the MRO walk per concrete type. A scalar subclass (`class MyInt(int)`)
# resolves to its base encoder once, then every later instance is a single dict
# hit. Cleared whenever the user registry changes so a late `register_encoder`
# cannot be shadowed by a stale cached miss.
_RESOLVED_ENCODERS: dict[type, Callable[[Any], Any] | None] = {}

# Cap the resolve cache so an app that mints many distinct runtime classes (a
# hot-reloader, per-tenant dynamic models) cannot grow it without bound. A normal
# app encodes a small, fixed set of types and never reaches the cap, so reads
# stay a plain dict hit; past the cap the oldest entry is dropped (FIFO).
_MAX_RESOLVED_ENCODERS = 4096


def _resolve_encoder(cls: type) -> Callable[[Any], Any] | None:
    """Find the encoder for `cls` by walking its MRO, memoizing the result.

    Consulted only after the exact-type fast paths miss. Walks
    `cls.__mro__` and returns the first base present in the user registry,
    then the leaf table, then the scalar table - so a registered base wins
    over a built-in one, and a subclass of `int`/`str`/`float`/`Decimal`/...
    encodes via its base instead of falling through to `vars(obj)`. A `None`
    result (no base matched) is cached too, so repeated unknown types do not
    re-walk the MRO.
    """
    cached = _RESOLVED_ENCODERS.get(cls, _MISSING)
    if cached is not _MISSING:
        return cached  # type: ignore[return-value]
    resolved: Callable[[Any], Any] | None = None
    # The registry (when non-empty) must win over the built-in tables for the
    # same base, so probe it first; otherwise the merged built-in map answers
    # in one lookup per base.
    registry = _REGISTERED_ENCODERS
    for base in cls.__mro__:
        encoder = registry.get(base) if registry else None
        if encoder is None:
            encoder = _BUILTIN_TYPE_ENCODERS.get(base)
        if encoder is not None:
            resolved = encoder
            break
    if len(_RESOLVED_ENCODERS) >= _MAX_RESOLVED_ENCODERS and cls not in _RESOLVED_ENCODERS:
        # FIFO eviction keeps memory bounded under dynamic class creation; the
        # dropped type simply re-walks its MRO on a later encode.
        del _RESOLVED_ENCODERS[next(iter(_RESOLVED_ENCODERS))]
    _RESOLVED_ENCODERS[cls] = resolved
    return resolved


def register_encoder(type_: type, encoder: Callable[[Any], Any]) -> None:
    """Register a process-level JSON encoder for `type_` and its subclasses.

    `encoder` receives one instance and must return a JSON-able value
    (str/int/float/bool/None or a list/dict of such). It is consulted by
    `jsonable_encoder` after the exact-type fast paths, resolved via an MRO
    walk so subclasses of `type_` are covered too. Registering a type that
    already has a built-in handler overrides that handler for the type and
    its subclasses.

    Usage::

        register_encoder(MyId, lambda v: v.hex)
    """
    if not isinstance(type_, type):
        raise TypeError("register_encoder requires a type as its first argument")
    if not callable(encoder):
        raise TypeError("register_encoder requires a callable encoder")
    _REGISTERED_ENCODERS[type_] = encoder
    _RESOLVED_ENCODERS.clear()


def unregister_encoder(type_: type) -> None:
    """Remove a previously registered encoder for `type_`.

    No-op if `type_` was never registered.
    """
    _REGISTERED_ENCODERS.pop(type_, None)
    _RESOLVED_ENCODERS.clear()


def _resolve_custom(
    obj: Any, custom_encoder: dict[type, Callable[[Any], Any]]
) -> Callable[[Any], Any] | None:
    """Resolve a per-call `custom_encoder` entry for `obj`.

    Resolution order: exact `type(obj)` first, then an insertion-order
    `isinstance` scan returning the first matching entry. Insertion order is
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
    timedelta/bytes conversions stay in lockstep. Returns `_MISSING` when `obj`
    is none of those four, letting each caller fall through to its own branches.
    The four target types are mutually disjoint, so the probe order is immaterial.
    """
    if isinstance(obj, Path):
        return str(obj)
    if isinstance(obj, decimal.Decimal):
        return _decimal_to_json(obj)
    if isinstance(obj, datetime.timedelta):
        return obj.total_seconds()
    if isinstance(obj, (bytes, bytearray)):
        # Lossless base64 (the JSON-canonical representation, matching OpenAPI/
        # JSON Schema `format: byte`). A UTF-8 `.decode()` would substitute
        # U+FFFD for any non-UTF-8 byte, corrupting image headers, hash digests,
        # gzip blobs, etc.; base64 round-trips every byte.
        return base64.b64encode(obj).decode("ascii")
    return _MISSING


def _public_vars(obj: Any) -> dict[str, Any]:
    """Return an object's `__dict__` minus private (underscore) attributes.

    Confines the filter to the structurally-derived namespace so the
    unknown-object fallback never leaks ORM/library bookkeeping such as
    `_sa_instance_state`. An object may opt back in to including private
    attributes by setting `__json_include_private__ = True` on its class.
    Re-raises `TypeError` for slots-only objects so the caller's
    `str(obj)` fallback still fires.
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
    exclude_none: bool,
    custom_encoder: Any,
    _seen: set[int] | None,
) -> list[Any]:
    """Recurse `jsonable_encoder` over each element of a sequence.

    Shared by the list/tuple/set/deque/generator branches of
    `jsonable_encoder` so the identical per-element recursion lives in one
    place instead of five copies.
    """
    return [
        jsonable_encoder(
            item,
            include=include,
            exclude=exclude,
            exclude_none=exclude_none,
            custom_encoder=custom_encoder,
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
    custom_encoder: dict[type, Callable[[Any], Any]] | None = None,
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

    `custom_encoder` is an optional `{type: fn}` mapping consulted before
    every built-in rule at every depth: the exact `type(obj)` wins, else
    the entries are scanned in insertion order returning the first
    `isinstance` match. Because it runs first it can override container and
    model handling as well as leaf scalars. Types registered process-wide
    via `register_encoder` are consulted later (after the exact-type fast
    paths) and cover subclasses through an MRO walk.

    Usage::

        data = jsonable_encoder(my_pydantic_model, exclude={"password"})
    """
    # A Secret must never be serialised to JSON; require an explicit
    # `.reveal()` at the call site. Checked before custom_encoder so a
    # caller cannot accidentally register the guard away.
    if isinstance(obj, Secret):
        _reject_secret()

    # Per-call custom encoders win over every built-in rule, at every depth.
    # Guarded so the common (no-custom) path pays nothing.
    if custom_encoder is not None:
        fn = _resolve_custom(obj, custom_encoder)
        if fn is not None:
            return fn(obj)

    # Process-level registry probed before the built-in tables so a registered
    # type (including for a built-in like `datetime`) overrides the default
    # handler. Guarded on the dict being non-empty so the common path - where
    # nothing is registered - pays a single truthiness check and skips the walk.
    # The resolved value is reused by the final MRO-walk gate below so the walk
    # runs at most once per call; `_MISSING` marks "registry empty, not walked".
    resolved_encoder: Callable[[Any], Any] | None | object = _MISSING
    if _REGISTERED_ENCODERS:
        resolved_encoder = _resolve_encoder(type(obj))
        if resolved_encoder is not None:
            return resolved_encoder(obj)

    # Exact-type leaf encoder (covers None, str, int, float, bool, UUID,
    # Decimal, datetime/date/time/timedelta in one hash lookup).
    encoder = _LEAF_TYPE_ENCODERS.get(type(obj))
    if encoder is not None:
        return encoder(obj)

    # Subclass-tolerant scalar fallthroughs (Path -> PosixPath/WindowsPath,
    # subclassed Enum / datetime / Decimal / bytes / bytearray).
    if isinstance(obj, enum.Enum):
        return obj.value
    scalar = _encode_shared_scalar(obj)
    if scalar is not _MISSING:
        return scalar
    if isinstance(obj, (datetime.datetime, datetime.date, datetime.time)):
        return obj.isoformat()
    if isinstance(obj, uuid.UUID):
        return str(obj)

    # MRO-walk resolver: catches the process-level user registry, the scalar
    # table, and scalar subclasses (`class MyInt(int)` -> the `int` encoder)
    # that the exact-type fast paths above miss. Memoized per concrete type.
    # Reuses the registry-gate result above (same `type(obj)`) when it already
    # walked; only walks here when the registry was empty (`_MISSING`).
    if resolved_encoder is _MISSING:
        resolved_encoder = _resolve_encoder(type(obj))
    if resolved_encoder is not None:
        return resolved_encoder(obj)  # type: ignore[operator]

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
            # `model_dump` already honours the filters for the model's own
            # fields, but a field whose value is a plain dict or a nested model
            # is re-encoded here, and the filters must reach it too - that is
            # what "at every depth" means, and dropping `include`/`exclude` left
            # a nested `password` in the output. The dataclass branch below
            # forwards all three; this one now agrees.
            return jsonable_encoder(
                obj.model_dump(**kwargs),
                include=include,
                exclude=exclude,
                exclude_none=exclude_none,
                custom_encoder=custom_encoder,
                _seen=_seen,
            )

        if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
            return jsonable_encoder(
                dataclasses.asdict(obj),
                include=include,
                exclude=exclude,
                exclude_none=exclude_none,
                custom_encoder=custom_encoder,
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
                    value,
                    include=include,
                    exclude=exclude,
                    exclude_none=exclude_none,
                    custom_encoder=custom_encoder,
                    _seen=_seen,
                )
            return result

        # The four sequence branches recurse identically per element; only the
        # iterable form differs (sets sort for deterministic output). They share
        # the module-level `_encode_seq` helper so the shared body costs no
        # per-call closure allocation.
        # Every sequence kind encodes the same way; only a set needs ordering
        # first, and `sorted(obj, key=str)` is what keeps its output
        # deterministic across runs.
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
                exclude_none=exclude_none,
                custom_encoder=custom_encoder,
                _seen=_seen,
            )

        # Fallback: try to convert to dict
        try:
            return jsonable_encoder(
                _public_vars(obj),
                include=include,
                exclude=exclude,
                custom_encoder=custom_encoder,
                _seen=_seen,
            )
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
    to their scalar form. bytes/bytearray encode as lossless base64 to stay
    consistent with `jsonable_encoder`. A model is dumped through `model_dump`,
    so its computed fields survive. Anything else falls back to `vars(obj)`,
    then `str(obj)` - matching `jsonable_encoder`'s last-resort behaviour so the
    two paths agree on the same conversions.
    """
    if isinstance(obj, Secret):
        _reject_secret()
    # Process-level registry first so a registered type (incl. one shadowing a
    # built-in handled below) is honoured on the orjson default-hook path too.
    # Guarded on a non-empty dict so the common path skips the MRO walk.
    if _REGISTERED_ENCODERS:
        encoder = _resolve_encoder(type(obj))
        if encoder is not None:
            return encoder(obj)
    if isinstance(obj, (set, frozenset)):
        return sorted(obj, key=str)
    scalar = _encode_shared_scalar(obj)
    if scalar is not _MISSING:
        return scalar
    # MRO walk (registry -> leaf -> scalar) so a subclass orjson cannot encode
    # natively - e.g. a third-party `datetime`/`float` subclass - still resolves
    # via its base instead of falling through to the `vars()` fallback (`{}`).
    encoder = _resolve_encoder(type(obj))
    if encoder is not None:
        return encoder(obj)
    if isinstance(obj, deque):
        return list(obj)
    if isinstance(obj, GeneratorType):
        return list(obj)
    # Before the `vars()` fallback, which sees only stored fields: a model's
    # computed fields are part of its serialisation contract, and dropping them
    # here made the same model encode differently depending on whether it
    # reached `jsonable_encoder` or this hook. `model_dump()` in python mode
    # rather than json mode, so orjson still encodes the leaves it handles
    # natively and only re-enters this hook for the ones it cannot.
    if isinstance(obj, BaseModel):
        return obj.model_dump()
    # The other supported model backend, for the same reason: `vars()` on a
    # slotted `Struct` raises, so one would otherwise reach `str(obj)` and
    # arrive as a Python repr. Dead when msgspec is not installed.
    if is_msgspec_struct(type(obj)):
        return struct_to_dict(obj)
    try:
        return _public_vars(obj)
    except TypeError:
        pass
    return str(obj)
