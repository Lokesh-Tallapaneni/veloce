# Changelog

All notable changes to this project are documented here.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **Comparative bench harness** (`bench/comparative/`): head-to-head
  latency and throughput measurements vs Flask and FastAPI under the
  same uvicorn runtime. Each workload runs all three frameworks in
  randomised order through a single `httpx.AsyncClient`, with a
  discarded cold-cache round to dampen first-run penalties. Reports
  median rps, p50, p99. `--seed` pins the schedule for reproducibility.
  Initial workloads: `json-hello` and `path-param`. Results recorded
  under `docs/bench/`. Veloce wins rps + p50 + p99 vs FastAPI on both
  workloads, and wins rps vs Flask by ~57 % (Flask wins p50/p99 under
  `asgiref.WsgiToAsgi` at low concurrency — see caveats in
  `docs/bench/README.md`).
- **Application core** — `Veloce` app object with HTTP method decorators
  (`get`/`post`/`put`/`patch`/`delete`/`head`/`options`/`trace`),
  lifespan handling, configurable docs URLs, and `app.run()`.
- **Radix-tree router** — typed path converters (`int`, `float`, `str`,
  `uuid`, `path`, custom registered converters), per-route
  `strict_slashes`, subdomain and host constraints, and rule defaults.
- **Request / Response** — lazily-parsed `Request` with multi-value
  query/header/cookie/form accessors, parsed conditional and range
  headers, and a `Response` family (`JSONResponse`, `HTMLResponse`,
  `PlainTextResponse`, `RedirectResponse`, `StreamingResponse`,
  `FileResponse`, `ORJSONResponse`, `UJSONResponse`).
- **Dependency injection** — `Depends` / `Security` / `SecurityScopes`,
  `yield`-style dependencies with teardown, `Annotated[...]` form,
  bare `Depends()` annotation inference, and `app.dependency_overrides`.
- **Parameters** — `Query`, `Path`, `Header`, `Cookie`, `Body`, `Form`,
  `File` markers with constraints (`ge`/`le`/`gt`/`lt`, `min_length`/
  `max_length`, `pattern`, `multiple_of`), list-valued collection, and
  `include_in_schema` / `title` / `examples`.
- **OpenAPI 3.1** — auto-generated schema, Swagger UI and ReDoc, security
  scheme emission, route-level `responses` / `callbacks` /
  `openapi_extra`, and a documentation-only `app.webhooks` router.
- **WebSockets** — ASGI WebSocket routing, raw message API, JSON/text/
  bytes iteration, subprotocol negotiation, dependency injection, and
  `WebSocketException` / `WebSocketRequestValidationError`.
- **Middleware** — CORS, GZip, TrustedHost, HTTPS redirect, sessions,
  rate limiting, request ID, proxy header handling, plus a
  dispatch-style base class.
- **Templating** — Jinja2 integration with async rendering, context
  processors, and registered filters/globals/tests.
- **Sessions** — signed, timestamped cookie sessions with secret
  rotation, a mutation-tracking `Session` container, and persistent
  (`permanent`) sessions.
- **Utilities** — background tasks, signals, Server-Sent Events,
  blueprints with nesting, class-based views, a CLI (`veloce run` /
  `veloce routes` / `veloce shell`), and an in-memory `TestClient`.
- **Security helpers** — HTTP Basic / Bearer / Digest, API key schemes,
  OAuth2 password and authorization-code flows, password hashing, and
  signed-value serialisation.
- `veloce.status` gains `HTTP_208_ALREADY_REPORTED`, `HTTP_226_IM_USED`,
  and `HTTP_421_MISDIRECTED_REQUEST` for full IANA HTTP status coverage.
- `constant_time_compare(a, b)` — a timing-safe secret-comparison helper
  (wrapping `hmac.compare_digest`), exported from the top-level package.
- Veloce's `WebSocket` frame parser reassembles fragmented messages —
  a `FIN=0` data frame followed by continuation frames (RFC 6455 §5.4);
  control frames may be interleaved without disturbing the in-progress
  message.
- `add_middleware` now accepts a standard ASGI middleware class — any
  class that is not a veloce `Middleware` subclass is treated as ASGI
  middleware and wraps the whole application (`cls(app, **options)`),
  so the third-party ASGI ecosystem (tracing, profiling, observability)
  plugs into a veloce app. The first-registered ASGI middleware is the
  outermost wrapper. Native `Middleware` classes are unaffected.
