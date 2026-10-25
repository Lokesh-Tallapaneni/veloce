"""Resolver codegen — registration-time codegen for handler resolution.

Runs at registration time only: the generated resolver is built once when a
route is registered and never recompiled on the request path. It replaces the
generic `_resolve_slots` interpreter with a straight-line `exec`-compiled
function that inlines each slot's name, target type and default.

`compile_param_resolver` handles the request plus synchronous `K_PARAM_MARKER`
slots - `Query()`, `Path()`, `Header()`, `Cookie()`. `compile_graph_resolver`
adds a no-wave dependency chain, including `yield`-teardown dependencies and
`Security(..., scopes=[...])`, whose scope union is a compile-time constant
because the graph edges fix it.

Either returns `None` for a plan it does not fully support - body / form / file
markers, parallelisable waves, overrides, MCP context - and the caller then uses
the interpreter, so behaviour is identical and only the subset is accelerated.
"""

from __future__ import annotations

import hashlib
import linecache
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, get_args, get_origin

from veloce._constants import (
    MSG_FIELD_REQUIRED,
    MSG_MISSING_PARAMETER,
    MSG_YIELD_NO_VALUE,
    STATE_INJECTED_RESPONSE,
)
from veloce._handler_plan import (
    _SEQUENCE_ORIGINS,
    K_BG_TASKS,
    K_DEPENDS,
    K_PARAM_MARKER,
    K_QUERY,
    K_REQUEST,
    K_RESPONSE,
    K_SECURITY_SCOPES,
    K_WEBSOCKET,
    MARKER_LOC,
    MK_COOKIE,
    MK_HEADER,
    MK_PATH,
    MK_QUERY,
    HandlerPlan,
)

# Slot kinds this compiler emits straight-line code for; anything else falls back.
_SUPPORTED = frozenset({K_REQUEST, K_QUERY, K_PARAM_MARKER})

# Slot kinds the graph compiler binds with no request-source lookup.
_GRAPH_BIND = frozenset({K_REQUEST, K_WEBSOCKET, K_BG_TASKS, K_RESPONSE})

# Synchronous sources; a body marker's `await` cannot be reproduced here.
_SYNC_MARKERS = frozenset({MK_QUERY, MK_PATH, MK_HEADER, MK_COOKIE})

# MK_PATH binds one path segment, so it has no repeated-value form.
_LIST_MARKERS = frozenset({MK_QUERY, MK_HEADER, MK_COOKIE})


class _Missing:
    __slots__ = ()


_MISSING = _Missing()


class _NotCompilable(Exception):
    """Raised mid-emit when a slot the pre-check missed cannot be compiled."""


@dataclass(slots=True)
class _EmitContext:
    """Compile-time state threaded through one graph emission.

    `n` is a monotonically increasing index shared across the whole tree so
    per-slot namespace keys (`_t{n}`, `_f{n}`, ...) and temp dict / result
    locals (`_kw{n}`, `_r{n}`) never collide between sub-plans. `dep_vars`
    maps a dependency's dedup key to the local holding its computed result, so
    a callable referenced more than once is emitted (and run) once - mirroring
    the interpreter's identity-keyed result cache. `in_progress` holds the keys
    whose emission has not finished, so a dependency reachable from itself is
    caught instead of emitted as a self-referential local. `scope_stack`
    mirrors `DependencyResolver._scope_stack`, but is walked at compile time:
    each `Security()` edge pushes its scopes for the duration of its sub-plan's
    emission, so every `SecurityScopes` slot below sees the same ordered union
    the interpreter would have built.
    """

    n: int = 0
    dep_vars: dict[Any, str] = field(default_factory=dict)
    in_progress: set[Any] = field(default_factory=set)
    scope_stack: list[str] = field(default_factory=list)


