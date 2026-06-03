"""Path converters - match-time validation and coercion of URL segments.

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

# UUID format per RFC 4122 (8-4-4-4-12 hex digits, case-insensitive).
_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-"
    r"[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{12}$"
)

# Body (un-anchored) form of the UUID pattern, reused when translating a
# `{id:uuid}` placeholder into a named regex group for the hybrid router's
# regex fallback. Keep in sync with `_UUID_RE`.
_UUID_PATTERN = (
    r"[0-9a-fA-F]{8}-"
    r"[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{12}"
)

# Un-anchored regex fragments for the built-in converters. A bare `{name}`
# (no converter) and the `str`/`string` converters both match one non-slash
# segment. `path` is greedy and crosses slashes. These feed the regex
# fallback only - the radix fast path never consults them.
_BUILTIN_REGEX: dict[str, str] = {
    "str": r"[^/]+",
    "string": r"[^/]+",
    "int": r"-?\d+",
    "float": r"-?\d+\.\d+",
    "uuid": _UUID_PATTERN,
    "path": r".+",
    # Single-segment (no slash) fragments for the regex fallback; the
    # converter re-validates the matched group, so these stay permissive.
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


# -- Converters ----------------------------------------------


class _Converter:
    """Base class - subclasses override `match` and may set `greedy`."""

    __slots__ = ()
    greedy = False

    def match(self, value: str) -> tuple[bool, Any]:
        """Validate (and coerce) `value`. Return (ok, coerced)."""
        raise NotImplementedError


class StringConverter(_Converter):
    """Default. Accepts any single non-empty segment (slashes excluded)."""

    __slots__ = ()

    def match(self, value: str) -> tuple[bool, Any]:
        # Empty path segments cannot match a {param}; the splitter strips
        # them out, so reaching here with an empty value is unexpected.
        if not value:
            return False, None
        return True, value


class IntConverter(_Converter):
    """Matches a decimal integer; coerces to Python int."""

    __slots__ = ()

    def match(self, value: str) -> tuple[bool, Any]:
        if not value or len(value) > _MAX_INT_DIGITS:
            return False, None
        check = value[1:] if value[0] == "-" else value
        if not check or not check.isdigit():
            return False, None
        return True, int(value)


class FloatConverter(_Converter):
    """Matches a decimal float (no scientific notation)."""

    __slots__ = ()

    def match(self, value: str) -> tuple[bool, Any]:
        if not value:
            return False, None
        # the float converter rejects "nan"/"inf" and scientific
        # notation; require a '.' to make this a clear float vs int.
        if "." not in value:
            return False, None
        if "e" in value or "E" in value:
            return False, None
        try:
            f = float(value)
        except ValueError:
            return False, None
        if math.isnan(f) or math.isinf(f):
            return False, None
        return True, f


class UUIDConverter(_Converter):
    """Matches a canonical UUID per RFC 4122; coerces to uuid.UUID."""

    __slots__ = ()

    def match(self, value: str) -> tuple[bool, Any]:
        if len(value) != 36 or not _UUID_RE.match(value):
            return False, None
        try:
            return True, uuid.UUID(value)
        except ValueError:
            return False, None


class PathConverter(_Converter):
    """Greedy converter - consumes the rest of the URL, including slashes."""

    __slots__ = ()
    greedy = True

    def match(self, value: str) -> tuple[bool, Any]:
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

    def match(self, value: str) -> tuple[bool, Any]:
        if not _DATE_RE.match(value):
            return False, None
        try:
            return True, datetime.date.fromisoformat(value)
        except ValueError:
            return False, None


class DateTimeConverter(_Converter):
    """Matches an ISO 8601 datetime; coerces to datetime.datetime."""

    __slots__ = ()

    def match(self, value: str) -> tuple[bool, Any]:
        if not _DATETIME_RE.match(value):
            return False, None
        try:
            return True, datetime.datetime.fromisoformat(_normalize_z(value))
        except ValueError:
            return False, None


class TimeConverter(_Converter):
    """Matches an ISO 8601 time; coerces to datetime.time."""

    __slots__ = ()

    def match(self, value: str) -> tuple[bool, Any]:
        if not _TIME_RE.match(value):
            return False, None
        try:
            return True, datetime.time.fromisoformat(_normalize_z(value))
        except ValueError:
            return False, None


class TimeDeltaConverter(_Converter):
    """Matches an ISO 8601 duration; coerces to datetime.timedelta.

    Stricter than Litestar: a bare number such as `60` is rejected - only
    an ISO duration (`P1DT2H`) with at least one component is accepted.
    """

    __slots__ = ()

    def match(self, value: str) -> tuple[bool, Any]:
        m = _TIMEDELTA_RE.match(value)
        if not m:
            return False, None
        days, hours, minutes, seconds = m.groups()
        return True, datetime.timedelta(
            days=int(days) if days else 0,
            hours=int(hours) if hours else 0,
            minutes=int(minutes) if minutes else 0,
            seconds=float(seconds) if seconds else 0,
        )


class DecimalConverter(_Converter):
    """Matches a decimal literal; coerces to decimal.Decimal."""

    __slots__ = ()

    def match(self, value: str) -> tuple[bool, Any]:
        if not value or len(value) > _MAX_DECIMAL_CHARS or not _DECIMAL_RE.match(value):
            return False, None
        try:
            return True, decimal.Decimal(value)
        except decimal.InvalidOperation:
            return False, None


class AnyConverter(_Converter):
    """Matches one of a fixed set of literal values: `{x:any(red,blue)}`."""

    __slots__ = ("_choices",)

    def __init__(self, choices: tuple[str, ...]) -> None:
        self._choices = frozenset(choices)

    def match(self, value: str) -> tuple[bool, Any]:
        return (value in self._choices), value


# -- Registry and parsing ------------------------------------


# Public base-class alias - subclass `Converter` to build a custom one.
Converter = _Converter

_BUILTIN = {
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

    cls = _BUILTIN.get(spec) or _CUSTOM.get(spec)
    if cls is None:
        raise ValueError(f"unknown path converter: {spec!r}")
    return cls()


# -- Regex-path classification and compilation ---------------


# Names the radix tree can express natively. A converter spec outside this
# set (a raw regex like `[0-9]+`) forces the route onto the regex fallback.
_TREE_EXPRESSIBLE = frozenset(_BUILTIN)

# A whole-segment placeholder: the brace spans the entire segment, e.g.
# `{id}` or `{id:int}`. `{n}/x` partial segments and multi-brace segments
# like `{name}.{ext}` are deliberately rejected by `is_regex_path`.
_WHOLE_SEGMENT_PARAM_RE = re.compile(r"^\{([^{}]*)\}$")


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


def _is_tree_expressible_spec(spec: str) -> bool:
    """Return True when `spec` (the `:...` of a placeholder) is radix-native."""
    if not spec:
        return True
    if spec.startswith("any(") and spec.endswith(")"):
        return True
    return spec in _TREE_EXPRESSIBLE or spec in _CUSTOM


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
        is_named = spec in _BUILTIN or (spec.startswith("any(") and spec.endswith(")"))
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
