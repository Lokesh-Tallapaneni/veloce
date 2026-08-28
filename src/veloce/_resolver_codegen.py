"""Resolver codegen — registration-time codegen for handler resolution.

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

`compile_graph_resolver` extends the same technique to a no-wave dependency
graph: a plan whose dependencies form a linear chain (no parallel-safe
batching the interpreter would otherwise run with `asyncio.gather`), with no
Security scopes, no `yield`-teardown dependencies, and no body / async markers.
Such a graph has no concurrency to preserve, so a straight-line `async`
resolver that awaits each dependency in order is behaviour-identical to the
interpreter while removing the per-slot dispatch. Plans with parallelisable
waves, overrides, or MCP context fall back to the interpreter, which keeps their
`gather` batching and stateful semantics.

A `yield`-teardown dependency compiles too: the generator is started here and
pushed onto the teardown list the caller passes in, which `run_teardowns` drains
in reverse exactly as it does for the interpreted path.

`Security(..., scopes=[...])` chains compile. The scope union a `SecurityScopes`
parameter sees is fixed by the graph edges, so it is computed once here and
baked in as a constant rather than accumulated on a per-request stack.

Synchronous `K_PARAM_MARKER` slots - `Query()`, `Path()`, `Header()`,
`Cookie()` - are inlined too: their request source is read directly with no
`await`. Body / form / file markers stay on the interpreter because their
source (`await request.json()` / `await request.form()`) cannot be reached from
the synchronously-invoked compiled function.
"""

from __future__ import annotations

import hashlib
import linecache
from collections.abc import Callable
from typing import Any, get_args, get_origin