def _compile_resolver(source: str, kind: str, plan: HandlerPlan, ns: dict[str, Any]) -> Any:
    """Compile `source` into `ns` under a name a traceback can render.

    Generated code has no file, so a frame from it prints as
    `File "<name>", line N` with no source line unless something supplies one -
    and a fixed name would be shared by every resolver in the process, leaving
    the frame unable to say which route it came from. The name here carries the
    handler, and the source is registered in `linecache`, which is what the
    traceback machinery consults.

    `mtime` is `None` by the `linecache` convention for source with no file on
    disk: `checkcache` skips such an entry instead of stat-ing a path that does
    not exist and evicting it. Registration-time only, one entry per compiled
    resolver; the request path never touches this.

    Returns the resolver, or `None` if the generated source does not compile.
    """
    handler = getattr(plan, "handler", None)
    name = getattr(handler, "__qualname__", None) or getattr(handler, "__name__", "?")
    # Keyed by source digest, not a counter, so a recompile reuses one entry.
    digest = hashlib.blake2b(source.encode(), digest_size=6).hexdigest()
    filename = f"<veloce-{kind}:{name}:{digest}>"
    try:
        exec(compile(source, filename, "exec"), ns)
    except SyntaxError:
        return None
    if filename not in linecache.cache:
        linecache.cache[filename] = (len(source), None, source.splitlines(keepends=True), filename)
        _register_source_key(filename)
    return ns["_resolver"]


# A static app's listings are bounded by its distinct resolvers; one registering
# routes for its lifetime is not. FIFO, since only a traceback reads these.
_MAX_CACHED_SOURCES = 1024

# Kept separate from `linecache.cache` so eviction only touches our entries.
_cached_source_keys: dict[str, None] = {}


def _register_source_key(filename: str) -> None:
    """Record a `linecache` entry this module wrote, evicting the oldest at the cap."""
    _cached_source_keys[filename] = None
    while len(_cached_source_keys) > _MAX_CACHED_SOURCES:
        oldest = next(iter(_cached_source_keys))
        del _cached_source_keys[oldest]
        linecache.cache.pop(oldest, None)


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
        if slot.kind == K_REQUEST:
            lines.append(f"    k[{slot.name!r}] = request")
        elif slot.kind == K_PARAM_MARKER:
            _emit_marker(lines, ns, j, slot)
        else:  # K_QUERY (path-or-query)
            _emit_scalar_param(lines, ns, j, slot)

    lines.append("    return k")

    return _compile_resolver("\n".join(lines), "resolver", plan, ns)


def compile_graph_resolver(
    plan: HandlerPlan,
    coerce_value: Callable[[Any, Any, str, str], Any],
    validation_error: type[Exception],
    offload: Callable[..., Any],
    background_tasks_cls: type[Any],
    response_cls: type[Any],
    security_scopes_cls: type[Any],
) -> Callable[[Any, dict[str, str], list[tuple[str, Any]]], Any] | None:
    """Generate an `async (request, path_params) -> kwargs` resolver for `plan`.

    Returns `None` unless the whole dependency graph is compilable: no
    parallelisable waves (so awaiting in order preserves the interpreter's
    concurrency), only cacheable `Depends`, and only synchronous binding /
    parameter slots. A `Security()` scope chain does compile - the union each
    `SecurityScopes` parameter sees is static in the graph, so it is resolved
    here rather than on a per-request stack - and so does a `yield`-teardown
    dependency, whose generator is pushed onto the `teardowns` list the caller
    passes so `run_teardowns` drains it unchanged. The
    caller additionally restricts use of the result to requests with no active
    dependency overrides and no MCP context, the two pieces of resolver state
    the compiled body deliberately does not consult. The injected runtime hooks
    (`coerce_value`, `offload`, the `BackgroundTasks` / `Response` classes)
    are passed in to avoid a `dependency` <-> codegen import cycle.
    """
    if not _graph_compilable(plan, set()):
        return None

    ns: dict[str, Any] = {
        "_cv": coerce_value,
        "_RVE": validation_error,
        "_M": _MISSING,
        "_offload": offload,
        "_BG": background_tasks_cls,
        "_Resp": response_cls,
    }
    # Injected like the others so this module never imports `dependency`.
    ns["_SS"] = security_scopes_cls
    lines = ["async def _resolver(request, path_params, teardowns):", "    k = {}"]
    ctx = _EmitContext()
    try:
        for slot in plan.slots:
            _emit_graph_slot(lines, ns, slot, "k", ctx)
    except _NotCompilable:
        return None
    lines.append("    return k")

    return _compile_resolver("\n".join(lines), "graph-resolver", plan, ns)


