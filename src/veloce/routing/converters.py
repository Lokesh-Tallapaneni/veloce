"""Path converters — match-time validation and coercion of URL segments.

Two segment syntaxes are accepted: the angle-bracket form
(`<int:id>`, `<path:p>`, `<any(a,b):x>`) and the brace form
(`{id:int}`, `{p:path}`). Both map to the same converter set.

A converter:
  - `match(segment)` returns `(ok, coerced_value)`.
  - `greedy` is True for the `path` converter which consumes the remainder
    of the URL (including slashes) instead of one segment.

When a route declares `{name:converter}`, the radix node holding that param
keeps the converter and applies it during the match traversal. A segment
the converter rejects causes the router to fall through to the next child
candidate (next param or wildcard) - which means a typed mismatch is a
**route miss**, not a 422.
"""

from __future__ import annotations

import datetime
import decimal
import math
import re
import uuid
from typing import Any

# UUID format per RFC 4122 (8-4-4-4-12 hex digits, case-insensitive). This is
# the body (un-anchored) form, reused when translating a `{id:uuid}` placeholder
# into a named regex group for the hybrid router's regex fallback.
_UUID_PATTERN = (
    r"[0-9a-fA-F]{8}-"
    r"[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{12}"
)

# Anchored form for whole-segment validation. Derived from the single
# `_UUID_PATTERN` source so the two cannot drift.
_UUID_RE = re.compile(f"^{_UUID_PATTERN}$")

# Un-anchored regex fragments for the built-in converters. A bare `{name}`
# (no converter) and the `str`/`string` converters both match one non-slash
# segment. `path` is greedy and crosses slashes. These feed the regex
# fallback only - the radix fast path never consults them.
_BUILTIN_REGEX: dict[str, str] = {
    "str": r"[^/]+",
    "string": r"[^/]+",
    "int": r"-?\d+",
    # A superset of what `FloatConverter.match` accepts: an optional sign, and a
    # dot with digits on either side or both. It was `-?\d+\.\d+`, which is
    # *stricter* than the converter, so `+1.5`, `.5` and `5.` were rejected
    # before the converter was consulted - the same route matched on the radix
    # tree and 404'd on the regex fallback. The dot stays required so a float
    # placeholder does not also match what an `int` one would.
    "float": r"[+-]?(?:\d+\.\d*|\.\d+)",
    "uuid": _UUID_PATTERN,
    "path": r".+",
    # Single-segment (no slash) fragments for the regex fallback. Each must be a
    # SUPERSET of what its converter accepts - the converter re-validates the
    # matched group, so a fragment narrower than its converter silently rejects
    # values the converter would have taken, and `_match_regex` moves on to the
    # next route instead.
    "date": r"\d{4}-\d{2}-\d{2}",
    "datetime": r"\d{4}-\d{2}-\d{2}[T ][\d:.]+(?:[+-][\d:]+|Z)?",
    "time": r"\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:[+-][\d:]+|Z)?",
    "timedelta": r"P(?:\d+D)?(?:T(?:\d+H)?(?:\d+M)?(?:\d+(?:\.\d+)?S)?)?",
    "decimal": r"[+-]?\d+(?:\.\d+)?",
}

# Cap int-parse input length to bound adversarial parse cost. The converter
# coerces to Python int (arbitrary precision), so this is a bignum-DoS guard,
# not a 64-bit range check.
_MAX_INT_DIGITS = 20


# ── Converters ────────────────────────────────────────────


class _Converter:
    """Base class - subclasses override `match` and may set `greedy`."""

    __slots__ = ()
    greedy = False

    #: How restrictive this converter is. When two parameter segments compete
    #: for the same position, the lower value is tried first, so
    #: `/items/{id:int}` beats `/items/{slug:str}` however they were declared.
    #:
    #: Declared on the converter rather than in a table the router keeps,
    #: because a table only knows the classes someone remembered to add: six of
    #: the eleven built-ins were missing from it and silently tied with `str`,
    #: which made route resolution depend on the order the decorators appear in
    #: the file. A custom converter that does not declare one is assumed no
    #: more restrictive than `str`, which is the only safe assumption about a
    #: pattern the framework cannot see.
    specificity = 50

    def match(self, value: str) -> tuple[bool, Any]:
        """Validate (and coerce) `value`. Return (ok, coerced)."""
        raise NotImplementedError


