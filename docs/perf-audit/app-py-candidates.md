# app.py — perf candidates (Wave 1)

Profile: `bench/profile_hot.py`, 16k requests, json-hello + path-param.
app.py owns ~50% of tottime via `_asgi_app` (19.8%), `_dispatch_request`
(13.4%), `handle_request` (8.0%), `_coerce_response` (2.3%), `__call__`
(2.3%), `_call_handler` (1.6%), `_run_response_middleware` (1.0%).

DSA / micro-opt techniques considered:

- **Branch ordering by frequency** — `isinstance` chains check the
  most-common type first; rare branches last.
- **Pre-encoded constants** — values that are deterministic per response
  class (e.g. `b"application/json"` for `JSONResponse`) get encoded once
  at class definition, not per request.
- **Allocation skip on no-op paths** — objects only built when a flag
  proves they'll be used (resolver, asgi_stack, header lists).
- **Lazy-await elimination** — an `await fn()` where `fn` has an empty
  body still pays coroutine creation + frame setup; gate the call.
- **Dedup of work across nested frames** — same config lookup done in
  parent and child frame collapses to one.
- **`dict.__getitem__` over `.get(...)` when key presence is guaranteed
  by the ASGI spec** (path, method).
- **Identity check on interned class constants** — `is` against a
  module-level string short-circuits before a full `==` walk.

## Candidates

### W1.A — Skip `DependencyResolver` allocation for trivial-plan routes

**Where:** `_dispatch_request` at line 1884.

```python
resolver = DependencyResolver()
resolver._overrides = self._dependency_overrides
resolver._override_subplans = self._override_subplans
```

This allocation + two attribute writes happens unconditionally. When
`route_info.is_trivial_plan` is True (json-hello case) the trivial
branch at line 2026 assigns `kwargs = {}` and never uses the resolver.
The allocation and writes are pure waste for trivial routes.

**Proposed:** match the route first, then allocate the resolver only
when `not route_info.is_trivial_plan`. The hooks before route match
(`before_request`, `_url_value_preprocessors`, blueprint hooks) do
not require the resolver; the few branches that *do* use it (mounted
sub-app, static, after_request hook) can keep a lazy allocator
helper.

**Profile evidence:** `dependency.__init__` is 0.023s / 16k = ~1.5%.
Trivial-route fraction of that ≈ 50% (json-hello is trivial,
path-param is not) → ~0.7% headline saving from this one change.

### W1.B — Pre-encoded content-type for `JSONResponse` / known classes

**Where:** `_asgi_app` at line 2932-2938.

```python
asgi_headers: list[tuple[bytes, bytes]] = [
    (
        b"content-type",
        _reject_header_crlf(response.content_type, "content-type").encode(),
    ),
    (b"content-length", str(content_length).encode()),
]
```

Per request, `_reject_header_crlf` walks the content-type string for
CRLF/NUL and `.encode()` ASCII-encodes a (typically) constant string.
For `JSONResponse` the content_type is the literal `"application/json"`
every time. The validation+encode round-trip is deterministic.

**Proposed:** stamp a `_content_type_bytes` class attribute on
`Response` subclasses with deterministic content types (`JSONResponse`,
`ORJSONResponse`, `HTMLResponse`, `PlainTextResponse`). At emit, prefer
`response._content_type_bytes` if it equals the current `content_type`
(handles handler-side override); otherwise fall back to the validate +
encode path. Identity check via class attribute means the common path
is one attribute load + `.encode()` skipped.

**Profile evidence:** `_reject_header_crlf` 0.007s + bytes.encode under
`_asgi_app` is part of the 0.299s envelope. Conservative estimate
~0.3% saving — but this is per-response so it stacks with W1.B.5
(below) for content-length.

### W1.C — Cache `str(content_length).encode()` for the small-int hot range

**Where:** `_asgi_app` line 2937, `(b"content-length", str(content_length).encode())`.

The content-length integer is bounded for the hot path: `/` returns
14 bytes, `/items/42` returns ~17 bytes, both small. `str(n).encode()`
allocates two objects per request (a str, then a bytes).

**Proposed:** module-level `_CL_BYTES_CACHE: dict[int, bytes] = {}` with
LRU-bounded size, OR pre-build `_CL_BYTES_SMALL = [str(i).encode() for
i in range(256)]` and index by content_length when in range. Beyond 256
fall through to the existing path. The bound is to avoid the cache
growing unbounded under variable-size responses.

**Profile evidence:** not visible as its own frame (called per request
inside `_asgi_app`). Sub-microsecond per call, but every request pays it.

### W1.D — Short-circuit `_run_response_middleware` at every call site

**Where:** `_dispatch_request`, multiple `await self._run_response_middleware(...)`
calls. Function body at line 2465.

```python
async def _run_response_middleware(self, request, response):
    for mw in reversed(self._middlewares):
        response = await mw.process_response(request, response)
    return response
```

When `self._middlewares` is empty, the function still pays for
coroutine object creation + frame setup + the await. Profile shows
0.015s for 16k calls (~940 ns/call) — that's exactly the empty-body
coroutine overhead.