# ── Compilability pre-check ───────────────────────────────
def _graph_compilable(plan: HandlerPlan, seen: set[int]) -> bool:
    """Whether `plan` and its whole sub-graph compile to a straight-line resolver.

    Rejects any plan with parallelisable waves (the interpreter runs those
    concurrently and a sequential compile would regress them), `yield`-teardown
    or non-cacheable dependencies, body / async markers, or any slot kind
    outside the binding / sync-param / `Depends` / `SecurityScopes` set.
    """
    if plan.wave_members:
        return False
    pid = id(plan)
    if pid in seen:
        return True
    seen.add(pid)
    for slot in plan.slots:
        kind = slot.kind
        if kind in _GRAPH_BIND or kind == K_QUERY:
            continue
        if kind == K_SECURITY_SCOPES:
            # The union is a compile-time constant; see `_emit_graph_slot`.
            continue
        if kind == K_PARAM_MARKER:
            if slot.marker_kind not in _SYNC_MARKERS:
                return False
            continue
        if kind == K_DEPENDS:
            # A non-cacheable dep needs a re-execution the dedup cannot express;
            # a `Security()` scope list and a `yield` dep are not bails.
            if not slot.use_cache:
                return False
            sub = slot.sub_plan
            if sub is None or not _graph_compilable(sub, seen):
                return False
            continue
        return False
    return True


# ── Graph emission ────────────────────────────────────────
def _emit_graph_slot(
    lines: list[str], ns: dict[str, Any], slot: Any, target: str, ctx: _EmitContext
) -> None:
    """Emit code binding one slot's value into the `target` kwargs dict."""
    kind = slot.kind
    name = slot.name

    if kind in (K_REQUEST, K_WEBSOCKET):
        lines.append(f"    {target}[{name!r}] = request")
        return

    if kind == K_BG_TASKS:
        lines.append("    if request._background_tasks is None:")
        lines.append("        request._background_tasks = _BG()")
        lines.append(f"    {target}[{name!r}] = request._background_tasks")
        return

    if kind == K_RESPONSE:
        # One Response per request; `status_code = 0` is the "not set by the
        # handler" sentinel. Re-emitted rather than calling
        # `DependencyResolver._bind_injected_response`, which measured 19.7%
        # slower when the slot is present (373 -> 447 ns/call).
        lines.append(f"    _inj = request._state.get({STATE_INJECTED_RESPONSE!r})")
        lines.append("    if _inj is None:")
        lines.append("        _inj = _Resp()")
        lines.append("        _inj.status_code = 0")
        lines.append(f"        request._state[{STATE_INJECTED_RESPONSE!r}] = _inj")
        lines.append(f"    {target}[{name!r}] = _inj")
        return

    if kind == K_SECURITY_SCOPES:
        # The `Security()` edges fix the union, so the list is a constant; the
        # `SecurityScopes` wrapper stays per request, as `scopes` is mutable.
        n = ctx.n
        ctx.n += 1
        sref = f"_sc{n}"
        ns[sref] = tuple(ctx.scope_stack)
        lines.append(f"    {target}[{name!r}] = _SS({sref})")
        return

    if kind == K_DEPENDS:
        var = _emit_dep(lines, ns, slot, ctx)
        lines.append(f"    {target}[{name!r}] = {var}")
        return

    j = ctx.n
    ctx.n += 1
    if kind == K_PARAM_MARKER:
        _emit_marker(lines, ns, j, slot, target)
        return
    if kind == K_QUERY:
        _emit_scalar_param(lines, ns, j, slot, target)
        return

    raise _NotCompilable


