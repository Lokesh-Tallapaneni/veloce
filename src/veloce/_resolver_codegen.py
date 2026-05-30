"""Registration-time code generation for parameter-only handler resolvers.

For handlers whose plan binds only the request and scalar path/query
parameters, the generic `_resolve_slots` interpreter (a `while` loop with a
per-slot `kind` dispatch) is replaced by a straight-line function generated
once at registration and `exec`-compiled. The generated function inlines each
slot's name, target type, and default, so the request path does no loop, no
kind branching, and no slot attribute lookups.

`compile_param_resolver` returns `None` for any plan it does not fully support
(dependencies, body models, markers, list params, websocket/background/response
injection, security scopes). The caller then uses the interpreter, so behaviour
is identical and only the supported subset is accelerated.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from veloce._handler_plan import (
    K_PATH,
    K_QUERY,
    K_REQUEST,
    HandlerPlan,
)

# Slot kinds this compiler can emit straight-line code for. Anything else
# forces a fallback to the interpreter.
_SUPPORTED = frozenset({K_REQUEST, K_QUERY, K_PATH})


class _Missing:
    __slots__ = ()


_MISSING = _Missing()


def compile_param_resolver(
    plan: HandlerPlan,
    coerce_value: Callable[[Any, Any, str, str], Any],
    validation_error: type[Exception],
) -> Callable[[Any, dict[str, str]], dict[str, Any]] | None:
    """Generate a `(request, path_params) -> kwargs` resolver for `plan`.

    Returns `None` unless every slot is request / scalar-path / scalar-query —
    the cases the generated code reproduces exactly. `coerce_value` and
    `validation_error` are injected (rather than imported) to avoid a
    `dependency` <-> codegen import cycle.
    """
    slots = plan.slots
    for slot in slots:
        if slot.kind not in _SUPPORTED:
            return None

    # Namespace seeded with the helpers and per-slot constants the body reads.
    ns: dict[str, Any] = {"_cv": coerce_value, "_RVE": validation_error, "_M": _MISSING}
    lines = ["def _resolver(request, path_params):", "    k = {}"]

    for j, slot in enumerate(slots):
        name = slot.name
        if slot.kind == K_REQUEST:
            lines.append(f"    k[{name!r}] = request")
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
                f"'msg': 'field required', 'type': 'missing'}}])"
            )
        else:  # K_PATH — no query fallback, no missing-required error
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
