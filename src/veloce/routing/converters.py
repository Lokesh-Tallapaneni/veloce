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
candidate (next param or wildcard) — which means a typed mismatch is a
**route miss**, not a 422.
"""

from __future__ import annotations

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
# fallback only — the radix fast path never consults them.
_BUILTIN_REGEX: dict[str, str] = {
    "str": r"[^/]+",
    "string": r"[^/]+",
    "int": r"-?\d+",
    "float": r"-?\d+\.\d+",
    "uuid": _UUID_PATTERN,
    "path": r".+",
}

# Cap int-parse input length to bound adversarial parse cost. The converter
# coerces to Python int (arbitrary precision), so this is a bignum-DoS guard,
# not a 64-bit range check.
_MAX_INT_DIGITS = 20


class _Converter:
    """Base class — subclasses override `match` and may set `greedy`."""

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
    """Greedy converter — consumes the rest of the URL, including slashes."""

    __slots__ = ()
    greedy = True

    def match(self, value: str) -> tuple[bool, Any]:
        # Reject empty (zero segments left) so static suffixes still win.
        if not value:
            return False, None
        return True, value


class AnyConverter(_Converter):
    """Matches one of a fixed set of literal values: `{x:any(red,blue)}`."""

    __slots__ = ("_choices",)

    def __init__(self, choices: tuple[str, ...]) -> None:
        self._choices = frozenset(choices)

    def match(self, value: str) -> tuple[bool, Any]:
        return (value in self._choices), value


# Public base-class alias — subclass `Converter` to build a custom one.
Converter = _Converter

_BUILTIN = {
    "str": StringConverter,
    "string": StringConverter,
    "int": IntConverter,
    "float": FloatConverter,
    "uuid": UUIDConverter,
    "path": PathConverter,
}

# User-registered converters — `register_converter(name, cls)` populates
# this; `parse_converter` consults it after the built-ins.
_CUSTOM: dict[str, type[_Converter]] = {}


def register_converter(name: str, converter_cls: type[_Converter]) -> None:
    """Register a custom path converter.

    After `register_converter("slug", SlugConverter)`, routes may use
    `{post:slug}` and the radix tree validates/coerces the segment via
    `SlugConverter().match(...)`. `converter_cls` must subclass
    `Converter` (= `_Converter`). A built-in name cannot be shadowed —
    that raises `ValueError`.
    """
    if name in _BUILTIN:
        raise ValueError(f"cannot override built-in converter {name!r}")
    if not (isinstance(converter_cls, type) and issubclass(converter_cls, _Converter)):
        raise TypeError("converter_cls must be a subclass of Converter")
    _CUSTOM[name] = converter_cls


def parse_converter(spec: str | None) -> _Converter:
    """Build a converter from the `:spec` portion of `{name:spec}`.

    `None` or empty `spec` → StringConverter (the default).
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


# Names the radix tree can express natively. A converter spec outside this
# set (a raw regex like `[0-9]+`) forces the route onto the regex fallback.
_TREE_EXPRESSIBLE = frozenset(_BUILTIN)

# A whole-segment placeholder: the brace spans the entire segment, e.g.
# `{id}` or `{id:int}`. `{n}/x` partial segments and multi-brace segments
# like `{name}.{ext}` are deliberately rejected by `is_regex_path`.
_WHOLE_SEGMENT_PARAM_RE = re.compile(r"^\{([^{}]*)\}$")

# Matches every `{...}` placeholder in a segment so we can count and inspect
# them — used to detect multi-brace and partial-segment shapes.
_BRACE_RE = re.compile(r"\{([^{}]*)\}")


def _is_tree_expressible_spec(spec: str) -> bool:
    """Return True when `spec` (the `:...` of a placeholder) is radix-native."""
    if not spec:
        return True
    if spec.startswith("any(") and spec.endswith(")"):
        return True
    return spec in _TREE_EXPRESSIBLE or spec in _CUSTOM


def is_regex_path(path: str) -> bool:
    """Decide whether `path` must use the regex fallback rather than the tree.

    A path is a regex route when any segment carries more than one
    placeholder, holds a placeholder that does not span the whole segment
    (`/v{n}/x`, `/files/{name}.{ext}`), or names a converter the radix tree
    cannot express (a raw regex like `{id:[0-9]+}`). A greedy `:path`
    placeholder followed by a non-empty suffix is also a regex route, since
    the tree only accepts `:path` as the final segment.
    """
    segments = [s for s in path.split("/") if s]
    total = len(segments)
    for idx, seg in enumerate(segments):
        braces = _BRACE_RE.findall(seg)
        if not braces:
            continue
        if len(braces) > 1:
            return True
        whole = _WHOLE_SEGMENT_PARAM_RE.match(seg)
        if whole is None:
            # A placeholder shares the segment with static text.
            return True
        spec = whole.group(1)
        _, _, conv = spec.partition(":")
        has_conv = ":" in spec
        if has_conv and not _is_tree_expressible_spec(conv):
            return True
        if conv == "path" and idx != total - 1:
            return True
    return False


def build_route_regex(path: str) -> re.Pattern[str]:
    """Compile `path` into an anchored regex with named groups per parameter.

    Built-in converters map to their `_BUILTIN_REGEX` fragment; a bare
    `{name}` matches one non-slash segment; a raw `{name:PATTERN}` uses
    `PATTERN` verbatim. Static text is `re.escape`'d. The result is anchored
    `^...$` and matched against the full request path.
    """
    out: list[str] = ["^"]

    def _emit_placeholder(spec: str) -> str:
        name, sep, conv = spec.partition(":")
        if not sep:
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
    for m in _BRACE_RE.finditer(path):
        out.append(re.escape(path[pos : m.start()]))
        out.append(_emit_placeholder(m.group(1)))
        pos = m.end()
    out.append(re.escape(path[pos:]))
    out.append("$")
    return re.compile("".join(out))