def _emit_dep(lines: list[str], ns: dict[str, Any], slot: Any, ctx: _EmitContext) -> str:
    """Emit a dependency's resolution and return the local holding its result.

    A callable already emitted returns its cached local unchanged. The dedup key
    mirrors `DependencyResolver._exec_depends`: callable identity alone for an
    ordinary dependency, and `(callable, frozenset(scopes))` for a
    scope-sensitive one, so the same callable reached through two different
    `Security()` paths is emitted - and run - twice rather than collapsing to
    one result, exactly as the interpreter resolves it.

    Sub-plan slots are emitted into a fresh temp dict first, then the callable
    is invoked - awaited for coroutines, offloaded when the slot opts in, called
    inline otherwise.
    """
    dep_callable = slot.dep_callable
    cid = id(dep_callable)
    scope_stack = ctx.scope_stack
    # A plain `Depends` carries none, so the scope path below is a no-op for it.
    new_scopes: list[str] = slot.target_type if isinstance(slot.target_type, list) else []
    if slot.scope_sensitive:
        key: Any = (cid, frozenset(scope_stack) | frozenset(new_scopes))
    else:
        key = cid
    dep_vars = ctx.dep_vars
    cached = dep_vars.get(key)
    if cached is not None:
        return cached
    in_progress = ctx.in_progress
    if key in in_progress:
        # A self-reachable dependency cannot be linearised; bail instead.
        raise _NotCompilable
    in_progress.add(key)

    n = ctx.n
    ctx.n += 1
    subkw = f"_kw{n}"
    var = f"_r{n}"
    fref = f"_f{n}"
    ns[fref] = dep_callable

    lines.append(f"    {subkw} = {{}}")
    sub = slot.sub_plan
    if new_scopes:
        scope_stack.extend(new_scopes)
    try:
        for sub_slot in sub.slots:
            _emit_graph_slot(lines, ns, sub_slot, subkw, ctx)
    finally:
        if new_scopes:
            del scope_stack[-len(new_scopes) :]

    if slot.dep_is_gen:
        # Registered right after the first `next`, so a later failure still tears
        # this one down - the interpreter's ordering.
        gref = f"_g{n}"
        mref = f"_m{n}"
        # Known at compile time, so the message is built once, not when raising.
        ns[mref] = MSG_YIELD_NO_VALUE.format(dependency=dep_callable)
        lines.append(f"    {gref} = {fref}(**{subkw})")
        lines.append("    try:")
        lines.append(f"        {var} = next({gref})")
        lines.append("    except StopIteration as err:")
        lines.append(f"        raise RuntimeError({mref}) from err")
        lines.append(f"    teardowns.append(('sync', {gref}))")
    elif slot.dep_is_async_gen:
        gref = f"_g{n}"
        mref = f"_m{n}"
        ns[mref] = MSG_YIELD_NO_VALUE.format(dependency=dep_callable)
        lines.append(f"    {gref} = {fref}(**{subkw})")
        lines.append("    try:")
        lines.append(f"        {var} = await {gref}.__anext__()")
        lines.append("    except StopAsyncIteration as err:")
        lines.append(f"        raise RuntimeError({mref}) from err")
        lines.append(f"    teardowns.append(('async', {gref}))")
    elif slot.dep_is_coro:
        lines.append(f"    {var} = await {fref}(**{subkw})")
    elif slot.dep_offload:
        lines.append(f"    {var} = await _offload({fref}, **{subkw})")
    else:
        lines.append(f"    {var} = {fref}(**{subkw})")

    in_progress.discard(key)
    dep_vars[key] = var
    return var


def _emit_coerced(
    lines: list[str],
    j: int,
    target_type: Any,
    lhs: str,
    value_expr: str,
    name: str,
    loc: str,
    indent: str,
) -> None:
    """Emit `lhs = <value_expr coerced to target_type>` at `indent`.

    `_coerce_value` decides what to do by walking a chain of `is` comparisons
    against the type - a question already answered here, at registration. The
    common answers are written into the source instead of re-asked per request:

      `str` / `Any`   the helper returns its argument unchanged, so the value is
                      read straight into the slot and there is no call at all.
      `int` / `float` the conversion itself, guarded to raise the same
                      `RequestValidationError` the helper raises for the same
                      input - error payload included, since a compiled route and
                      an interpreted one must answer a bad value identically.

    Anything else keeps the call. `bool` needs the helper's string handling,
    enums and `Literal` have their own branches, and the rest goes to Pydantic;
    reimplementing those in generated source would be duplication, not speed.

    Identity checks rather than a dict lookup: a target type is not always
    hashable, and hashing one to find out would cost more than the two compares.
    """
    if target_type is str or target_type is Any:
        lines.append(f"{indent}{lhs} = {value_expr}")
        return
    if target_type is int or target_type is float:
        converter = "int" if target_type is int else "float"
        message = f"Invalid value for {name}: expected {converter}"
        lines.append(f"{indent}try:")
        lines.append(f"{indent}    {lhs} = {converter}({value_expr})")
        lines.append(f"{indent}except (ValueError, TypeError) as _e:")
        lines.append(
            f"{indent}    raise _RVE([{{'loc': [{loc!r}, {name!r}], "
            f"'msg': {message!r}, 'type': 'type_error'}}]) from _e"
        )
        return
    lines.append(f"{indent}{lhs} = _cv({value_expr}, _t{j}, {name!r}, {loc!r})")


