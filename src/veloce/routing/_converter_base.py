"""Converter base and the simple segment converters — string, number, UUID, path, any.

The `Converter` contract and the converters whose match is a single check on one
segment. The temporal and decimal ones live in `_converter_temporal`; the
registry, spec parsing and regex-path classification live in `routing.converters`,
which re-exports everything here under its historical path.
"""

from __future__ import annotations

import math
import re
import uuid
import warnings
from typing import Any

from veloce._internal import _require_methods

# RFC 4122, un-anchored so it is reusable as a group in the regex fallback.
_UUID_PATTERN = (
    r"[0-9a-fA-F]{8}-"
    r"[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{12}"
)

# Anchored for whole-segment validation; one source, so the two cannot drift.
_UUID_RE = re.compile(f"^{_UUID_PATTERN}$")

# Fragments for the regex fallback only; the radix fast path never reads them.
_BUILTIN_REGEX: dict[str, str] = {
    "str": r"[^/]+",
    "string": r"[^/]+",
    "int": r"-?\d+",
    # A superset of `FloatConverter.match`, since a stricter fragment would 404
    # what the tree matches. The dot stays required to exclude `int` values.
    "float": r"[+-]?(?:\d+\.\d*|\.\d+)",
    "uuid": _UUID_PATTERN,
    "path": r".+",
    # Each is a SUPERSET of its converter, which re-validates the matched group.
    "date": r"\d{4}-\d{2}-\d{2}",
    "datetime": r"\d{4}-\d{2}-\d{2}[T ][\d:.]+(?:[+-][\d:]+|Z)?",
    "time": r"\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:[+-][\d:]+|Z)?",
    "timedelta": r"P(?:\d+D)?(?:T(?:\d+H)?(?:\d+M)?(?:\d+(?:\.\d+)?S)?)?",
    "decimal": r"[+-]?\d+(?:\.\d+)?",
}

# A bignum-DoS guard on arbitrary-precision coercion, not a 64-bit range check.
_MAX_INT_DIGITS = 20


# ── Converters ────────────────────────────────────────────


class Converter:
    """Base class - subclasses override `match` and may set `greedy`."""

    __slots__ = ()
    greedy = False

    #: How restrictive this converter is: the lower value is tried first when two
    #: parameter segments compete, so `/items/{id:int}` beats `/items/{slug:str}`
    #: however declared. On the converter, not in a router table that would tie
    #: an unlisted one with `str` and make resolution depend on decorator order.
    specificity = 50

    #: Checked at definition: a converter that cannot match silently 404s.
    _required = ("match",)

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        _require_methods(cls, Converter, Converter._required)
        # Warned, not raised, so an older third-party converter still imports.
        if "__slots__" not in cls.__dict__:
            warnings.warn(
                f"{cls.__name__} does not declare __slots__; add `__slots__ = ()` "
                "so its instances stay slotted like every other Converter",
                stacklevel=2,
            )

    def match(self, value: str) -> tuple[bool, Any]:
        """Validate (and coerce) `value`. Return (ok, coerced)."""
        raise NotImplementedError


class StringConverter(Converter):
    """Default. Accepts a single non-empty segment (slashes excluded).

    Optional constraints bound the segment length so a violation is a route
    miss, not a handler-layer error: `{code:str(length=2)}` is an exact length
    and `{slug:str(minlength=3,maxlength=64)}` an inclusive range. Zero-arg use
    keeps the unbounded fast path.
    """

    __slots__ = ("_minlength", "_maxlength")
    #: Any single non-empty segment - the baseline.
    specificity = 50

    _minlength: int
    _maxlength: int | None

    def __init__(
        self,
        length: int | None = None,
        minlength: int | None = None,
        maxlength: int | None = None,
    ) -> None:
        if length is not None:
            if minlength is not None or maxlength is not None:
                raise ValueError("str converter: length cannot combine with minlength/maxlength")
            if length < 1:
                raise ValueError("str converter: length must be >= 1")
            self._minlength = self._maxlength = length
            return
        lo = 1 if minlength is None else minlength
        if lo < 1:
            raise ValueError("str converter: minlength must be >= 1")
        if maxlength is not None and maxlength < lo:
            raise ValueError("str converter: maxlength must be >= minlength")
        self._minlength = lo
        self._maxlength = maxlength

    def match(self, value: str) -> tuple[bool, Any]:
        """Accept the segment when its length satisfies the configured bounds."""
        # The splitter strips empty segments, so one cannot arrive here.
        length = len(value)
        if length < self._minlength:
            return False, None
        if self._maxlength is not None and length > self._maxlength:
            return False, None
        return True, value