- `AsyncTestClient` (and the `app.async_test_client()` factory) — the
  async counterpart of `TestClient`. Used as `async with` inside an
  async test, its request methods are coroutines awaited on the test's
  own running event loop. Cookie persistence, redirect following, and
  the JSON / form / files body shapes match `TestClient`.
- `app.mount(prefix, app)` now accepts any ASGI application, not only a
  veloce sub-app. A non-veloce app is dispatched at the ASGI layer with
  the matched prefix moved from the scope's `path` onto `root_path`;
  veloce sub-apps keep their existing dispatch path. A mounted ASGI app
  receives `http` and `websocket` scopes — the parent app owns the
  `lifespan` cycle, so a mounted app must self-initialise rather than
  rely on ASGI `lifespan` events. Mount prefixes must not overlap.
- `Config.from_env_file(path)` loads a dotenv-style `.env` file —
  `KEY=VALUE` lines, `#` comments, an optional `export ` prefix, and
  quoted values — into the app config (UPPERCASE keys only).
- `app.run(ssl_context=...)` — the built-in development server now
  accepts an optional `ssl.SSLContext`, handed straight to
  `loop.create_server(ssl=...)`, for local HTTPS testing. Left unset the
  serving path is byte-for-byte the same plain-HTTP path as before.
  Production should still terminate TLS at uvicorn or a reverse proxy.
- `EventLoopWatchdog` — an opt-in development aid that detects a
  coroutine blocking the event loop (a synchronous driver, `time.sleep`,
  a CPU-heavy loop) and logs a warning carrying the blocked stack and a
  prescriptive hint (blocking-I/O vs CPU-bound). A loop heartbeat plus a
  separate daemon thread spot the stall. Enable it with the
  `EVENT_LOOP_WATCHDOG` config key; unset (the default) nothing is
  constructed, so a production app pays nothing.
- `ServerSessionMiddleware` keeps the session payload server-side in a
  pluggable `SessionStore` (default: an in-process `InMemorySessionStore`)
  — the cookie carries only an opaque, high-entropy session id. Sessions
  are now *revocable*: empty one in a handler (`session.clear()`) or
  delete it straight from the store (`await store.delete(session_id)`),
  and a tampered or stale cookie simply fails to resolve. A network
  backend (e.g. Redis) plugs in by implementing the async `SessionStore`
  interface. The existing signed-cookie `SessionMiddleware` is unchanged.
- `app.add_instrumentation(hook)` registers an observability hook called
  once per finished HTTP request with a `RequestMetrics` record — method,
  concrete path, matched route *template* (a low-cardinality metric
  label), status code, and wall-clock duration. Hooks may be sync or
  async; one that raises is logged and never breaks the response. With no
  hook registered the request path pays nothing — not even a clock read.
  The `request_started` / `request_finished` signals now also carry the
  `Request`, so a tracing bridge can correlate a request's start with its
  finish.
- Query / path / header / cookie parameters are now validated through
  Pydantic for any annotation the fast scalar path does not cover —
  `datetime`, `date`, `time`, `UUID`, `Decimal`, `Literal[...]` and other
  rich types are parsed and rejected with a `422` on bad input, the same
  treatment a request-body model already received. The `str` / `int` /
  `float` / `bool` / `Enum` dispatch fast path is untouched. OpenAPI
  parameter schemas now emit the matching `format` / `enum` keywords
  instead of collapsing every non-primitive to a bare string.

### Changed

- A handler that returns a bare `str` now defaults to
  `Content-Type: text/html; charset=utf-8` (previously `text/plain`), so a
  bare-`str` return and `make_response(str)` produce the same media type.
- Multipart form parsing now uses the `python-multipart` streaming
  parser instead of an in-memory `body.split` — it correctly handles a
  boundary token that happens to occur inside binary file data, and a
  malformed body degrades to the parts that parsed cleanly rather than a
  `500`.
- `app.run()` starts the built-in **development** server; it now logs a
  startup reminder that production deployments should run under uvicorn
  (or another ASGI server). See the new Deployment guide.
- `Response.set_cookie` now defaults `samesite` to `"Lax"` — a
  CSRF-resistant default that matches modern browser behaviour. Pass
  `samesite=None` to omit the attribute, or `"None"` (with `secure=True`)
  for a genuinely cross-site cookie.
