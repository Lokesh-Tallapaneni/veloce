"""Router helpers — path shape, trailing-slash and exclusion utilities."""

from __future__ import annotations

import functools
import keyword
import re
from collections.abc import Sequence
from typing import Any

from veloce.middleware.base import Middleware
from veloce.routing.converters import (
    Converter,
    _iter_placeholders,
    path_param_converters,
)

# `"error"` raises, `"warn"` logs and replaces, `"override"` is silent.
_DUPLICATE_POLICIES = frozenset({"error", "warn", "override"})


def _split_exclusions(
    entries: Sequence[str | type[Any]] | None,
) -> tuple[frozenset[str], tuple[type[Any], ...]] | None:
    """Split `exclude_middleware` into its name set and its type tuple.

    `None` when the route excludes nothing, which the dispatch path tests for
    before doing any filtering. An entry that is neither a name nor a
    `Middleware` subclass is a `TypeError` here rather than an exclusion that
    silently matches nothing - which is the failure this shape exists to end.
    """
    if not entries:
        return None
    names: set[str] = set()
    types: list[type[Any]] = []
    for entry in entries:
        if isinstance(entry, str):
            names.add(entry)
        elif isinstance(entry, type) and issubclass(entry, Middleware):
            types.append(entry)
        else:
            raise TypeError(
                "exclude_middleware entries must be a middleware class or a "
                f"middleware name, got {entry!r}"
            )
    return frozenset(names), tuple(types)


# `/items/{slug}` and `/items/{id}` both become `/items/{}`, so a tree route and
# a regex fallback mapping to one effective path can be detected.
_PARAM_SHAPE_RE = re.compile(r"\{[^{}]*\}")

#: The unreserved set (RFC 3986 Sec. 2.3) plus the two `pchar` extras. A value
#: made only of these is its own encoding, so `url_for` skips `quote` entirely.
_NEEDS_QUOTE_IN_SEGMENT = re.compile(r"[^A-Za-z0-9_.~:@-]")

#: The same, plus `/` for a greedy `path` converter.
_NEEDS_QUOTE_IN_PATH = re.compile(r"[^A-Za-z0-9_.~:@/-]")


def _path_shape(path: str) -> str:
    """Return `path` with every `{param}` collapsed to `{}` for shape compare."""
    return _PARAM_SHAPE_RE.sub("{}", path)


@functools.lru_cache(maxsize=512)
def _cached_split_path(path: str) -> tuple[str, ...]:
    """Split `path` into segments, keeping any the path actually contains.

    An absolute path always starts with an empty part, and a trailing slash
    adds one; both are structural and dropped. Every *other* empty is a real
    empty segment (`//admin/x`, `/a//b`) and is kept, so the radix walk looks
    for a child named `""`, finds none, and the path simply does not match.

    Dropping them instead made `//admin/x` produce the same segments as
    `/admin/x` and match the same route, while `request.path` still read the
    original - which is how a prefix check and the router came to disagree.
    Keeping them makes the refusal structural: there is nothing to check on the
    match path, because a path that cannot be walked cannot be matched.
    """
    parts = path.split("/")
    end = len(parts) - 1 if len(parts) > 1 and parts[-1] == "" else len(parts)
    start = 1 if parts and parts[0] == "" else 0
    return tuple(parts[start:end])


def _reverse_converters_for(template: str) -> dict[str, Converter]:
    """Map each typed placeholder in `template` to its converter for url_for.

    A bare `{name}` (no spec) and a raw-regex placeholder (`{id:[0-9]+}`) have
    no single coercing converter, so they are omitted - those params accept any
    stringifiable value during reverse. Built-in, custom, and `any(...)` specs
    map to the same converter the radix matcher applies, so url_for can reject a
    value the matcher would never accept. A bare or raw-regex placeholder is
    therefore *not* validated - `url_for` guarantees resolvability only for the
    placeholders this returns, which is why the omission is stated here rather
    than promised away. `any(...)` is whitelisted explicitly
    because it carries parentheses that the bare-identifier test reads as regex.
    """
    return path_param_converters(template)


def _check_duplicate_params(full_path: str) -> None:
    """Reject a path whose parameter names are illegal or bound twice.

    Path parameters are passed to the handler as keyword arguments, so a name
    that is not a legal Python identifier (or is a reserved keyword) can never
    bind and would otherwise fail opaquely deep in the handler-plan binder at
    request time. A duplicate is likewise always a bug: on the radix path the
    second capture silently clobbers the first at match time; on the regex path
    `re.compile` raises an opaque "redefinition of group name" error. Catch all
    three at registration with one clear, path-scoped error, using the same
    placeholder scanner both branches consume so the names checked are exactly
    the names bound.
    """
    seen: set[str] = set()
    for ph in _iter_placeholders(full_path):
        if not ph.name.isidentifier() or keyword.iskeyword(ph.name):
            raise ValueError(
                f"Route {full_path!r}: invalid path parameter name {ph.name!r}; "
                "a parameter name must be a valid Python identifier and not a keyword"
            )
        if ph.name in seen:
            raise ValueError(f"Route {full_path!r}: duplicate path parameter {ph.name!r}")
        seen.add(ph.name)


def _slash_mismatch(node: Any, request_has_slash: bool) -> bool:
    """Whether trailing-slash strictness rules this node out for this request.

    A route registered with a trailing slash matches only slashed requests, and
    one registered without matches only unslashed ones. `tolerant_slash`
    (per-route `strict_slashes=False`) skips the gate entirely, and a node that
    had *both* forms registered serves both shapes so neither arm fires.

    One predicate because two consumers must agree: `_match_tree` decides
    whether a request routes, and `get_allowed_methods` decides what the `Allow`
    header advertises. Written separately they can disagree, and then a 405
    names a method that would not have matched, or omits one that would.
    """
    if node.tolerant_slash:
        return False
    if node.trailing_slash and not node.unslashed_variant and not request_has_slash:
        return True
    return bool(
        node.unslashed_variant and not node.trailing_slash and request_has_slash and node.handlers
    )