class IntConverter(Converter):
    """Matches a decimal integer; coerces to Python int.

    Optional constraints participate in matching rather than the handler layer,
    so `{page:int(min=1)}` makes `0` a route miss. `signed=False` forbids the
    leading `-`; zero-arg use keeps the unbounded fast path.
    """

    __slots__ = ("_min", "_max", "_signed")
    #: Digits only, optionally signed.
    specificity = 20

    def __init__(
        self,
        min: int | None = None,
        max: int | None = None,
        *,
        signed: bool = True,
    ) -> None:
        if min is not None and max is not None and max < min:
            raise ValueError("int converter: max must be >= min")
        self._min = min
        self._max = max
        self._signed = signed

    def match(self, value: str) -> tuple[bool, Any]:
        """Accept a decimal-integer segment within the configured bounds; coerce to `int`."""
        if not value or len(value) > _MAX_INT_DIGITS:
            return False, None
        if value[0] == "-":
            if not self._signed:
                return False, None
            check = value[1:]
        else:
            check = value
        if not check or not check.isdigit():
            return False, None
        coerced = int(value)
        if self._min is not None and coerced < self._min:
            return False, None
        if self._max is not None and coerced > self._max:
            return False, None
        return True, coerced


class FloatConverter(Converter):
    """Matches a decimal float (no scientific notation); coerces to float.

    Optional `min`/`max` bound the value during matching and `signed=False`
    forbids the leading `-`. Zero-arg use keeps the unbounded fast path.
    """

    __slots__ = ("_min", "_max", "_signed")
    #: Digits with a required fractional part - a strict subset of `decimal`,
    #: so it is the more restrictive of the two and is tried first.
    specificity = 40

    def __init__(
        self,
        min: float | None = None,
        max: float | None = None,
        *,
        signed: bool = True,
    ) -> None:
        if min is not None and max is not None and max < min:
            raise ValueError("float converter: max must be >= min")
        self._min = min
        self._max = max
        self._signed = signed

    def match(self, value: str) -> tuple[bool, Any]:
        """Accept a decimal-float segment within the configured bounds; coerce to `float`."""
        if not value:
            return False, None
        # A required `.` keeps this distinct from `int`.
        if "." not in value:
            return False, None
        if "e" in value or "E" in value:
            return False, None
        if not self._signed and value[0] == "-":
            return False, None
        try:
            f = float(value)
        except ValueError:
            return False, None
        if math.isnan(f) or math.isinf(f):
            return False, None
        if self._min is not None and f < self._min:
            return False, None
        if self._max is not None and f > self._max:
            return False, None
        return True, f


class UUIDConverter(Converter):
    """Matches a canonical UUID per RFC 4122; coerces to uuid.UUID."""

    __slots__ = ()
    #: A fixed 36-character format; almost nothing else matches.
    specificity = 10

    def match(self, value: str) -> tuple[bool, Any]:
        """Accept a canonical RFC 4122 UUID segment; coerce to `uuid.UUID`."""
        if len(value) != 36 or not _UUID_RE.match(value):
            return False, None
        try:
            return True, uuid.UUID(value)
        except ValueError:
            return False, None


class PathConverter(Converter):
    """Greedy converter - consumes the rest of the URL, including slashes."""

    __slots__ = ()
    #: Greedy: consumes the rest of the URL. Always tried last.
    specificity = 90
    greedy = True

    def match(self, value: str) -> tuple[bool, Any]:
        """Accept any non-empty remainder of the URL, slashes included."""
        if not value:
            return False, None
        return True, value


class AnyConverter(Converter):
    """Matches one of a fixed set of literal values: `{x:any(red,blue)}`."""

    __slots__ = ("_choices",)
    #: An explicit set of literals - only those values match.
    specificity = 25

    def __init__(self, choices: tuple[str, ...]) -> None:
        self._choices = frozenset(choices)

    def match(self, value: str) -> tuple[bool, Any]:
        """Accept the segment only when it is one of the declared literal values."""
        return (value in self._choices), value