- WebSocket dependency injection now runs through the same pre-planned
  `HandlerPlan` / `DependencyResolver` as HTTP dispatch. WebSocket
  dependencies gain `yield`-style teardown and `Security` /
  `SecurityScopes` support, and path parameters are coerced to their
  annotated type — previously WebSocket DI used a separate, weaker
  resolver that supported none of these.
- An uploaded file is now backed by a `SpooledTemporaryFile`: each
  multipart part streams into one as it is parsed, staying in memory
  while small and rolling over to a real temp file on disk once it grows
  past 1 MiB. A large upload no longer holds two or three full copies of
  itself in RAM (raw body + per-part `bytearray` + `BytesIO`).
- `request.stream()` now yields the body in bounded 64 KiB chunks instead
  of one chunk covering the whole body, so a handler can process a large
  body incrementally.

### Fixed

- `register_blueprint` no longer drops a blueprint's routes registered with
  `include_in_schema=False`, nor its WebSocket routes — every route is added
  to the radix tree.
- `EventSourceResponse` encodes yielded `ServerSentEvent` objects over the
  ASGI transport instead of raising `TypeError`.
- Dependency type hints are resolved from the right object for class
  dependencies (`__init__`), callable instances (`__call__`), and
  `functools.partial` wrappers, so their parameter types are coerced.
- The in-memory `TestClient` accepts WebSocket connect paths that include a
  query string.
- `WebSocket.send()` — the raw ASGI-message escape hatch — now enforces the
  same handshake state machine as `send_text` / `send_bytes`: sending before
  `accept()` or after `close()` raises instead of proceeding silently.
- Multipart parsing no longer leaks a `SpooledTemporaryFile` when a request
  is rejected by a DoS cap (oversized part or too many parts): the
  in-progress part's spool and every spool already collected from
  completed parts are closed on the reject path.
- Server-side sessions use a conditional store write: a session revoked by
  a concurrent request (logout, `store.delete(...)`) while another request
  is in flight is no longer resurrected when that request writes back —
  `SessionStore` gains a race-safe `replace()` method.
- HTTP requests each get their own `DependencyResolver` instead of sharing
  one: a concurrent request can no longer clear another in-flight request's
  pending `yield`-dependency teardowns (database session close, file-handle
  release), which previously could be silently skipped under load.
- `Config.from_env_file` strips an unquoted inline ` #` comment from a
  `.env` value (`KEY=value  # note` → `value`) while leaving a `#` inside a
  quoted value intact.
- `app.mount()` rejects an overlapping prefix registration (a prefix equal
  to, nested under, or containing an existing mount) with `ValueError`,
  instead of silently shadowing one mount with another.
- `Request.files()` no longer returns duplicate `UploadFile` entries, nor
  runs in O(n²), when several files are uploaded under one form field
  name — it now iterates the form's `(key, value)` pairs once.
- `StaticFiles._etag_cache` is now a bounded LRU (default cap 1 024 entries
  per instance, configurable via the class attribute `ETAG_CACHE_MAX`).
  The previous unbounded dict grew for the lifetime of the worker on a
  large static tree.
- `LoggingMiddleware` no longer keeps a per-instance dict keyed by
  `id(request)`. The start timestamp lives on `request._state`, so it
  cannot leak when a handler raises, and a recycled `id()` cannot
  collide with a stale entry to log a nonsensical duration.
- `jsonable_encoder(obj, include=..., exclude=...)` forwards the filters
  into recursive calls — `exclude={"password"}` now strips the field at
  every depth, matching the dataclass branch.
- `Signal` actually filters by sender. A receiver connected with
  `signal.connect(fn, sender=X)` fires only when `send(X)` runs (matched
  by `is`, falling back to `==`). A receiver connected with the default
  `sender=ANY_SENDER` (sentinel re-exported from `veloce.signals`) still
  fires for every send.
- `UploadFile.read`/`write`/`seek`/`close` now offload the blocking
  filesystem syscalls to a thread once the spool has rolled over to
  disk; the cheap in-memory `BytesIO` path stays on the loop.
- `hash_password_async` / `verify_password_async` — async-safe wrappers
  that run the scrypt KDF on a thread. The sync `hash_password` /
  `verify_password` are unchanged; calling either from an `async def`
  handler blocks the loop for ~100 ms, so async handlers should reach
  for the `_async` variants. Both are exported from the top-level
  package.
- `WebSocket._receive_queue` is now bounded (default `maxsize=64`;
  configurable via the `recv_queue_maxsize` constructor argument). The
  cap turns the previously-unbounded queue into a backpressure signal:
  a peer that sends faster than the handler reads now blocks the
  producer on `put` instead of growing the queue without limit.