# ── Parameter / marker emission (shared with the param-only compiler) ──
def _default_expression(
    ns: dict[str, Any], index: int, factory: Callable[[], Any] | None, static: Any
) -> str:
    """Bind a default into the namespace and name it in the generated source.

    A static default is read inline, so the common path pays no call; a factory
    is called per request so each gets its own value.
    """
    if factory is not None:
        ns[f"_df{index}"] = factory
        return f"_df{index}()"
    ns[f"_d{index}"] = static
    return f"_d{index}"


def _emit_field_validate(
    lines: list[str],
    j: int,
    target: str,
    name: str,
    loc: str,
    indent: str,
    marker: Any,
    reported: str,
) -> None:
    """Emit the bound check for a source-less `Field` marker, or nothing.

    `reported` is the name the error names, which is not always the slot's: an
    aliased parameter is read and published under the alias, so reporting the
    Python parameter describes a key the client never sent.
    """
    if marker is None:
        return
    lines.append(f"{indent}try:")
    lines.append(f"{indent}    {target}[{name!r}] = _fv{j}({target}[{name!r}], {reported!r})")
    lines.append(f"{indent}except ValueError as _e:")
    lines.append(
        f"{indent}    raise _RVE([{{'loc': [{loc!r}, {reported!r}], "
        f"'msg': str(_e), 'type': 'value_error'}}]) from _e"
    )


def _emit_scalar_param(
    lines: list[str], ns: dict[str, Any], j: int, slot: Any, target: str = "k"
) -> None:
    """Emit a scalar `K_QUERY` (path-or-query) slot into `target`.

    Path lookup + coercion is shared; the kinds differ only in the query
    fallback and the missing-required behaviour.
    """
    name = slot.name
    ns[f"_t{j}"] = slot.target_type or str
    # A plain mutable default is wrapped in a copying factory at registration.
    default_expr = (
        _default_expression(ns, j, slot.default_factory, slot._static_default)
        if slot.has_default
        else None
    )

    effective = slot.target_type or str
    # A source-less `Field` marker binds whichever source answered.
    marker = slot.marker if slot.marker_validates else None
    if marker is not None:
        ns[f"_fv{j}"] = marker.validate
    # The alias names the query key; the path keys on the declared name.
    lookup = slot.lookup_name

    lines.append(f"    _v = path_params.get({name!r}, _M)")
    lines.append("    if _v is not _M:")
    _emit_coerced(lines, j, effective, f"{target}[{name!r}]", "_v", name, "path", " " * 8)
    _emit_field_validate(lines, j, target, name, "path", " " * 8, marker, name)
    lines.append("    else:")
    lines.append("        _qp = request.query_params")
    lines.append(f"        if {lookup!r} in _qp:")
    # Reported under `lookup`, the key the client sent, as the interpreter does.
    _emit_coerced(
        lines, j, effective, f"{target}[{name!r}]", f"_qp[{lookup!r}]", lookup, "query", " " * 12
    )
    _emit_field_validate(lines, j, target, name, "query", " " * 12, marker, lookup)
    lines.append("        else:")
    if default_expr is not None:
        lines.append(f"            {target}[{name!r}] = {default_expr}")
    elif slot.is_optional:
        lines.append(f"            {target}[{name!r}] = None")
    else:
        lines.append(
            f"            raise _RVE([{{'loc': ('query', {lookup!r}), "
            f"'msg': {MSG_FIELD_REQUIRED!r}, 'type': 'missing'}}])"
        )


