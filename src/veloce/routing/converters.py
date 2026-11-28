"""Path converters — segment coercion, and the rest of what a template says.

Two segment syntaxes are accepted: the angle-bracket form
(`<int:id>`, `<path:p>`, `<any(a,b):x>`) and the brace form
(`{id:int}`, `{p:path}`). Both map to the same converter set.

A converter:
  - `match(segment)` returns `(ok, coerced_value)`.
  - `greedy` is True for the `path` converter which consumes the remainder
    of the URL (including slashes) instead of one segment.

Beyond match-time coercion the module owns the rest of what a route template
says about itself: placeholder scanning, the decision of whether a path is
tree-expressible or needs a compiled regex, and the JSON-Schema fragment each
parameter contributes.

A radix node keeps the converter its param declares and applies it during the
match traversal, falling through to the next child candidate when it rejects a
segment - so a typed mismatch is a **route miss**, not a 422.
"""

from __future__ import annotations

import functools
import re
import warnings
from typing import Any

from veloce._internal import _SCALAR_JSON_SCHEMAS, _require_methods
from veloce.routing._converter_base import (
    _BUILTIN_REGEX,
    _MAX_INT_DIGITS,
    _UUID_PATTERN,
    _UUID_RE,
    AnyConverter,
    Converter,
    FloatConverter,
    IntConverter,
    PathConverter,
    StringConverter,
    UUIDConverter,
)
from veloce.routing._converter_temporal import (
    _DATE_RE,
    _DATETIME_RE,
    _DECIMAL_RE,
    _HAS_CISO8601,
    _MAX_DECIMAL_CHARS,
    _TIME_RE,
    _TIMEDELTA_RE,
    _TIMEDELTA_STR_RE,
    DateConverter,
    DateTimeConverter,
    DecimalConverter,
    TimeConverter,
    TimeDeltaConverter,
    _normalize_z,
    _parse_datetime,
    _parse_datetime_accelerated,
    _parse_datetime_ciso,
    _parse_datetime_stdlib,
)

# Every name imported above is re-exported, so the historical
# `veloce.routing.converters.X` paths keep resolving.

# ── Registry and parsing ──────────────────────────────────


# The pre-rename spelling, kept so an importer of the private name resolves.
_Converter = Converter