**Proposed:** at each call site, gate with `if self._middlewares:`.
About 7 call sites in `_dispatch_request`. Each path hits at most one
of these, so the saving is ~940 ns × 16000 = 0.015s = 1.0% of total.

### W1.E — Dedup `MAX_CONTENT_LENGTH` lookup between `_asgi_app` and `handle_request`

**Where:** `_asgi_app` line 2808 + `handle_request` line 1810. Both do
`self.config.get("MAX_CONTENT_LENGTH")` per request. On a workload that
doesn't configure it, the `dict.get` returns None twice.

**Proposed:** lift the lookup to a cached attribute, invalidated when
`config.__setitem__` writes the key (or read once at first dispatch).
A `config_changed` token or version int works too — but the simpler
fix is the **single-flight check in `_asgi_app`** because that's the
ASGI entry; `handle_request` can read from a hop attribute set by
`_asgi_app`. Subtlety: a sync `app.test_client().get(...)` invokes
`handle_request` directly, not through `_asgi_app`. So the cache has
to live on the app, not on the request — `self._max_content_length`
populated by `_load_max_content_length()` called once on first config
mutation.

**Profile evidence:** `dict.get` aggregate is 0.062s / 176k calls;
~16k of those are this lookup × 2 = 32k. Removing 16k of them ≈ 0.006s
= 0.4%.

### W1.F — Reorder `_coerce_response` isinstance chain by frequency

**Where:** `_coerce_response` at line 2421.

```python
if isinstance(result, Response):
    return result
if response_class is not None:
    ...
if isinstance(result, (dict, list)):
    return JSONResponse(result)
if isinstance(result, str):
    ...
if isinstance(result, bytes):
    ...
if hasattr(result, "model_dump"):
    ...
if isinstance(result, tuple):
    ...
return JSONResponse(result)
```

The dict/list case is the most common handler return shape (REST APIs).
But it falls *after* the `response_class is not None` and Response
checks. For routes without a custom response_class:
- A `Response`-returning handler is the fastest path (already first).
- A `dict`-returning handler walks `Response` check → `response_class
  None` check → `dict/list` check. Three operations to hit the
  common path.

**Proposed:** the `Response`-first check stays. The `response_class is
not None` branch is the *less common* case; swap order to put the
`isinstance(result, (dict, list))` branch immediately after the
Response check, ahead of the `response_class` gate. Then the hot path
is two checks instead of three.

**Profile evidence:** `_coerce_response` is 0.035s / 16k = ~2200 ns
per call. Saving one `is not None` check is ~10 ns × 16k = 0.16 ms.
Tiny on the headline but free.

### W1.G — Direct `scope["path"]` / `scope["method"]` instead of `.get()`

**Where:** `_asgi_app` lines 2843, 2847; `_dispatch_request` line 2916.

ASGI spec requires `path` and `method` to be present in HTTP scope.
`scope.get("path", "/")` + `scope.get("query_string", b"")` use `dict.get`
where the key is always present. Subscript is slightly faster than
`.get(default)` because there's no default-arg pop.

**Proposed:** `scope["path"]`, `scope["method"]`, `scope["query_string"]`.
Conservative: keep `.get` for `headers` and `root_path` (those *can*
be omitted in pathological scopes, though uvicorn always provides them).

**Profile evidence:** `dict.get` aggregate is 0.062s / 176k = ~350 ns/call.
Switching to subscript saves ~50 ns per call × ~50k = 0.0025s = 0.2%.
Minor. Mostly a code-style improvement.

### W1.H — `Response.is_streamed` via boolean attribute, not `getattr`

**Where:** `Response.is_streamed` at line 302.

```python
return getattr(self, "_stream", None) is not None
```

`_stream` is only set on `StreamingResponse`. For every base `Response`
the `getattr` does a slot probe → AttributeError → return None. Cheap
but not free (~30 ns).

**Proposed:** add `_stream` to `__slots__` with a `None` default in
`Response.__init__`. `is_streamed` becomes `return self._stream is not
None` — one slot load, no AttributeError.

**Profile evidence:** 0.010s / 16k = ~600 ns/call (most of that is
the property descriptor overhead, not the getattr). Switching to a
direct slot load shaves maybe ~50 ns. Small.

## Out-of-scope (no profile evidence on json-hello / path-param)

These are valid DSA opportunities but the profile shows zero calls on
the measured workload. Per `.claude/rules/perf-changes.md` they do not
get headline credit; documented here for the cold-file sweep.

- **C-X.1**: `_url_value_preprocessors` iteration empty-check (cold).
- **C-X.2**: `_mounted_apps` prefix matching could use a trie when
  N grows; N=0 on hot path.
- **C-X.3**: `_static_handlers` list traversal — same, empty.
- **C-X.4**: `_before_request_hooks`, `_after_request_hooks` — same.
- **C-X.5**: `_bp_before_hooks` / `_bp_after_hooks` dict lookup — same.
- **C-X.6**: WebSocket dispatch path — different profile workload.
- **C-X.7**: `_handle_error` / exception_handlers — only on error paths.
- **C-X.8**: `_apply_response_model` — empty when no response_model.
- **C-X.9**: `_run_instrumentation` — empty when no hooks.
