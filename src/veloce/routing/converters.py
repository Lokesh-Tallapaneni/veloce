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
        if not value:
            return False, None
        # Accept optional leading '-' for signed ints. Reject leading zeros'
        # leading zeros are accepted — int("042") parses fine.
        try:
            return True, int(value)
        except ValueError:
            return False, None


class FloatConverter(_Converter):
    """Matches a decimal float (no scientific notation)."""

    __slots__ = ()

    def match(self, value: str) -> tuple[bool, Any]:
        if not value:
            return False, None
        # the float converter rejects "nan"/"inf" and scientific
        # notation; require a '.' to make this a clear float vs int.
        if "." not in value and "e" not in value and "E" not in value:
            return False, None
        try:
            f = float(value)
        except ValueError:
            return False, None
        if f != f or f in (float("inf"), float("-inf")):
            return False, None
        return True, f


class UUIDConverter(_Converter):
    """Matches a canonical UUID per RFC 4122; coerces to uuid.UUID."""

    __slots__ = ()

    def match(self, value: str) -> tuple[bool, Any]:
        if not _UUID_RE.match(value):
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
        self._choices = choices

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