def _emit_marker(
    lines: list[str], ns: dict[str, Any], j: int, slot: Any, target: str = "k"
) -> None:
    """Emit straight-line code for a synchronous `K_PARAM_MARKER` slot into `target`.

    Mirrors `DependencyResolver._resolve_marker` for the query / path / header
    / cookie markers: the list-typed branch, the scalar source lookup, the
    None/default/optional/missing-required handling, the target-type coercion
    guard, and the `marker.validate(...)` constraint check wrapped so a
    `ValueError` becomes a `RequestValidationError`.
    """
    marker = slot.marker
    mk = slot.marker_kind
    lookup = slot.lookup_name
    loc = MARKER_LOC[mk]
    name = slot.name

    ns[f"_t{j}"] = slot.target_type
    # Bound once at registration, so the per-request call is a plain local.
    ns[f"_val{j}"] = marker.validate

    # `None` when the parameter declares no default, which the emit sites branch on.
    marker_default = (
        _default_expression(ns, j, marker.default_factory, marker.default)
        if marker.has_default
        else None
    )

    # List-typed query / header / cookie marker - collect every repeated value.
    from veloce.dependency import _fixed_shape_args  # breaks a dependency<->codegen cycle

    if mk in _LIST_MARKERS and get_origin(slot.target_type) in _SEQUENCE_ORIGINS:
        inner_args = get_args(slot.target_type)
        inner = inner_args[0] if inner_args else str
        ns[f"_in{j}"] = inner
        # Only a homogeneous `list[T]` inlines; the rest take the shared helper.
        simple = _fixed_shape_args(slot.target_type) is None and (
            get_origin(slot.target_type) is list
        )
        if mk == MK_HEADER:
            lines.append(f"    _vals = request.headers.getlist({lookup.lower()!r})")
        elif mk == MK_COOKIE:
            lines.append(f"    _vals = request.cookies.getlist({lookup!r})")
        else:  # MK_QUERY
            lines.append(f"    _vals = request.query_params.getlist({lookup!r})")
        lines.append("    if not _vals:")
        if marker_default is not None:
            lines.append(f"        {target}[{name!r}] = {marker_default}")
        elif slot.is_optional:
            lines.append(f"        {target}[{name!r}] = None")
        else:
            lines.append(
                f"        raise _RVE([{{'loc': [{loc!r}, {name!r}], "
                f"'msg': {MSG_MISSING_PARAMETER.format(name=name)!r}, "
                f"'type': 'value_error.missing'}}])"
            )
        lines.append("    else:")
        # Registration-time; `dependency` imports this module, breaking the cycle.
        from veloce.dependency import _collect_sequence_marker, _validate_list_marker

        if not simple:
            ns[f"_cs{j}"] = _collect_sequence_marker
            ns[f"_tt{j}"] = slot.target_type
            lines.append(f"        {target}[{name!r}] = _cs{j}(_vals, _tt{j}, {name!r}, {loc!r})")
        elif inner is str:
            lines.append(f"        {target}[{name!r}] = list(_vals)")
        else:
            lines.append(
                f"        {target}[{name!r}] = [_cv(_x, _in{j}, {name!r}, {loc!r}) for _x in _vals]"
            )

        # The plan's resolved tuple binds here, not read back through the marker.
        list_checks = slot.list_checks
        if list_checks is not None:
            ns[f"_lv{j}"] = _validate_list_marker
            ns[f"_lc{j}"] = list_checks
            lines.append(f"        _lv{j}({target}[{name!r}], _lc{j}, {name!r}, {loc!r})")
        return

    # Scalar source lookup per marker kind.
    if mk == MK_PATH:
        lines.append(f"    _raw = path_params.get({lookup!r})")
    elif mk == MK_HEADER:
        # One header, no whole mapping: `Header()` paid ~2.7 us to decode them all.
        key = lookup.lower().encode("latin-1")
        lines.append(f"    _raw = request._peek_header_key({key!r})")
    elif mk == MK_COOKIE:
        lines.append(f"    _raw = request.cookies.get({lookup!r})")
    else:  # MK_QUERY
        lines.append(f"    _raw = request.query_params.get({lookup!r})")

    lines.append("    if _raw is None:")
    if marker_default is not None:
        lines.append(f"        {target}[{name!r}] = {marker_default}")
    elif slot.is_optional:
        lines.append(f"        {target}[{name!r}] = None")
    else:
        lines.append(
            f"        raise _RVE([{{'loc': [{loc!r}, {name!r}], "
            f"'msg': {MSG_MISSING_PARAMETER.format(name=name)!r}, "
            f"'type': 'value_error.missing'}}])"
        )
    lines.append("    else:")
    # Skipped for a str / None target; the dict/list guard mirrors the interpreter.
    if slot.target_type is not None and slot.target_type is not str:
        lines.append("        if not isinstance(_raw, (dict, list)):")
        lines.append(f"            _raw = _cv(_raw, _t{j}, {name!r}, {loc!r})")
    lines.append("        try:")
    lines.append(f"            {target}[{name!r}] = _val{j}(_raw, {name!r})")
    lines.append("        except ValueError as _e:")
    lines.append(
        f"            raise _RVE([{{'loc': [{loc!r}, {name!r}], "
        f"'msg': str(_e), 'type': 'value_error'}}]) from _e"
    )
