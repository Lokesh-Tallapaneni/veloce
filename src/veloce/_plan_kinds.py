"""Slot-kind constants and sequence-shape helpers for the handler plan."""

from __future__ import annotations

import copy
from typing import Any, cast, get_args, get_origin

from veloce._params import (
    ParamBase,
    deepcopyable,
    is_immutable,
)

# ── Slot-kind constants ───────────────────────────────────
# Bare integers (not IntEnum) for cheap branching in the request hot loop.
K_REQUEST = 0
K_BG_TASKS = 1
K_DEPENDS = 2
K_PARAM_MARKER = 3
K_QUERY = 5
K_QUERY_LIST = 6
K_BODY_MODEL = 7
K_UPLOAD_FILE = 8
K_SECURITY_SCOPES = 11
K_RESPONSE = 12
K_WEBSOCKET = 13
# A model whose FIELDS are read from one request source rather than the body.
K_MODEL_GROUP = 14

MK_QUERY = 0
MK_PATH = 1
MK_HEADER = 2
MK_COOKIE = 3
MK_BODY = 4
MK_FORM = 5
MK_FILE = 6

#: Every sequence origin a repeated value or a JSON array validates into, so the
#: body validator, the field spec and the schema lowering cannot list different sets.
_SEQUENCE_TYPES = (list, set, frozenset, tuple)
_SEQUENCE_ORIGINS = frozenset(_SEQUENCE_TYPES)


def _normalise_sequence_default(marker: ParamBase, target_type: Any) -> ParamBase:
    """Return a marker whose default is the container the annotation declares.

    `t: set[str] = Query(default=["a", "b"])` names a set and offers a list. A
    copy, never the marker itself: one marker can be shared between routes.
    Whether the result is shared per request or rebuilt is decided by what is in
    it - a mutable or mutably-held member means a fresh value each time. An
    unconvertible default is left as written rather than raising.
    """
    origin = get_origin(target_type)
    if origin not in _SEQUENCE_ORIGINS or not marker.has_default:
        return marker
    source = marker.default
    if not isinstance(source, _SEQUENCE_TYPES):
        if marker.default_factory is None:
            return marker
        inner_factory = marker.default_factory
        normalised = copy.copy(marker)
        normalised.default_factory = lambda: _rebuild_sequence(origin, inner_factory())
        return normalised
    # Read from the static default even when a factory exists, which would copy
    # a long list only to rebuild it as a set.
    members = tuple(source)
    try:
        built = origin(members)
    except TypeError:
        return marker
    normalised = copy.copy(marker)
    if not is_immutable(members) and deepcopyable(members):
        normalised.default_factory = lambda: origin(copy.deepcopy(members))
    elif origin is set or origin is list:
        # Copying the built container, not rebuilding: a set copy re-uses its hashes.
        normalised.default_factory = cast("set[Any] | list[Any]", built).copy
    else:
        normalised.default = built
        normalised.default_factory = None
    return normalised


def _rebuild_sequence(origin: Any, value: Any) -> Any:
    """Build `origin` from `value`, leaving it alone if it cannot be built."""
    if not isinstance(value, _SEQUENCE_TYPES):
        # `set` accepts anything iterable: `"ab"` becomes `{"a", "b"}`.
        return value
    try:
        return origin(value)
    except TypeError:
        # The author's bug, but this runs per request: raising would 500 each one.
        return value


def _fixed_shape_args(annotation: Any) -> tuple[Any, ...] | None:
    """Return a fixed-shape tuple's per-position types, or `None` if homogeneous.

    `tuple[int, str]` declares two positions; `tuple[int, ...]`, `list[int]` and
    `set[int]` declare one element type for every member.
    """
    if get_origin(annotation) is not tuple:
        return None
    args = get_args(annotation)
    if Ellipsis in args:
        return None
    if args == ((),):
        # How `tuple[()]` decomposes before 3.11: zero positions, not "no element
        # type".
        return ()
    return args


#: Single-sourced so the interpreter and the codegen read one map.
MARKER_LOC = {
    MK_QUERY: "query",
    MK_PATH: "path",
    MK_HEADER: "header",
    MK_COOKIE: "cookie",
    MK_BODY: "body",
    MK_FORM: "form",
    MK_FILE: "form",
}


# ── Plan builders ─────────────────────────────────────────
#: Means "this route declares no path parameter", which only a caller knows.
_NO_PATH_PARAMS: frozenset[str] = frozenset()
