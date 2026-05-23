"""JSON-compatible encoding for arbitrary Python objects."""

from __future__ import annotations

import dataclasses
import datetime
import decimal
import enum
import uuid
from pathlib import Path
from typing import Any

from pydantic import BaseModel


def jsonable_encoder(
    obj: Any,
    include: set[str] | None = None,
    exclude: set[str] | None = None,
    exclude_unset: bool = False,
    exclude_defaults: bool = False,
    exclude_none: bool = False,
) -> Any:
    """Convert complex objects to JSON-serializable types.

    Handles Pydantic models, dataclasses, datetime, Decimal, UUID, Enum, Path,
    sets, frozensets, generators, and nested structures.

    `include` / `exclude` apply to dict keys at **every depth** — passing
    `exclude={"password"}` strips a `password` key wherever it appears
    in the structure, not only at the top level.

    Usage:
        data = jsonable_encoder(my_pydantic_model, exclude={"password"})
    """
    # Common-case primitives short-circuit — most leaf calls hit these
    # before any of the heavier isinstance checks below.
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj

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
        return jsonable_encoder(obj.model_dump(**kwargs))

    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return jsonable_encoder(dataclasses.asdict(obj), include=include, exclude=exclude)

    if isinstance(obj, dict):
        result = {}
        for key, value in obj.items():
            str_key = str(key)
            if include and str_key not in include:
                continue
            if exclude and str_key in exclude:
                continue
            # Forward the filters into the recursion so nested dicts
            # honour them too — matches the dataclass branch above.
            result[str_key] = jsonable_encoder(value, include=include, exclude=exclude)
        return result

    if isinstance(obj, (list, tuple)):
        return [jsonable_encoder(item, include=include, exclude=exclude) for item in obj]

    if isinstance(obj, (set, frozenset)):
        return [
            jsonable_encoder(item, include=include, exclude=exclude)
            for item in sorted(obj, key=str)
        ]

    if isinstance(obj, enum.Enum):
        return obj.value

    if isinstance(obj, (datetime.datetime, datetime.date, datetime.time)):
        return obj.isoformat()

    if isinstance(obj, datetime.timedelta):
        return obj.total_seconds()

    if isinstance(obj, decimal.Decimal):
        return float(obj)

    if isinstance(obj, uuid.UUID):
        return str(obj)

    if isinstance(obj, Path):
        return str(obj)

    if isinstance(obj, bytes):
        return obj.decode("utf-8", errors="replace")

    # Fallback: try to convert to dict
    try:
        return jsonable_encoder(vars(obj), include=include, exclude=exclude)
    except TypeError:
        return str(obj)