from veloce._constants import MSG_FIELD_REQUIRED, STATE_INJECTED_RESPONSE
from veloce._handler_plan import (
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

# Slot kinds this compiler can emit straight-line code for. Anything else
# forces a fallback to the interpreter.
_SUPPORTED = frozenset({K_REQUEST, K_QUERY, K_PARAM_MARKER})

# Slot kinds the graph compiler binds with no request-source lookup: the
# request itself, the WebSocket (bound the same way on a WS resolve), the
# per-request background-task collector, and the injected Response.
_GRAPH_BIND = frozenset({K_REQUEST, K_WEBSOCKET, K_BG_TASKS, K_RESPONSE})

# Marker kinds whose request source is synchronous (no `await`). Body / form /
# file markers read `await request.json()` / `await request.form()` in the
# interpreter and so cannot be reproduced in the sync compiled function.
_SYNC_MARKERS = frozenset({MK_QUERY, MK_PATH, MK_HEADER, MK_COOKIE})

# Same wording `DependencyResolver._exec_depends` raises, so both paths report a
# non-yielding dependency identically. The callable is known at compile time, so
# the message is built once per dependency rather than on the raise path.
_MSG_NO_YIELD = "yield dependency {!r} returned without yielding a value"

# Marker kinds that support repeated values via a list/set/tuple annotation.
# MK_PATH binds a single path segment, so it has no list form.
_LIST_MARKERS = frozenset({MK_QUERY, MK_HEADER, MK_COOKIE})


class _Missing:
    __slots__ = ()


_MISSING = _Missing()


class _NotCompilable(Exception):
    """Raised mid-emit when a slot the pre-check missed cannot be compiled."""


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
    # Keyed by a digest of the source rather than a counter, so recompiling the
    # same plan - which a test suite or a re-registered route does - reuses one
    # entry instead of adding another that nothing will ever free. The bound is
    # the number of distinct resolvers, which is what it should be.
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


# The digest key means recompiling the same plan reuses one entry, so a static
# app's listings are bounded by its distinct resolvers. A process that registers
# routes over its lifetime - per-tenant routers, a plugin adding routes at
# runtime - has no such bound, and every distinct handler shape would retain a
# full source listing forever for something only a traceback ever reads.
#
# Bounded FIFO rather than LRU: recency is not worth tracking for entries only a
# traceback consults, and a static app never reaches the cap. When a dynamic one
# does, the oldest listing is dropped and a frame from that resolver prints
# without its source line - which is what every frame from generated code did
# before this registration existed.
_MAX_CACHED_SOURCES = 1024

# Insertion-ordered set of the `linecache` keys this module owns. Kept separate
# from `linecache.cache` so eviction only ever touches entries written here.
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
    background_tasks_cls: type,
    response_cls: type,
    security_scopes_cls: type,
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
    # `n` is a monotonically increasing index shared across the whole tree so
    # per-slot namespace keys (`_t{n}`, `_f{n}`, ...) and temp dict / result
    # locals (`_kw{n}`, `_r{n}`) never collide between sub-plans. `dep_vars`
    # maps a dependency callable's identity to the local holding its computed
    # result, so a callable referenced more than once is emitted (and run)
    # once - mirroring the interpreter's identity-keyed result cache.
    # `scope_stack` mirrors `DependencyResolver._scope_stack`, but is walked at
    # compile time: each `Security()` edge pushes its scopes for the duration of
    # its sub-plan's emission, so every `SecurityScopes` slot below sees the
    # same ordered union the interpreter would have built.
    ctx: dict[str, Any] = {
        "n": 0,
        "dep_vars": {},
        "in_progress": set(),
        "scope_stack": [],
    }
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
            # Non-cacheable deps would need a runtime re-execution the dedup
            # below cannot express. A `Security()` scope list is not a bail: it
            # is walked at compile time and keyed into `dep_vars` the way the
            # interpreter keys its cache. Nor is a `yield` dependency: its
            # generator is pushed onto the caller's teardown list in emission
            # order, which is the order the interpreter pushes in.
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
    lines: list[str], ns: dict[str, Any], slot: Any, target: str, ctx: dict[str, Any]
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
        # One Response per request, shared with any dependency that also
        # declares the parameter; `status_code = 0` is the dispatcher's
        # "not set by the handler" sentinel. Mirrors
        # `DependencyResolver._bind_injected_response`, which is the spec this
        # emitter tracks - deliberately re-emitted rather than called.
        #
        # Emitting `k[name] = _bind(request)` instead was measured against this
        # straight-line form: 19.7% slower when the slot is already present
        # (373 -> 447 ns/call) and 10.6% slower when the Response is constructed
        # (1183 -> 1308 ns/call), with the identical-work control inside the
        # noise floor both times (-1.3% and +4.2%). A helper call per request is
        # exactly what `compile_graph_resolver` exists to remove, so the copy
        # stays. The shared key constant costs nothing (it is inlined into the
        # generated source below), so only the body is duplicated.
        lines.append(f"    _inj = request._state.get({STATE_INJECTED_RESPONSE!r})")
        lines.append("    if _inj is None:")
        lines.append("        _inj = _Resp()")
        lines.append("        _inj.status_code = 0")
        lines.append(f"        request._state[{STATE_INJECTED_RESPONSE!r}] = _inj")
        lines.append(f"    {target}[{name!r}] = _inj")
        return

    if kind == K_SECURITY_SCOPES:
        # The scope union is fixed by the `Security()` edges above this slot, so
        # the list is a compile-time constant. The `SecurityScopes` wrapper is
        # still built per request: `scopes` is a plain list a dependency may
        # mutate, and the interpreter hands out a fresh instance every time, so
        # sharing one here would let one request's mutation reach the next.
        # `SecurityScopes.__init__` copies the list, so the constant below is
        # never the object a dependency can reach.
        n = ctx["n"]
        ctx["n"] += 1
        sref = f"_sc{n}"
        ns[sref] = tuple(ctx["scope_stack"])
        lines.append(f"    {target}[{name!r}] = _SS({sref})")
        return

    if kind == K_DEPENDS:
        var = _emit_dep(lines, ns, slot, ctx)
        lines.append(f"    {target}[{name!r}] = {var}")
        return

    j = ctx["n"]
    ctx["n"] += 1
    if kind == K_PARAM_MARKER:
        _emit_marker(lines, ns, j, slot, target)
        return
    if kind == K_QUERY:
        _emit_scalar_param(lines, ns, j, slot, target)
        return

    raise _NotCompilable


def _emit_dep(lines: list[str], ns: dict[str, Any], slot: Any, ctx: dict[str, Any]) -> str:
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
    scope_stack: list[str] = ctx["scope_stack"]
    # The scopes this edge contributes. A plain `Depends` carries none, so the
    # whole scope path below is a no-op for it.
    new_scopes: list[str] = slot.target_type if isinstance(slot.target_type, list) else []
    if slot.scope_sensitive:
        key: Any = (cid, frozenset(scope_stack) | frozenset(new_scopes))
    else:
        key = cid
    dep_vars = ctx["dep_vars"]
    cached = dep_vars.get(key)
    if cached is not None:
        return cached
    in_progress = ctx["in_progress"]
    if key in in_progress:
        # A dependency reachable from itself cannot be linearised; bail to the
        # interpreter rather than emit a self-referential local.
        raise _NotCompilable
    in_progress.add(key)

    n = ctx["n"]
    ctx["n"] += 1
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
        # Start it, take the first yield as the result, and register the live
        # generator so `run_teardowns` advances past the yield. Registration
        # happens immediately after the first `next`, so a failure later in the
        # graph still tears this one down - the same ordering the interpreter
        # gets from appending at this point.
        gref = f"_g{n}"
        mref = f"_m{n}"
        ns[mref] = _MSG_NO_YIELD.format(dep_callable)
        lines.append(f"    {gref} = {fref}(**{subkw})")
        lines.append("    try:")
        lines.append(f"        {var} = next({gref})")
        lines.append("    except StopIteration as err:")
        lines.append(f"        raise RuntimeError({mref}) from err")
        lines.append(f"    teardowns.append(('sync', {gref}))")
    elif slot.dep_is_async_gen:
        gref = f"_g{n}"
        mref = f"_m{n}"
        ns[mref] = _MSG_NO_YIELD.format(dep_callable)
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
def _emit_scalar_param(
    lines: list[str], ns: dict[str, Any], j: int, slot: Any, target: str = "k"
) -> None:
    """Emit a scalar `K_QUERY` (path-or-query) slot into `target`.

    Path lookup + coercion is shared; the kinds differ only in the query
    fallback and the missing-required behaviour.
    """
    name = slot.name
    ns[f"_t{j}"] = slot.target_type or str
    ns[f"_hd{j}"] = slot.has_default
    ns[f"_io{j}"] = slot.is_optional
    # A plain mutable default is wrapped in a copying factory at registration so
    # each request gets its own value (see `_guard_plain_mutable_default`); emit
    # a fresh call for it, and read an immutable default inline.
    if slot.default_factory is not None:
        ns[f"_df{j}"] = slot.default_factory
        default_expr = f"_df{j}()"
    else:
        ns[f"_d{j}"] = slot._static_default
        default_expr = f"_d{j}"

    effective = slot.target_type or str
    lines.append(f"    _v = path_params.get({name!r}, _M)")
    lines.append("    if _v is not _M:")
    _emit_coerced(lines, j, effective, f"{target}[{name!r}]", "_v", name, "path", " " * 8)
    lines.append("    else:")
    lines.append("        _qp = request.query_params")
    lines.append(f"        if {name!r} in _qp:")
    _emit_coerced(
        lines, j, effective, f"{target}[{name!r}]", f"_qp[{name!r}]", name, "query", " " * 12
    )
    lines.append(f"        elif _hd{j}:")
    lines.append(f"            {target}[{name!r}] = {default_expr}")
    lines.append(f"        elif _io{j}:")
    lines.append(f"            {target}[{name!r}] = None")
    lines.append("        else:")
    lines.append(
        f"            raise _RVE([{{'loc': ('query', {name!r}), "
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
            lines.append(f"        {target}[{name!r}] = {default_expr}")
        elif slot.is_optional:
            lines.append(f"        {target}[{name!r}] = None")
        else:
            lines.append(
                f"        raise _RVE([{{'loc': [{loc!r}, {name!r}], "
                f"'msg': 'Missing required parameter: {name}', "
                f"'type': 'value_error.missing'}}])"
            )
        lines.append("    else:")
        if inner is str:
            lines.append(f"        {target}[{name!r}] = list(_vals)")
        else:
            lines.append(
                f"        {target}[{name!r}] = [_cv(_x, _in{j}, {name!r}, {loc!r}) for _x in _vals]"
            )
        return

    # Scalar source lookup per marker kind.
    if mk == MK_PATH:
        lines.append(f"    _raw = path_params.get({lookup!r})")
    elif mk == MK_HEADER:
        # One header, read without building the whole mapping - see
        # `Request._peek_header_key`. A `Header()` parameter is the case that
        # paid ~2.7 us to decode every header and look at one of them.
        key = lookup.lower().encode("latin-1")
        lines.append(f"    _raw = request._peek_header_key({key!r})")
    elif mk == MK_COOKIE:
        lines.append(f"    _raw = request.cookies.get({lookup!r})")
    else:  # MK_QUERY
        lines.append(f"    _raw = request.query_params.get({lookup!r})")

    lines.append("    if _raw is None:")
    if marker.has_default:
        lines.append(f"        {target}[{name!r}] = {default_expr}")
    elif slot.is_optional:
        lines.append(f"        {target}[{name!r}] = None")
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
    lines.append(f"            {target}[{name!r}] = _val{j}(_raw, {name!r})")
    lines.append("        except ValueError as _e:")
    lines.append(
        f"            raise _RVE([{{'loc': [{loc!r}, {name!r}], "
        f"'msg': str(_e), 'type': 'value_error'}}]) from _e"
    )