class StringConverter(_Converter):
    """Default. Accepts a single non-empty segment (slashes excluded).

    Optional constraints bound the segment length so a violation is a route
    miss, not a handler-layer error: `{code:str(length=2)}` fixes the length,
    while `{slug:str(minlength=3,maxlength=64)}` sets an inclusive range.
    `length` is shorthand for an exact length (sets both bounds). Zero-arg use
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
        # Empty path segments cannot match a {param}; the splitter strips
        # them out, so reaching here with an empty value is unexpected.
        length = len(value)
        if length < self._minlength:
            return False, None
        if self._maxlength is not None and length > self._maxlength:
            return False, None
        return True, value


class IntConverter(_Converter):
    """Matches a decimal integer; coerces to Python int.

    Optional constraints participate in matching rather than the handler
    layer: `{page:int(min=1)}` rejects `0`/negatives as a route miss, and
    `{n:int(min=1,max=100)}` bounds both ends. `signed=False` forbids the
    leading `-` outright. Zero-arg use keeps the unbounded fast path.
    """

    __slots__ = ("_min", "_max", "_signed")
    #: Digits only, optionally signed.
    specificity = 20

    def __init__(
        self,
        min: int | None = None,
        max: int | None = None,
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


class FloatConverter(_Converter):
    """Matches a decimal float (no scientific notation); coerces to float.

    Optional `min`/`max` bound the value during matching and `signed=False`
    forbids the leading `-`. Zero-arg use keeps the unbounded fast path.
    """

    __slots__ = ("_min", "_max", "_signed")
    #: Digits with a required fractional part.
    specificity = 41

    def __init__(
        self,
        min: float | None = None,
        max: float | None = None,
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
        # the float converter rejects "nan"/"inf" and scientific
        # notation; require a '.' to make this a clear float vs int.
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


class UUIDConverter(_Converter):
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


class PathConverter(_Converter):
    """Greedy converter - consumes the rest of the URL, including slashes."""

    __slots__ = ()
    #: Greedy: consumes the rest of the URL. Always tried last.
    specificity = 90
    greedy = True

    def match(self, value: str) -> tuple[bool, Any]:
        """Accept any non-empty remainder of the URL, slashes included."""
        # Reject empty (zero segments left) so static suffixes still win.
        if not value:
            return False, None
        return True, value


# Anchored prefilters keep the try/except parse off the hot path for the
# common reject. The actual validation is delegated to the stdlib
# `fromisoformat` parsers (3.10-compatible after Z normalization).
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}$")
_DATETIME_RE = re.compile(r"\d{4}-\d{2}-\d{2}[T ][\d:.]+(?:[+-][\d:]+|Z)?$")
_TIME_RE = re.compile(r"\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:[+-][\d:]+|Z)?$")
# ISO 8601 duration: at least one component required.
_TIMEDELTA_RE = re.compile(
    r"P(?=\d|T)(?:(\d+)D)?(?:T(?=\d)(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?)?$"
)
# Python's `str(timedelta)` form: `[D day[s], ]H:MM:SS[.ffffff]`. Accepting it
# lets a real `timedelta` round-trip through `url_for`, which reverse-validates
# the reversed value via `converter.match(str(value))`. The day count and the
# hour field are unbounded/variable-width because `timedelta` normalizes only
# minutes/seconds (RFC 8601 covers the ISO form above; this covers stdlib repr).
_TIMEDELTA_STR_RE = re.compile(
    r"(?:(?P<days>-?\d+) days?, )?(?P<hours>\d+):(?P<minutes>\d{2}):(?P<seconds>\d{2}(?:\.\d{1,6})?)$"
)
_DECIMAL_RE = re.compile(r"[+-]?\d+(?:\.\d+)?$")
_MAX_DECIMAL_CHARS = 40


def _normalize_z(value: str) -> str:
    """Replace a trailing `Z` with `+00:00` (3.10 fromisoformat rejects Z)."""
    if value.endswith("Z"):
        return value[:-1] + "+00:00"
    return value


class DateConverter(_Converter):
    """Matches an ISO 8601 date; coerces to datetime.date."""

    __slots__ = ()
    #: A fixed ISO-8601 shape.
    specificity = 31

    def match(self, value: str) -> tuple[bool, Any]:
        """Accept an ISO 8601 date segment; coerce to `datetime.date`."""
        if not _DATE_RE.match(value):
            return False, None
        try:
            return True, datetime.date.fromisoformat(value)
        except ValueError:
            return False, None


class DateTimeConverter(_Converter):
    """Matches an ISO 8601 datetime; coerces to datetime.datetime."""

    __slots__ = ()
    #: A fixed ISO-8601 shape.
    specificity = 30

    def match(self, value: str) -> tuple[bool, Any]:
        """Accept an ISO 8601 datetime segment; coerce to `datetime.datetime`."""
        if not _DATETIME_RE.match(value):
            return False, None
        try:
            return True, datetime.datetime.fromisoformat(_normalize_z(value))
        except ValueError:
            return False, None


class TimeConverter(_Converter):
    """Matches an ISO 8601 time; coerces to datetime.time."""

    __slots__ = ()
    #: A fixed ISO-8601 shape.
    specificity = 32

    def match(self, value: str) -> tuple[bool, Any]:
        """Accept an ISO 8601 time segment; coerce to `datetime.time`."""
        if not _TIME_RE.match(value):
            return False, None
        try:
            return True, datetime.time.fromisoformat(_normalize_z(value))
        except ValueError:
            return False, None


class TimeDeltaConverter(_Converter):
    """Matches an ISO 8601 duration or `str(timedelta)`; coerces to timedelta.

    Stricter than Litestar: a bare number such as `60` is rejected. An ISO
    duration (`P1DT2H`, at least one component) is accepted, as is Python's
    own `str(timedelta)` form (`1:00:00`, `1 day, 2:00:00`) so a real
    `timedelta` round-trips through `url_for`, which reverse-validates the
    reversed value via `converter.match(str(value))`.
    """

    __slots__ = ()
    #: A fixed duration shape.
    specificity = 33

    def match(self, value: str) -> tuple[bool, Any]:
        """Accept an ISO 8601 duration or `str(timedelta)` segment; coerce to `timedelta`."""
        m = _TIMEDELTA_RE.match(value)
        if m is not None:
            days, hours, minutes, seconds = m.groups()
            return True, datetime.timedelta(
                days=int(days) if days else 0,
                hours=int(hours) if hours else 0,
                minutes=int(minutes) if minutes else 0,
                seconds=float(seconds) if seconds else 0,
            )
        # `str(timedelta)` repr: `[D day[s], ]H:MM:SS[.ffffff]`. A negative
        # timedelta renders as e.g. `-1 day, 23:00:00`, so days may be signed
        # while the clock fields stay non-negative; reconstruct via the same
        # constructor, which re-normalizes to the canonical representation.
        sm = _TIMEDELTA_STR_RE.match(value)
        if sm is None:
            return False, None
        return True, datetime.timedelta(
            days=int(sm["days"]) if sm["days"] else 0,
            hours=int(sm["hours"]),
            minutes=int(sm["minutes"]),
            seconds=float(sm["seconds"]),
        )


class DecimalConverter(_Converter):
    """Matches a decimal literal; coerces to decimal.Decimal."""

    __slots__ = ()
    #: Digits with an optional fractional part.
    specificity = 40

    def match(self, value: str) -> tuple[bool, Any]:
        """Accept a decimal-literal segment; coerce to `decimal.Decimal`."""
        if not value or len(value) > _MAX_DECIMAL_CHARS or not _DECIMAL_RE.match(value):
            return False, None
        try:
            return True, decimal.Decimal(value)
        except decimal.InvalidOperation:
            return False, None


class AnyConverter(_Converter):
    """Matches one of a fixed set of literal values: `{x:any(red,blue)}`."""

    __slots__ = ("_choices",)
    #: An explicit set of literals - only those values match.
    specificity = 25

    def __init__(self, choices: tuple[str, ...]) -> None:
        self._choices = frozenset(choices)

    def match(self, value: str) -> tuple[bool, Any]:
        """Accept the segment only when it is one of the declared literal values."""
        return (value in self._choices), value


# ── Registry and parsing ──────────────────────────────────


# Public base-class alias - subclass `Converter` to build a custom one.
Converter = _Converter

_BUILTIN: dict[str, type[_Converter]] = {
    "str": StringConverter,
    "string": StringConverter,
    "int": IntConverter,
    "float": FloatConverter,
    "uuid": UUIDConverter,
    "path": PathConverter,
    "date": DateConverter,
    "datetime": DateTimeConverter,
    "time": TimeConverter,
    "timedelta": TimeDeltaConverter,
    "decimal": DecimalConverter,
}

# User-registered converters - `register_converter(name, cls)` populates
# this; `parse_converter` consults it after the built-ins.
_CUSTOM: dict[str, type[_Converter]] = {}

# Converter names that accept the `name(args)` constraint grammar. `any(...)`
# is handled separately because its arguments are a literal value list, not
# keyword constraints. A spec like `{n:bogus(min=1)}` for an unknown name
# raises rather than silently dropping the constraints.
_PARAMETRIZED = frozenset({"str", "string", "int", "float"})

# A `name(args)` spec, e.g. `int(min=1,max=100)` or `str(length=2)`. The name
# is captured separately from the parenthesised argument body so the body can
# be split independently. Whitespace around the parenthesis is tolerated.
_PARAM_SPEC_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s*\((.*)\)$", re.DOTALL)


def _coerce_arg(raw: str) -> Any:
    """Coerce one converter-argument token to int, float, bool, or str.

    The brace grammar carries no type annotations, so a bare token is parsed
    the way a literal would be: an integer where possible, then a float, then
    the booleans `true`/`false`, otherwise the unquoted string. This keeps the
    argument syntax declarative (`int(min=1)`, `str(length=2)`) without forcing
    quotes around the common numeric case.
    """
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        pass
    low = raw.lower()
    if low == "true":
        return True
    if low == "false":
        return False
    if len(raw) >= 2 and raw[0] in "\"'" and raw[-1] == raw[0]:
        return raw[1:-1]
    return raw


def _parse_converter_args(body: str) -> tuple[list[Any], dict[str, Any]]:
    """Split the `(...)` body of a converter spec into positional/keyword args.

    Arguments are comma-separated; a `key=value` token is a keyword argument
    and a bare token is positional. Each value is coerced by `_coerce_arg`. An
    empty body yields no arguments. This is a deliberately small grammar - no
    nested parentheses or quoted commas - matching what the brace placeholders
    need; richer specs belong in a custom converter.
    """
    args: list[Any] = []
    kwargs: dict[str, Any] = {}
    body = body.strip()
    if not body:
        return args, kwargs
    for token in body.split(","):
        token = token.strip()
        if not token:
            raise ValueError("converter argument list has an empty element")
        name, sep, raw = token.partition("=")
        if sep:
            key = name.strip()
            raw = raw.strip()
            if not key.isidentifier():
                raise ValueError(f"invalid converter argument name: {name.strip()!r}")
            if not raw:
                raise ValueError(f"converter argument {key!r} has an empty value")
            if key in kwargs:
                raise ValueError(f"duplicate converter argument: {key!r}")
            kwargs[key] = _coerce_arg(raw)
        else:
            if kwargs:
                raise ValueError("positional converter argument after a keyword argument")
            args.append(_coerce_arg(token))
    return args, kwargs


def register_converter(name: str, converter_cls: type[_Converter]) -> None:
    """Register a custom path converter.

    After `register_converter("slug", SlugConverter)`, routes may use
    `{post:slug}` and the radix tree validates/coerces the segment via
    `SlugConverter().match(...)`. `converter_cls` must subclass
    `Converter` (= `_Converter`). A built-in name cannot be shadowed -
    that raises `ValueError`.
    """
    if name in _BUILTIN:
        raise ValueError(f"cannot override built-in converter {name!r}")
    if not (isinstance(converter_cls, type) and issubclass(converter_cls, _Converter)):
        raise TypeError("converter_cls must be a subclass of Converter")
    _CUSTOM[name] = converter_cls


def parse_converter(spec: str | None) -> _Converter:
    """Build a converter from the `:spec` portion of `{name:spec}`.

    `None` or empty `spec` -> StringConverter (the default).
    Unknown specs raise `ValueError` at route-registration time, never at
    match time. Custom converters registered via `register_converter`
    are consulted after the built-ins.
    """
    if not spec:
        return StringConverter()

    if spec.startswith("any(") and spec.endswith(")"):
        body = spec[4:-1].strip()
        if not body:
            raise ValueError("any() converter requires at least one value")
        choices = tuple(c.strip() for c in body.split(","))
        return AnyConverter(choices)

    # `name(args)` form: a parametrized built-in converter such as
    # `int(min=1,max=100)` or `str(length=2)`. The constructor validates the
    # parsed kwargs, so a bad bound raises at registration time.
    pm = _PARAM_SPEC_RE.match(spec)
    if pm is not None:
        conv_name, body = pm.group(1), pm.group(2)
        if conv_name not in _PARAMETRIZED:
            raise ValueError(f"converter {conv_name!r} does not accept arguments")
        args, kwargs = _parse_converter_args(body)
        param_cls = _BUILTIN[conv_name]
        try:
            return param_cls(*args, **kwargs)
        except TypeError as exc:
            raise ValueError(f"invalid arguments for {conv_name!r} converter: {exc}") from exc

    cls = _BUILTIN.get(spec) or _CUSTOM.get(spec)
    if cls is None:
        raise ValueError(f"unknown path converter: {spec!r}")
    return cls()


# ── Regex-path classification and compilation ─────────────


# Names the radix tree can express natively. A converter spec outside this
# set (a raw regex like `[0-9]+`) forces the route onto the regex fallback.
_TREE_EXPRESSIBLE = frozenset(_BUILTIN)


class _Placeholder:
    """One `{...}` placeholder located in a path: its span, name, and spec.

    `spec` is the text after the first `:` (or `None` for a bare `{name}`),
    and may itself carry regex braces - `{id:[0-9]{2}}` yields spec `[0-9]{2}`.
    """

    __slots__ = ("start", "end", "name", "spec")

    def __init__(self, start: int, end: int, name: str, spec: str | None) -> None:
        self.start = start
        self.end = end
        self.name = name
        self.spec = spec


def _iter_placeholders(text: str) -> list[_Placeholder]:
    """Scan `text` for top-level `{...}` placeholders, balance-aware.

    A placeholder opens at an unescaped `{` and closes at the matching `}`,
    so a converter spec may contain its own balanced braces (`[0-9]{2}`).
    The first `:` inside the placeholder separates the name from the spec;
    everything after it (up to the closing brace) is the spec verbatim.
    """
    out: list[_Placeholder] = []
    i = 0
    n = len(text)
    while i < n:
        if text[i] != "{":
            i += 1
            continue
        # Walk to the matching close brace, tracking nesting depth so an
        # inner `{2}` quantifier does not terminate the placeholder early.
        depth = 1
        j = i + 1
        while j < n and depth:
            if text[j] == "{":
                depth += 1
            elif text[j] == "}":
                depth -= 1
            j += 1
        if depth:
            # Unbalanced brace - leave the rest as literal text.
            break
        inner = text[i + 1 : j - 1]
        name, sep, spec = inner.partition(":")
        out.append(_Placeholder(i, j, name, spec if sep else None))
        i = j
    return out


def _is_parametrized_spec(spec: str) -> bool:
    """Return True when `spec` is a parametrized built-in (`int(min=1)`)."""
    pm = _PARAM_SPEC_RE.match(spec)
    return pm is not None and pm.group(1) in _PARAMETRIZED


def _is_tree_expressible_spec(spec: str) -> bool:
    """Return True when `spec` (the `:...` of a placeholder) is radix-native."""
    if not spec:
        return True
    if spec.startswith("any(") and spec.endswith(")"):
        return True
    if spec in _TREE_EXPRESSIBLE or spec in _CUSTOM:
        return True
    # A parametrized built-in (`int(min=1)`, `str(length=2)`) stays on the
    # radix path: the node holds the constrained converter and applies it
    # during traversal, so a bound violation is a converter miss, not regex.
    return _is_parametrized_spec(spec)


# A bare-identifier spec (`{id:bogus}`) is an unknown-converter typo, not a raw
# regex; only a spec carrying regex metacharacters is treated as a regex route.
_BARE_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _looks_like_regex(conv: str) -> bool:
    """Return True when `conv` is a raw regex rather than a converter name."""
    return _BARE_IDENT_RE.match(conv) is None


def _validate_bare_word_spec(spec: str | None) -> None:
    """Raise for a bare-word converter spec a regex-forced segment cannot honour.

    A regex-forced segment may still carry a bare-identifier converter spec
    (`/v{version:bogus}/api`). Two cases must raise at registration rather than
    silently miscompile into literal regex:

    - an unknown-converter typo (`{version:bogus}`) - the same `ValueError`
      the radix path raises via `parse_converter`.
    - a registered custom converter (`{name:slug}`) - its `match()` has no
      regex representation, so `build_route_regex` cannot express it. Emitting
      `(?P<name>slug)` would match the literal text "slug" instead of the
      converter's semantics. Built-in converters and `any(...)` are exempt:
      `build_route_regex` translates those to real patterns.
    """
    if not spec:
        return
    if _looks_like_regex(spec):
        return
    if spec in _CUSTOM:
        raise ValueError(
            f"custom converter {spec!r} cannot be used in a regex-routed path "
            "segment; it has no regex representation"
        )
    if not _is_tree_expressible_spec(spec):
        raise ValueError(f"unknown path converter: {spec!r}")


def is_regex_path(path: str) -> bool:
    """Decide whether `path` must use the regex fallback rather than the tree.

    A path is a regex route when any segment carries more than one
    placeholder, holds a placeholder that does not span the whole segment
    (`/v{n}/x`, `/files/{name}.{ext}`), or names a converter the radix tree
    cannot express (a raw regex like `{id:[0-9]+}`). A greedy `:path`
    placeholder followed by a non-empty suffix is also a regex route, since
    the tree only accepts `:path` as the final segment.

    Once a path is classified as a regex route, every bare-word converter
    spec across all of its segments is validated: an unknown name like
    `{version:bogus}` or a registered custom converter like `{name:slug}`
    raises `ValueError` here rather than slipping through as literal regex.
    Validating every segment (not just the one that forced regex routing)
    catches typos in later segments such as `/v{version:int}/{id:bogus}`,
    which would otherwise miscompile into a group matching the literal text.
    Custom converters have no regex representation, so they are rejected in
    regex routes rather than miscompiled.
    """
    segments = [s for s in path.split("/") if s]
    total = len(segments)
    all_placeholders: list[list[_Placeholder]] = []
    forced = False
    for idx, seg in enumerate(segments):
        placeholders = _iter_placeholders(seg)
        all_placeholders.append(placeholders)
        if not placeholders:
            continue
        if len(placeholders) > 1:
            forced = True
        ph = placeholders[0]
        whole = ph.start == 0 and ph.end == len(seg)
        if not whole:
            # A placeholder shares the segment with static text.
            forced = True
        for cand in placeholders:
            spec = cand.spec
            if spec and not _is_tree_expressible_spec(spec) and _looks_like_regex(spec):
                # Raw regex converter (`{id:[0-9]+}`).
                forced = True
            if spec == "path" and idx != total - 1:
                forced = True
    if forced:
        # Validate every bare-word spec across all segments so an unknown
        # converter typo anywhere in a regex-routed path still raises instead
        # of becoming a group that matches the literal converter name.
        for placeholders in all_placeholders:
            for cand in placeholders:
                _validate_bare_word_spec(cand.spec)
        return True
    return False


def extract_regex_converters(path: str) -> dict[str, _Converter]:
    """Return the built-in converter for each placeholder in a regex route.

    Bare `{name}` and raw-regex placeholders (`{id:[0-9]+}`) have no built-in
    converter and are omitted - their matched groups stay as strings. Built-in
    specs (`int`, `float`, `uuid`, `path`, `any(...)`) map to the converter the
    radix tree would apply, so `_match_regex` can coerce matched groups to the
    same Python types the tree produces. Custom converters never reach here:
    `is_regex_path` rejects them in regex-forced segments because they have no
    regex representation.
    """
    converters: dict[str, _Converter] = {}
    for ph in _iter_placeholders(path):
        spec = ph.spec
        if not spec:
            continue
        pm = _PARAM_SPEC_RE.match(spec)
        is_parametrized = pm is not None and pm.group(1) in _PARAMETRIZED
        is_named = (
            spec in _BUILTIN or is_parametrized or (spec.startswith("any(") and spec.endswith(")"))
        )
        if is_named:
            converters[ph.name] = parse_converter(spec)
    return converters


def build_route_regex(path: str) -> re.Pattern[str]:
    """Compile `path` into an anchored regex with named groups per parameter.

    Built-in converters map to their `_BUILTIN_REGEX` fragment; a bare
    `{name}` matches one non-slash segment; a raw `{name:PATTERN}` uses
    `PATTERN` verbatim. Static text is `re.escape`'d. The result is anchored
    `^...$` and matched against the full request path.
    """
    out: list[str] = ["^"]

    def _emit_placeholder(name: str, conv: str | None) -> str:
        if conv is None:
            return f"(?P<{name}>[^/]+)"
        builtin = _BUILTIN_REGEX.get(conv)
        if builtin is not None:
            return f"(?P<{name}>{builtin})"
        if conv.startswith("any(") and conv.endswith(")"):
            body = conv[4:-1]
            choices = "|".join(re.escape(c.strip()) for c in body.split(","))
            return f"(?P<{name}>{choices})"
        # Parametrized built-in (`int(min=1)`, `str(length=2)`): emit the base
        # converter's permissive fragment; the constrained converter from
        # `extract_regex_converters` re-validates the matched group, so bounds
        # are enforced even on the regex fallback.
        pm = _PARAM_SPEC_RE.match(conv)
        if pm is not None and pm.group(1) in _PARAMETRIZED:
            return f"(?P<{name}>{_BUILTIN_REGEX[pm.group(1)]})"
        # Raw regex converter: the spec after the colon is the pattern itself.
        return f"(?P<{name}>{conv})"

    pos = 0
    for ph in _iter_placeholders(path):
        out.append(re.escape(path[pos : ph.start]))
        out.append(_emit_placeholder(ph.name, ph.spec))
        pos = ph.end
    out.append(re.escape(path[pos:]))
    out.append("$")
    return re.compile("".join(out))


# JSON Schema type for each built-in converter, so a path parameter can be
# documented even when no handler parameter declares one. Only the converters
# that coerce to a non-string value need an entry; everything else - including
# a raw-regex spec and any user-registered converter - is carried as a string,
# which is what the segment is before coercion.
_CONVERTER_JSON_TYPES: dict[str, dict[str, Any]] = {
    "int": {"type": "integer"},
    "float": {"type": "number"},
    "decimal": {"type": "number"},
    "uuid": {"type": "string", "format": "uuid"},
    "date": {"type": "string", "format": "date"},
    "datetime": {"type": "string", "format": "date-time"},
    "time": {"type": "string", "format": "time"},
    "path": {"type": "string", "format": "path"},
}


def path_param_schemas(template: str) -> dict[str, dict[str, Any]]:
    """Map each placeholder in `template` to the JSON Schema of its value.

    A route's path parameters are part of its contract whether or not a handler
    parameter declares one: a dependency reading `request.path_params` consumes
    the same segment. Both documentation paths (OpenAPI and the MCP tool schema)
    use this to describe a parameter no signature mentions, rather than
    publishing a contract that omits a value the route requires.
    """
    schemas: dict[str, dict[str, Any]] = {}
    for placeholder in _iter_placeholders(template):
        spec = placeholder.spec
        name = spec.split("(", 1)[0].strip() if spec else ""
        schemas[placeholder.name] = dict(_CONVERTER_JSON_TYPES.get(name, {"type": "string"}))
    return schemas