- All four wall-clock perf checks in the test suite —
  `TestPerformanceAfterFixes` (`tests/test_async_safety.py`),
  `TestNoSyncIOInHotPath` (`tests/test_async_io.py`), and
  `TestPerformance` (`tests/test_iteration3.py`) — are now marked
  `@pytest.mark.perf` and excluded from the default `pytest` run via
  `addopts = ["-m", "not perf"]` in `pyproject.toml`. The
  relative-to-async budget alone was still flaky under full-suite CPU
  contention; opt in with `pytest -m perf` on a quiet machine. Catastrophic
  dispatch regressions remain gated in CI by
  `bench/dispatch_bench.py --min-rps 2000`.
- Documentation corrected against the code: sync (`def`) handlers are
  documented as supported (run in a thread-pool executor); the built-in
  development server is documented as HTTP/1.1-only (WebSocket and HTTP/2
  workloads run under an external ASGI server); the shipped
  `ServerSessionMiddleware` / `SessionStore` replaces a stale "on the
  roadmap" note; and scoped request hooks are clarified as a `Blueprint`
  feature, not a plain `Router` one.

### Performance

- Per-request reflection eliminated from the hot path: handler
  signatures are inspected once at registration into a frozen
  resolution plan.
- Static route lookup is O(1) per tree level — static child nodes are
  indexed in a dict instead of scanned linearly, so match cost no longer
  grows with the number of sibling routes.
- Request dispatch matches each route once per request instead of twice.
- The dependency resolver no longer re-imports its slot-kind constants on
  every call, and `_call_handler` skips a per-request
  coroutine-function probe by reading the precomputed handler plan.
- `StaticFiles` resolves the served root's real path once at construction
  rather than on every request; the request-scoped `g` store is allocated
  lazily, so handlers that never touch `g` pay no allocation.
- A parameter's `pattern` / `regex` constraint is compiled once at
  declaration time instead of recompiled on every `validate` call.
- `CORSMiddleware` precomputes its origin allow-list as a frozenset and a
  lowercased header allow-set at construction, so per-request CORS checks
  are O(1) instead of scanning a list.
- `Jinja2Templates` `auto_reload` now follows the bound app's `debug`
  flag when left unset — production rendering skips the per-render
  template `stat` syscall. Pass an explicit `auto_reload=` to pin it.
- `response_model=list[Model]` dumps a handler-returned element that is
  already an instance of the target model directly, skipping a
  re-validation round-trip (and correctly preserving per-element
  `exclude_unset`, matching the scalar `response_model` path).
- The ASGI entry point decodes request headers via a list comprehension
  rather than a generator, trimming a per-header generator-frame resume.
- A route whose handler takes no injected parameters and declares no
  dependencies is now dispatched through a trivial-route fast path that
  skips the dependency resolver entirely instead of resolving to `{}`.

### Security

- `MAX_CONTENT_LENGTH` is now enforced incrementally — an oversized
  request body is refused with `413` while still being received, before
  the whole payload is buffered into memory.
- The built-in development server enforces a request-read timeout
  (`HttpProtocol.REQUEST_TIMEOUT`, 30 s) — a half-sent request is dropped
  with `408`, bounding how long a slowloris-style slow client can pin a
  connection open.
- `WebSocketOriginMiddleware` rejects cross-site WebSocket handshakes
  (CSWSH) by checking the handshake `Origin` against an allow-list.
- `SecurityHeadersMiddleware` attaches `X-Content-Type-Options`,
  `X-Frame-Options`, `Referrer-Policy`, and optional HSTS / CSP /
  `Permissions-Policy` response headers.
- `CSRFMiddleware` accepts a `secret` that HMAC-signs the token (with an
  optional `max_age` expiry), so a cookie value carrying no valid server
  signature is refused — raising the bar against cookie-injection CSRF.
  The CSRF cookie now defaults to `Secure`.
- Multipart form parsing caps the part count and per-part size
  (`MAX_FORM_PARTS` / `MAX_FORM_PART_SIZE` config keys), raising `413`
  — a guard against algorithmic-complexity DoS from a maliciously
  structured form.
- `app.use_secure_defaults()` applies a hardened baseline (secure session
  cookies + `SecurityHeadersMiddleware`); `app.security_audit()` and the
  new `veloce check` CLI command report configuration risks before
  deployment.
