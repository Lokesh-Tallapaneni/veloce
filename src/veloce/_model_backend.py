"""Model-backend detection - Pydantic vs msgspec.

Detection is registration-time on the request side (a plan slot is tagged with
the backend once) and a single `isinstance` on the response side. msgspec is an
optional `[fast]` extra: it is probed once at import and never required, so with
it absent every msgspec branch is dead and behaviour is identical to today.
"""

from __future__ import annotations

from enum import IntEnum
from typing import Any

from pydantic import BaseModel as _PydanticModel

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


def is_pydantic_model(tp: Any) -> bool:
    """Return True when `tp` is a `pydantic.BaseModel` subclass."""
    return isinstance(tp, type) and issubclass(tp, _PydanticModel)


def is_msgspec_struct(tp: Any) -> bool:
    """Return True when `tp` is a `msgspec.Struct` subclass (and msgspec is installed)."""
    # `_HAS_MSGSPEC` short-circuits first, so `_MSGSPEC_STRUCT` is never None here.
    return _HAS_MSGSPEC and isinstance(tp, type) and issubclass(tp, _MSGSPEC_STRUCT)


def backend_of(tp: Any) -> ModelBackend:
    """Classify a single, already-unwrapped type. Registration-time only.

    A class cannot subclass both `BaseModel` and `msgspec.Struct` (their
    metaclasses conflict), so the Pydantic-first order is for clarity, not
    correctness.
    """
    if is_pydantic_model(tp):
        return ModelBackend.PYDANTIC
    if is_msgspec_struct(tp):
        return ModelBackend.MSGSPEC
    return ModelBackend.NONE