_BUILTIN: dict[str, type[Converter]] = {
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

# Populated by `register_converter`; consulted after the built-ins.
_CUSTOM: dict[str, type[Converter]] = {}

# `any(...)` is separate: its arguments are literals, not keyword constraints.
_PARAMETRIZED = frozenset({"str", "string", "int", "float"})

# A `name(args)` spec, with the name captured apart from the argument body.
_PARAM_SPEC_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s*\((.*)\)$", re.DOTALL)


def _coerce_arg(raw: str) -> Any:
    """Coerce one converter-argument token to int, float, bool, or str.

    The brace grammar carries no type annotations, so a bare token is read as a
    literal would be - int, then float, then `true`/`false`, else the unquoted
    string - which spares the common numeric case its quotes.
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

    Arguments are comma-separated; a `key=value` token is a keyword argument and
    a bare token positional, each coerced by `_coerce_arg`. A deliberately small
    grammar - no nested parentheses or quoted commas - so richer specs belong in
    a custom converter.
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


def register_converter(name: str, converter_cls: type[Converter]) -> None:
    """Register a custom path converter.

    After `register_converter("slug", SlugConverter)`, routes may use
    `{post:slug}` and the radix tree coerces the segment through
    `SlugConverter().match(...)`. Shadowing a built-in name raises `ValueError`.

    Args:
        name: The converter name used in `{param:name}`.
        converter_cls: A `Converter` subclass.

    Raises:
        ValueError: If `name` is a built-in converter.
        TypeError: If `converter_cls` is not a `Converter` subclass.

    """
    if name in _BUILTIN:
        raise ValueError(f"cannot override built-in converter {name!r}")
    if not (isinstance(converter_cls, type) and issubclass(converter_cls, Converter)):
        raise TypeError("converter_cls must be a subclass of Converter")
    _CUSTOM[name] = converter_cls
    _cached_path_param_converters.cache_clear()


def unregister_converter(name: str) -> None:
    """Remove a converter registered with `register_converter`.

    No-op when `name` was never registered; a built-in name raises `ValueError`
    as in `register_converter`. Routes already registered keep the instance they
    were built with, so this affects only later registrations.
    """
    if name in _BUILTIN:
        raise ValueError(f"cannot unregister built-in converter {name!r}")
    _CUSTOM.pop(name, None)
    _cached_path_param_converters.cache_clear()


def parse_converter(spec: str | None) -> Converter:
    """Build a converter from the `:spec` portion of `{name:spec}`.

    `None` or empty `spec` gives the default `StringConverter`. An unknown spec
    raises `ValueError` at route-registration time, never at match time; custom
    converters are consulted after the built-ins.
    """
    if not spec:
        return StringConverter()

    if spec.startswith("any(") and spec.endswith(")"):
        body = spec[4:-1].strip()
        if not body:
            raise ValueError("any() converter requires at least one value")
        choices = tuple(c.strip() for c in body.split(","))
        return AnyConverter(choices)

    # The constructor validates the kwargs, so a bad bound raises at registration.
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


# A spec outside this set forces the route onto the regex fallback.
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

    A placeholder closes at the *matching* `}`, so a converter spec may carry its
    own balanced braces (`[0-9]{2}`). The first `:` separates name from spec, and
    everything after it is the spec verbatim.
    """
    out: list[_Placeholder] = []
    i = 0
    n = len(text)
    while i < n:
        if text[i] != "{":
            i += 1
            continue
        # Depth-tracked, so an inner `{2}` quantifier does not close it early.
        depth = 1
        j = i + 1
        while j < n and depth:
            if text[j] == "{":
                depth += 1
            elif text[j] == "}":
                depth -= 1
            j += 1
        if depth:
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
    # A parametrized built-in stays on the radix path, which holds its converter.
    return _is_parametrized_spec(spec)


# A bare identifier (`{id:bogus}`) is a typo, not a raw regex.
_BARE_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _looks_like_regex(conv: str) -> bool:
    """Return True when `conv` is a raw regex rather than a converter name."""
    return _BARE_IDENT_RE.match(conv) is None


def _validate_bare_word_spec(spec: str | None) -> None:
    """Raise for a bare-word converter spec a regex-forced segment cannot honour.

    A regex-forced segment may still carry a bare-identifier spec
    (`/v{version:bogus}/api`). Two cases must raise rather than miscompile into
    literal regex: an unknown-converter typo, and a registered custom converter,
    whose `match()` has no regex representation - `(?P<name>slug)` would match
    the literal text "slug". Built-ins and `any(...)` are exempt, since
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

    A path is a regex route when any segment carries more than one placeholder,
    holds one that does not span the whole segment (`/files/{name}.{ext}`), or
    names a converter the tree cannot express (`{id:[0-9]+}`). A greedy `:path`
    followed by a non-empty suffix is one too, since the tree accepts `:path`
    only as the final segment.

    Every bare-word spec across *all* segments is then validated, so a typo in a
    later segment (`/v{version:int}/{id:bogus}`) raises rather than miscompiling
    into a group matching the literal text.
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
            forced = True
        for cand in placeholders:
            spec = cand.spec
            if spec and not _is_tree_expressible_spec(spec) and _looks_like_regex(spec):
                forced = True
            if spec == "path" and idx != total - 1:
                forced = True
    if forced:
        # Across all segments, so a typo anywhere raises rather than compiling.
        for placeholders in all_placeholders:
            for cand in placeholders:
                _validate_bare_word_spec(cand.spec)
        return True
    return False


def extract_regex_converters(path: str) -> dict[str, Converter]:
    """Return the built-in converter for each placeholder in a regex route.

    Bare `{name}` and raw-regex placeholders have no built-in converter and are
    omitted, so their matched groups stay strings. A built-in spec maps to the
    converter the radix tree would apply, so `_match_regex` coerces to the same
    Python types. Custom converters never reach here.
    """
    converters: dict[str, Converter] = {}
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

    Built-in converters map to their `_BUILTIN_REGEX` fragment, a bare `{name}`
    matches one non-slash segment, and a raw `{name:PATTERN}` uses `PATTERN`
    verbatim. Static text is escaped and the result anchored `^...$`.
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
        # The permissive base fragment; the converter re-validates the group.
        pm = _PARAM_SPEC_RE.match(conv)
        if pm is not None and pm.group(1) in _PARAMETRIZED:
            return f"(?P<{name}>{_BUILTIN_REGEX[pm.group(1)]})"
        return f"(?P<{name}>{conv})"

    pos = 0
    for ph in _iter_placeholders(path):
        out.append(re.escape(path[pos : ph.start]))
        out.append(_emit_placeholder(ph.name, ph.spec))
        pos = ph.end
    out.append(re.escape(path[pos:]))
    out.append("$")
    return re.compile("".join(out))


# Only non-string coercions need an entry; everything else carries as a string.
_CONVERTER_JSON_TYPES: dict[str, dict[str, Any]] = {
    "int": _SCALAR_JSON_SCHEMAS["integer"],
    "float": _SCALAR_JSON_SCHEMAS["number"],
    "decimal": _SCALAR_JSON_SCHEMAS["number"],
    "uuid": _SCALAR_JSON_SCHEMAS["uuid"],
    "date": _SCALAR_JSON_SCHEMAS["date"],
    "datetime": _SCALAR_JSON_SCHEMAS["date-time"],
    "time": _SCALAR_JSON_SCHEMAS["time"],
    "path": _SCALAR_JSON_SCHEMAS["path"],
}


@functools.lru_cache(maxsize=512)
def _cached_path_param_converters(template: str) -> dict[str, Converter]:
    """Walk `template`, mapping each typed placeholder to its converter.

    The mapping is shared, not copied, so a caller that mutates must copy first.
    """
    converters: dict[str, Converter] = {}
    for placeholder in _iter_placeholders(template):
        spec = placeholder.spec
        if not spec:
            continue
        is_any = spec.startswith("any(") and spec.endswith(")")
        if not is_any and not _is_parametrized_spec(spec) and _looks_like_regex(spec):
            continue
        converters[placeholder.name] = parse_converter(spec)
    return converters


def path_param_converters(template: str) -> dict[str, Converter]:
    """Map each *typed* placeholder in `template` to the converter it applies.

    A bare `{name}` and a raw-regex placeholder have no single coercing converter
    and are omitted, so a caller learns which placeholders it can validate rather
    than being promised coverage that does not exist. A parametrized built-in
    (`int(min=1)`) and `any(...)` carry parens that read as regex but map to real
    converters, so both are whitelisted.

    Shared by `url_for`'s reverse check and the MCP tool door, so a value a URL
    could never carry is refused identically wherever it arrives.

    Cached on the template: building a converter per placeholder measured 12.5 us
    for a single `{id:int}` and is paid per tool call. `register_converter` and
    `unregister_converter` clear the cache.
    """
    return _cached_path_param_converters(template)


def path_param_schemas(template: str) -> dict[str, dict[str, Any]]:
    """Map each placeholder in `template` to the JSON Schema of its value.

    A route's path parameters are part of its contract whether or not a handler
    parameter declares one, since a dependency reading `request.path_params`
    consumes the same segment. Both OpenAPI and the MCP tool schema use this to
    describe a parameter no signature mentions.
    """
    schemas: dict[str, dict[str, Any]] = {}
    for placeholder in _iter_placeholders(template):
        spec = placeholder.spec
        name = spec.split("(", 1)[0].strip() if spec else ""
        schemas[placeholder.name] = dict(_CONVERTER_JSON_TYPES.get(name, {"type": "string"}))
    return schemas
