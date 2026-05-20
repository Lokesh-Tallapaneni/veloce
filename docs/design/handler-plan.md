# Design: HandlerPlan (D15 — hot-path reflection elimination)

## Contract

Move all `inspect.signature` and `typing.get_type_hints` work out of the per-request
path. A `HandlerPlan` is built **once**, at route registration time, and attached
to the `RouteInfo`. The plan is a list of pre-resolved `_Slot` records — one per
handler parameter — each tagged with the source the value should be read from
(`REQUEST`, `BG_TASKS`, `DEPENDS`, `PARAM_MARKER`, `PATH/QUERY`, `QUERY_LIST`,
`BODY_MODEL`, `UPLOAD_FILE`).

The `DependencyResolver` consumes the plan with a single loop and a small
integer-tag switch. No reflection, no `get_type_hints` round-trip, no
`inspect.signature` walk on the hot path.

## Observable behavior (must be unchanged)

The change is purely internal. Every test in `tests/` that exercised
`DependencyResolver` before must pass unchanged. In particular:

- Parameters annotated `request: Request` continue to receive the request.
- `BackgroundTasks`-annotated parameters continue to receive the lazy queue.
- `Depends(callable, use_cache=True|False)` continues to resolve recursively
  with one cache per request, and `use_cache=False` re-resolves each call site.
- `Query`/`Path`/`Header`/`Cookie`/`Body`/`Form`/`File` markers continue to
  read from the same sources with the same coercion and constraint rules.
- Pydantic `BaseModel` body parameters continue to deserialize via
  `model_validate` and raise `ValidationError` on failure.
- `UploadFile` parameters continue to come from the parsed form.
- Optional parameters with no value continue to default to `None`.
- `app.dependency_overrides[fn] = fake_fn` continues to substitute the
  replacement at resolve time; sub-plans for the override are built on-the-fly
  (only on first call) since the override signature is required by the contract
  to match the original.

## Data model

```
HandlerPlan
  handler:        Callable           # original handler ref, for error messages
  is_coro:        bool               # cached once
  slots:          list[_Slot]        # one per handler parameter
  route_dep_plans:list[_Slot]        # K_DEPENDS slots for route-level deps

_Slot
  kind:           int                # K_REQUEST..K_NONE
  name:           str                # handler param name (kwargs key)
  target_type:    Any                # coercion target for K_QUERY/K_PARAM_MARKER
  default:        Any                # for K_QUERY/K_QUERY_LIST/K_DEFAULT
  has_default:    bool
  is_optional:    bool
  list_inner:     Any                # element type for K_QUERY_LIST
  model:          type[BaseModel]    # for K_BODY_MODEL
  marker:         _ParamBase         # for K_PARAM_MARKER
  marker_kind:    int                # MK_QUERY..MK_FILE
  lookup_name:    str                # alias or param name
  sub_plan:       HandlerPlan        # for K_DEPENDS
  use_cache:      bool               # for K_DEPENDS
  dep_callable:   Callable           # for K_DEPENDS
  dep_is_coro:    bool               # cached once
```

All fields packed onto one class so the hot loop branches on a single int tag
rather than dispatching on `type(slot)`. `__slots__` everywhere — these objects
are small and there is one per handler parameter.

## Hot-path budget (per request, excluding I/O)

Before:
- `inspect.signature(handler)` — ~30–80 µs
- `get_type_hints(handler)` — ~80–200 µs (resolves forward refs, walks MRO)
- per-parameter Python loop with isinstance/issubclass — ~5 µs × N
- recursive resolution for `Depends` — `inspect.signature` again for every dep

After:
- list iteration over `len(slots)` `_Slot` objects
- one int compare + attribute read per slot
- no signature/hints work; sub-deps reuse their pre-built sub-plans

Expected dispatch overhead drop: ≈200 µs → ≈10 µs on a 3-parameter handler with
one `Depends`. Order-of-magnitude improvement on simple endpoints where the
handler body itself is microseconds; measurable but smaller on endpoints with
heavy I/O.

## Threading model

`HandlerPlan` is **immutable** after construction. No locks needed at read time.
`DependencyResolver._cache` is per-request (cleared on entry to `resolve`), so
it is implicitly single-threaded per request. The plan is shared across
concurrent requests without mutation. Free-threaded (no-GIL) Python: safe.

## Public API

No public-API change. Internal only. `RouteInfo` gains:
- `handler_plan: HandlerPlan` — built in `Router.add_route` after the
  `RouteInfo` is constructed.
- `route_dep_plans: list[_Slot]` — pre-planned K_DEPENDS slots for the
  `dependencies=` route kwarg.

`DependencyResolver` gains:
- `resolve_plan(plan, request, path_params, route_dep_plans)` — the new
  fast path.
- `resolve(handler, request, path_params, route_dependencies)` is kept as a
  thin wrapper that builds the plan on demand for callers that do not
  pre-plan (e.g. tests).

## Trade-offs

- **Registration is slower** by a small constant per route. A 100-route app
  pays a one-time ~10–50 ms cost at import vs ~200 µs × requests-per-second
  ongoing. Net win even on dev workloads.
- **Plan construction must be tolerant**: handlers with broken `__annotations__`
  or forward refs that can't be resolved fall through to an empty-plan
  `HandlerPlan` (all params come from `param.default` if any). The
  `get_type_hints` call is guarded with a broad `except` for this reason.
- **Dependency overrides** require building a sub-plan on first call because
  the override callable wasn't known at registration. We cache the sub-plan
  on the resolver after the first build. Trade: a tiny first-call cost on
  overridden tests; zero cost in production.

## References

- Optimization principle: no reflection on the hot path — do all
  introspection at registration time (route decoration, dependency
  registration, model compilation) and cache the result in a frozen
  plan object.
- PEP 563 + PEP 649: stringified annotations resolved via `get_type_hints`.
