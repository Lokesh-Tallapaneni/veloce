"""Resolver codegen - registration-time codegen for parameter-only handlers.

Runs at registration time only: the generated resolver is built once when a
route is registered and never recompiled on the request path.

For handlers whose plan binds only the request and scalar path/query
parameters, the generic `_resolve_slots` interpreter (a `while` loop with a
per-slot `kind` dispatch) is replaced by a straight-line function generated
once at registration and `exec`-compiled. The generated function inlines each
slot's name, target type, and default, so the request path does no loop, no
kind branching, and no slot attribute lookups.

`compile_param_resolver` returns `None` for any plan it does not fully support
(dependencies, body models, async markers, websocket/background/response
injection, security scopes). The caller then uses the interpreter, so behaviour
is identical and only the supported subset is accelerated.

Synchronous `K_PARAM_MARKER` slots - `Query()`, `Path()`, `Header()`,
`Cookie()` - are inlined too: their request source is read directly with no
`await`. Body / form / file markers stay on the interpreter because their
source (`await request.json()` / `await request.form()`) cannot be reached from
the synchronously-invoked compiled function.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, get_args, get_origin

from veloce._constants import MSG_FIELD_REQUIRED
from veloce._handler_plan import (
    K_PARAM_MARKER,
    K_PATH,
    K_QUERY,
    K_REQUEST,
    MK_COOKIE,
    MK_HEADER,
    MK_PATH,
    MK_QUERY,
    HandlerPlan,
)

# Slot kinds this compiler can emit straight-line code for. Anything else
# forces a fallback to the interpreter.
_SUPPORTED = frozenset({K_REQUEST, K_QUERY, K_PATH, K_PARAM_MARKER})

# Marker kinds whose request source is synchronous (no `await`). Body / form /
# file markers read `await request.json()` / `await request.form()` in the
# interpreter and so cannot be reproduced in the sync compiled function.
_SYNC_MARKERS = frozenset({MK_QUERY, MK_PATH, MK_HEADER, MK_COOKIE})

# Marker kinds that support repeated values via a list/set/tuple annotation.
# MK_PATH binds a single path segment, so it has no list form.
_LIST_MARKERS = frozenset({MK_QUERY, MK_HEADER, MK_COOKIE})

# Mirror of dependency._MARKER_LOC for the sync subset, used for the
# openapi-style `loc` strings the generated error payloads carry.
_MARKER_LOC = {MK_QUERY: "query", MK_PATH: "path", MK_HEADER: "header", MK_COOKIE: "cookie"}


class _Missing:
    __slots__ = ()


_MISSING = _Missing()


def compile_param_resolver(
    plan: HandlerPlan,
    coerce_value: Callable[[Any, Any, str, str], Any],
    validation_error: type[Exception],
) -> Callable[[Any, dict[str, str]], dict[str, Any]] | None:
    """Generate a `(request, path_params) -> kwargs` resolver for `plan`.

    Returns `None` unless every slot is request / scalar-path / scalar-query /
    a synchronous parameter marker - the cases the generated code reproduces
    exactly. `coerce_value` and `validation_error` are injected (rather than
    imported) to avoid a `dependency` <-> codegen import cycle.
    """
    slots = plan.slots
    for slot in slots:
        if slot.kind not in _SUPPORTED:
            return None
        if slot.kind == K_PARAM_MARKER and slot.marker_kind not in _SYNC_MARKERS:
            return None

    # Namespace seeded with the helpers and per-slot constants the body reads.
    ns: dict[str, Any] = {"_cv": coerce_value, "_RVE": validation_error, "_M": _MISSING}
    lines = ["def _resolver(request, path_params):", "    k = {}"]

    for j, slot in enumerate(slots):
        name = slot.name
        if slot.kind == K_REQUEST:
            lines.append(f"    k[{name!r}] = request")
            continue

        if slot.kind == K_PARAM_MARKER:
            _emit_marker(lines, ns, j, slot)
            continue

        # K_QUERY (path-or-query) and K_PATH share path lookup + coercion; they
        # differ only in the query fallback and the missing-required behaviour.
        ns[f"_t{j}"] = slot.target_type or str
        ns[f"_hd{j}"] = slot.has_default
        ns[f"_io{j}"] = slot.is_optional
        ns[f"_d{j}"] = slot.default

        lines.append(f"    _v = path_params.get({name!r}, _M)")
        lines.append("    if _v is not _M:")
        lines.append(f"        k[{name!r}] = _cv(_v, _t{j}, {name!r}, 'path')")
        if slot.kind == K_QUERY:
            lines.append("    else:")
            lines.append("        _qp = request.query_params")
            lines.append(f"        if {name!r} in _qp:")
            lines.append(f"            k[{name!r}] = _cv(_qp[{name!r}], _t{j}, {name!r}, 'query')")
            lines.append(f"        elif _hd{j}:")
            lines.append(f"            k[{name!r}] = _d{j}")
            lines.append(f"        elif _io{j}:")
            lines.append(f"            k[{name!r}] = None")
            lines.append("        else:")
            lines.append(
                f"            raise _RVE([{{'loc': ('query', {name!r}), "
                f"'msg': {MSG_FIELD_REQUIRED!r}, 'type': 'missing'}}])"
            )
        else:  # K_PATH - no query fallback, no missing-required error
            lines.append(f"    elif _hd{j}:")
            lines.append(f"        k[{name!r}] = _d{j}")
            lines.append(f"    elif _io{j}:")
            lines.append(f"        k[{name!r}] = None")

    lines.append("    return k")

    source = "\n".join(lines)
    try:
        exec(compile(source, "<veloce-resolver>", "exec"), ns)
    except SyntaxError:
        return None
    return ns["_resolver"]


def _emit_marker(lines: list[str], ns: dict[str, Any], j: int, slot: Any) -> None:
    """Emit straight-line code for a synchronous `K_PARAM_MARKER` slot.

    Mirrors `DependencyResolver._resolve_marker` for the query / path / header
    / cookie markers: the list-typed branch, the scalar source lookup, the
    None/default/optional/missing-required handling, the target-type coercion
    guard, and the `marker.validate(...)` constraint check wrapped so a
    `ValueError` becomes a `RequestValidationError`.
    """
    marker = slot.marker
    mk = slot.marker_kind
    lookup = slot.lookup_name
    loc = _MARKER_LOC[mk]
    name = slot.name

    ns[f"_t{j}"] = slot.target_type
    ns[f"_io{j}"] = slot.is_optional
    # `marker.validate` is bound once at registration; the per-request call is a
    # plain local invocation rather than an attribute lookup on the marker.
    ns[f"_val{j}"] = marker.validate

    # Decide static-vs-factory default once, here at codegen time. A static
    # default snapshots the value into the namespace and reads it inline (no
    # call overhead on the common path); a `default_factory` binds the callable
    # and emits a fresh call so each request gets an independent object.
    if marker.has_default:
        if marker.default_factory is not None:
            ns[f"_df{j}"] = marker.default_factory
            default_expr = f"_df{j}()"
        else:
            ns[f"_d{j}"] = marker.default
            default_expr = f"_d{j}"

    # List-typed query / header / cookie marker - collect every repeated value.
    if mk in _LIST_MARKERS and get_origin(slot.target_type) in (list, set, tuple):
        inner_args = get_args(slot.target_type)
        inner = inner_args[0] if inner_args else str
        ns[f"_in{j}"] = inner
        if mk == MK_HEADER:
            lines.append(f"    _vals = request.headers.getlist({lookup.lower()!r})")
        elif mk == MK_COOKIE:
            lines.append(f"    _vals = request.cookies.getlist({lookup!r})")
        else:  # MK_QUERY
            lines.append(f"    _vals = request.query_params.getlist({lookup!r})")
        lines.append("    if not _vals:")
        if marker.has_default:
            lines.append(f"        k[{name!r}] = {default_expr}")
        elif slot.is_optional:
            lines.append(f"        k[{name!r}] = None")
        else:
            lines.append(
                f"        raise _RVE([{{'loc': [{loc!r}, {name!r}], "
                f"'msg': 'Missing required parameter: {name}', "
                f"'type': 'value_error.missing'}}])"
            )
        lines.append("    else:")
        if inner is str:
            lines.append(f"        k[{name!r}] = list(_vals)")
        else:
            lines.append(
                f"        k[{name!r}] = [_cv(_x, _in{j}, {name!r}, {loc!r}) for _x in _vals]"
            )
        return

    # Scalar source lookup per marker kind.
    if mk == MK_PATH:
        lines.append(f"    _raw = path_params.get({lookup!r})")
    elif mk == MK_HEADER:
        lines.append(f"    _raw = request.headers.get({lookup.lower()!r})")
    elif mk == MK_COOKIE:
        lines.append(f"    _raw = request.cookies.get({lookup!r})")
    else:  # MK_QUERY
        lines.append(f"    _raw = request.query_params.get({lookup!r})")

    lines.append("    if _raw is None:")
    if marker.has_default:
        lines.append(f"        k[{name!r}] = {default_expr}")
    elif slot.is_optional:
        lines.append(f"        k[{name!r}] = None")
    else:
        lines.append(
            f"        raise _RVE([{{'loc': [{loc!r}, {name!r}], "
            f"'msg': 'Missing required parameter: {name}', "
            f"'type': 'value_error.missing'}}])"
        )
    lines.append("    else:")
    # target_type coercion guard - skip for str / None target and for raw
    # dict/list values (a JSON body cannot reach this sync path, but the guard
    # is kept identical to the interpreter).
    if slot.target_type is not None and slot.target_type is not str:
        lines.append("        if not isinstance(_raw, (dict, list)):")
        lines.append(f"            _raw = _cv(_raw, _t{j}, {name!r}, {loc!r})")
    lines.append("        try:")
    lines.append(f"            k[{name!r}] = _val{j}(_raw, {name!r})")
    lines.append("        except ValueError as _e:")
    lines.append(
        f"            raise _RVE([{{'loc': [{loc!r}, {name!r}], "
        f"'msg': str(_e), 'type': 'value_error'}}]) from _e"
    )
