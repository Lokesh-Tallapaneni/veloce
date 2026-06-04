# Changelog

All notable changes to this project are documented here.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Mounted Veloce sub-apps now have their lifecycle driven by the parent. A
  Veloce instance mounted with `app.mount(prefix, sub_app)` runs its
  `on_startup` handlers and lifespan context manager when the parent starts,
  and is torn down when the parent shuts down. Children start after the parent
  and are unwound in reverse, and a child whose startup fails unwinds the
  already-started children along with the parent's own resources. Mounted
  non-Veloce ASGI applications continue to own their own lifecycle and are not
  driven by the parent.
- `app.supervise(coro_factory, *, name, max_restarts, restart_window, backoff,
  max_backoff)` runs a long-lived coroutine and restarts it on failure. The
  factory is called again to produce a fresh coroutine on each restart; crashes
  are logged, restarts are spaced by a doubling backoff bounded by
  `max_backoff`, and a count-within-window circuit breaker stops restarting once
  `max_restarts` failures occur within `restart_window` seconds (the counter
  resets after a clean run longer than the window). The supervisor runs as an
  `app.spawn(...)` task, so it is tracked by name and cancelled and drained on
  shutdown; `asyncio.CancelledError` is never suppressed.
- `add_middleware(..., priority=N)` orders the request/response middleware
  pipeline deterministically. Higher priority runs earlier in the request phase
  and correspondingly later in the response phase; middleware of equal priority
  keeps registration order. The ordered chain is resolved once at registration
  time, so per-request dispatch pays no sorting cost, and apps that set no
  priority keep the existing registration-order behaviour unchanged.
- Generated OpenAPI documents now include the `422` validation-error response
  for every operation whose request is validated (any path, query, header, or
  cookie parameter, a JSON request body, or a form field). The response
  references a shared `HTTPValidationError` component schema describing the
  `{"detail": [{"loc", "msg", "type"}, ...]}` body that the dependency resolver
  returns when a parameter fails validation, with a `ValidationError` component
  for each per-error item. Operations with no validatable parameter do not
  advertise a `422`, and an explicitly declared `422` (via `responses=` or
  `openapi_extra`) is preserved rather than overwritten.
- `@app.websocket_listener(path)` declarative WebSocket route. The decorated
  callback handles one message at a time; the framework owns the accept
  handshake, the receive loop, and the clean close on disconnect. The callback
  is invoked as `cb(data)`, or `cb(ws, data)` when its first parameter is named
  `ws`/`socket` (or it declares two positional parameters); returning a
  non-`None` value sends it back, returning `None` sends nothing. `receive` and
  `send` select the codec (`"json"` default, or `"text"` / `"bytes"`).
  `on_connect(ws)` runs after accept and `on_disconnect(ws)` always runs when
  the loop ends, including on peer disconnect. Sync callbacks and hooks are
  offloaded to the executor. The imperative `@app.websocket` decorator is
  unchanged for full handshake/loop control. Available on `Veloce`, `Router`,
  and `Blueprint`.
- `CORSMiddleware` now supports Private Network Access. The new
  `allow_private_network` option (default `False`) echoes
  `Access-Control-Allow-Private-Network: true` on a preflight that carries
  `Access-Control-Request-Private-Network: true`. The grant is opt-in and
  never emitted unless configured.

### Changed

- `SessionMiddleware` and `ServerSessionMiddleware` do less work on the response
  path. The session's `accessed`/`modified` state is read once and reused for the
  `Vary` and persist decisions instead of through repeated probes, and
  `Response.add_vary` uses membership tests for its single-token fast path rather
  than redundant lookups plus a defensive `pop`. Behavior is unchanged, including
  graceful handling of a non-`Session` object placed under the reserved `session`
  state key. On an in-process median-of-nine microbenchmark of the session
  response path, interleaved against the unoptimized baseline to cancel machine
  drift, a read-only session response is about 6% faster and a read-modify-write
  response a few percent faster; the saved work is the redundant `Vary` lookups
  and the repeated session-state reads.

- App-level features (middleware, instrumentation hooks, `@app.middleware("http")`
  functions, mounts, and static handlers) are now tracked through a single
  generation counter and compiled into a per-app pipeline artifact that is rebuilt
  only when a registration changes. The WebSocket handshake host/origin allow-list
  gate now reads its checks from this compiled artifact instead of probing every
  registered middleware on each connect; allow/deny behavior and the `1008` close
  code are unchanged. Registration verbs that previously appended directly
  (`add_http_middleware`, `@app.middleware("http")`, `add_instrumentation`, `mount`,
  `mount_static`) now route through one internal sink that preserves each call
  site's existing setup-lock contract exactly.

- `CORSMiddleware` preflight requests now validate the requested method.
  An `OPTIONS` preflight whose `Access-Control-Request-Method` is not in
  `allow_methods` returns a diagnostic `400` (`Disallowed CORS method`)
  instead of a `204`, matching the existing disallowed-origin behavior.
  Soft `OPTIONS` probes that omit `Access-Control-Request-Method` are
  unaffected.

- `SessionMiddleware` gains an opt-in `chunked` mode for sessions whose signed
  cookie exceeds `max_cookie_size`. With `chunked=True`, the signed value is
  split across numbered cookies (`session.0`, `session.1`, ...) on the response
  and transparently reassembled on the next request; a `max_chunks` keyword
  (default 8) bounds the split so an oversized session is dropped with a warning
  rather than emitting an unbounded number of cookies. Shrinking or deleting a
  session clears its stale chunk cookies. The default remains `chunked=False`,
  preserving the prior drop-with-warning behavior for an oversized cookie.
- Registration-time kwarg-ambiguity check: a handler (or dependency) parameter
  whose name is reserved for an injected object (`request`, or `ws` /
  `websocket` on a WebSocket route) but that also declares an explicit value
  marker (`Query()`, `Path()`, `Header()`, `Cookie()`, `Body()`, `Form()`,
  `File()`) now raises `ConfigurationError` (exported from the top-level
  `veloce` package) at registration, with the dependency chain in the message
  for nested dependencies. Previously the by-name injection silently won and the
  marker was ignored, producing the wrong value at runtime. The check is
  intent-aware: `request: Request`, `q: str = Query()`, and `request=Depends(...)`
  are all left untouched, so valid handlers are never rejected.
- OS-level TCP keepalive on the built-in serving path. `Veloce.run()` and the
  gunicorn worker now set `SO_KEEPALIVE` on each accepted connection so the
  kernel detects and reaps a peer that died without closing the connection,
  which the application idle timer cannot observe. Controlled by `TCP_KEEPALIVE`
  (default enabled) with optional `TCP_KEEPALIVE_IDLE`, `TCP_KEEPALIVE_INTERVAL`
  and `TCP_KEEPALIVE_COUNT` config keys mapping to `TCP_KEEPIDLE` /
  `TCP_KEEPINTVL` / `TCP_KEEPCNT`; the tuning keys are applied only on platforms
  that expose them and otherwise leave the OS defaults in place. ASGI servers
  own their own sockets and are unaffected.
- `StaticFiles(html=True)` now serves a directory's `index.html` and supports a
  custom `404.html`. A slash-less URL that maps to a directory containing
  `index.html` (for example `/docs`) redirects to the trailing-slash form
  (`/docs/`) so the page's relative links resolve against the directory; the
  slash-terminated request then serves the index file. The redirect status is
  `307` by default and is configurable through the new `redirect_status` keyword
  (`307` or `308` only); the query string is preserved across the redirect. When
  a request matches no file, a `404.html` in the served root is returned with
  status `404` if present. Both behaviours are gated on `html=True`, so default
  serving is unchanged. An `index.html` still takes precedence over a generated
  `directory_index` listing.
- Encoder extensibility for `jsonable_encoder`. A per-call `custom_encoder`
  argument accepts a `{type: callable}` mapping that is consulted before every
  built-in rule at every nesting level: the exact `type(obj)` entry is used
  first, otherwise the entries are scanned in insertion order and the first
  `isinstance` match wins. Because it runs first it can override container and
  model handling as well as leaf scalars. A process-level registry is also
  available through `register_encoder(type, fn)` and `unregister_encoder(type)`
  (both exported from the top-level `veloce` package); registered encoders are
  resolved by an MRO walk so subclasses are covered, override built-in handlers
  for the same type, and apply on both the `jsonable_encoder` path and the
  orjson `default=` response path. The `Secret` serialization guard still runs
  ahead of any custom or registered encoder.

### Changed

- An SSE source iterator may now yield bare values. `EventSourceResponse`
  coerces a yielded `Mapping` into a single `data:` field carrying its JSON, and
  any other non-`str`/`bytes`/`ServerSentEvent` value (for example an `int` or
  `float`) into a `data:` field carrying its text. `ServerSentEvent`, `str`, and
  `bytes` keep their existing handling. Previously a yielded `dict`/`int`/`float`
  fell through unencoded and crashed the chunk writer.
- `jsonable_encoder` now dispatches scalar subclasses through their base type's
  encoder. A subclass of `int`, `str`, `float`, `Decimal`, or another known
  scalar (for example `class MyId(int)`) previously fell through to the
  object-vars fallback and serialized as `{}`; it now encodes as its base
  scalar. The resolution walks the class MRO once per concrete type and
  memoizes the result. Serialized output for all previously supported built-in
  types is unchanged.
- `jsonable_encoder` and the orjson `default=` hook now encode `bytes` and
  `bytearray` as lossless base64 strings instead of decoding them as UTF-8 with
  replacement. The previous behaviour substituted U+FFFD for every non-UTF-8
  byte, silently corrupting binary values (image headers, hash digests,
  compressed blobs) into JSON that could not round-trip. The base64 form matches
  the OpenAPI/JSON Schema `format: byte` representation and decodes back to the
  exact original bytes. ASCII/UTF-8 byte values now serialize as their base64
  encoding (for example `b"hi"` becomes `"aGk="`) rather than the decoded text.
- RFC 9110 status-name aliases in `veloce.status`: `HTTP_413_CONTENT_TOO_LARGE`,
  `HTTP_414_URI_TOO_LONG`, `HTTP_416_RANGE_NOT_SATISFIABLE`, and
  `HTTP_422_UNPROCESSABLE_CONTENT` are now defined alongside the existing legacy
  spellings (`HTTP_413_REQUEST_ENTITY_TOO_LARGE`, `HTTP_414_REQUEST_URI_TOO_LONG`,
  `HTTP_416_REQUESTED_RANGE_NOT_SATISFIABLE`, `HTTP_422_UNPROCESSABLE_ENTITY`),
  pointing at the same integer codes. The legacy names are retained for
  back-compatibility.

- Sliding session expiry: `SessionMiddleware` and `ServerSessionMiddleware`
  accept `renew_on_access` (default `False`). When enabled, a session that was
  only read during a request - not modified - has its expiry refreshed on the
  way out: the cookie middleware re-signs the cookie with a fresh `Max-Age`, and
  the server-side middleware refreshes the store entry's TTL (via the new
  `SessionStore.touch`) and re-stamps the cookie. `SessionStore` gains a
  `touch(session_id, max_age)` method to refresh an entry's expiry without
  rewriting its payload; `InMemorySessionStore` refreshes the expiry in place.

- Constrained route syntax: path converters now accept arguments in the brace
  form, so bounds participate in matching instead of only producing a
  handler-layer error. `{n:int(min=1,max=100)}` and `{x:float(min=0.0)}` bound
  the numeric value (`signed=False` forbids a leading `-`), and
  `{code:str(length=2)}` / `{slug:str(minlength=3,maxlength=64)}` bound the
  segment length. A bound violation is a route miss (404), not a 422. The
  constraints are enforced on both the radix fast path and the regex fallback,
  and `url_for` rejects an out-of-bounds value. Zero-argument converters keep
  their previous behaviour.

- Duplicate-route detection: registering a second handler for the same path and
  HTTP method now raises `DuplicateRouteError` (exported from the top-level
  `veloce` package) at registration time, catching silent route shadowing at
  startup. The policy is configurable with the `on_duplicate` argument on
  `Veloce(...)` and `Router(...)`: `"error"` (default), `"warn"` (log and
  replace), or `"override"` (replace silently). Registering the same handler on
  the same path and method again - as happens when a router is included twice -
  is an idempotent re-mount and never reported as a conflict.

- Multipart form parsing gains independent file/field limits. `MAX_FORM_FILES`
  and `MAX_FORM_FIELDS` cap the number of file parts and text-field parts
  separately, `MAX_FORM_FILE_SIZE` and `MAX_FORM_FIELD_SIZE` override the
  shared `MAX_FORM_PART_SIZE` for files and text fields respectively, and
  `MAX_FORM_FIELD_MEMORY` bounds the cumulative resident bytes of all text
  fields (value plus field-name bytes). All default to `None`, so only the
  existing `MAX_FORM_PARTS` / `MAX_FORM_PART_SIZE` caps apply unless set.
  `parse_multipart_form` accepts matching `max_files`, `max_fields`,
  `max_file_size`, `max_field_size`, and `max_field_memory` keyword arguments.
  Each limit raises `413 Request Entity Too Large` when exceeded.

- A multipart text field that declares its own `Content-Type` charset (one of
  `ascii`, `us-ascii`, `utf-8`, or `iso-8859-1`) is now decoded with that
  charset per RFC 7578, instead of always assuming UTF-8. An unsupported
  declared charset is rejected with `400 Bad Request`; the global
  `charset_fallback` still applies when a part declares no charset.
- `ProxyFix` accepts an `x_port` hop count and trusts `X-Forwarded-Port`. The
  resolved public port fills in `request.url` (and therefore `base_url`,
  redirects, and absolute URLs) when the forwarded Host carries no port of its
  own, so a reverse proxy on a non-default port such as 8443 is preserved. A
  port embedded in the Host / `X-Forwarded-Host` always wins, and a
  non-numeric or out-of-range value is dropped rather than trusted. RFC 7239
  carries the port inside `Forwarded host=...:port`, which already flows
  through `x_host`.
- The OpenAPI generator now keys component schemas on the model class identity
  instead of the bare `__name__`. Two distinct models that share a name (for
  example `schemas.User` and `db.User`) no longer overwrite each other: each
  keeps its own component, qualified by the diverging module segment
  (`User__schemas` / `User__db`) when names would otherwise collide, with every
  `$ref` pointing at the correct schema. Nested-model references are rewritten
  from Pydantic's `#/$defs/...` form to `#/components/schemas/...` so they
  resolve in the assembled document.

- Request bodies and response models now use separate JSON Schemas — the
  validation schema for input and the serialization schema for output — so
  computed and read-only fields are documented as clients actually receive
  them. The two are emitted as a single component when byte-identical and split
  into `Name` / `Name-Output` only when they diverge. Set
  `separate_input_output_schemas=False` on `Veloce(...)` to reuse the
  validation schema for both.

- Auto-generated `operationId`s that collide are now disambiguated
  deterministically with a stable path-derived suffix, keeping the emitted
  document valid for client code generation; a single aggregated warning lists
  every collision and its resolution. Explicit `operation_id=` values are left
  untouched, and the behaviour can be disabled with
  `disambiguate_operation_ids=False` on `Veloce(...)`.

- A `validate_openapi` flag on `Veloce(...)` enables a lightweight structural
  check of the assembled document (operations declare responses, parameters
  carry `name` and `in`, every schema `$ref` resolves), raising a precise error
  that names the offending path and method. It defaults to `app.debug`, so the
  check runs in development and costs nothing in production unless explicitly
  enabled.
- `app.spawn(coro, *, name=None)` schedules a long-lived, app-scoped background
  task that is tracked with a strong reference and cancelled-and-drained on
  shutdown within the `GRACEFUL_TASK_TIMEOUT` budget (default 10 seconds). Named
  tasks are retrievable via `app.get_spawned_task(name)` and cancellable via
  `app.cancel_spawned_task(name)`; a duplicate name raises `ValueError`.
  Failures surface through the same logging path as request-scoped background
  tasks. Calling `spawn` without a running event loop raises `RuntimeError`.

- `SetupError` (a `RuntimeError` subclass, importable from the top-level
  `veloce` package) is raised when routes, hooks, blueprints, middleware, or
  error handlers are registered after the application has started serving
  requests. The lock latches on the first dispatch and is relaxed under
  `DEBUG`/`TESTING` and inside the in-memory `TestClient`, so hot-reload and
  test monkeypatching are unaffected.
- Routes can opt out of named middleware with `exclude_middleware=[...]` on
  any route decorator (`@app.get(...)`, `@app.route(...)`, `add_api_route`,
  and the same on `Blueprint`/`Router`). Each entry is matched against a
  middleware's `name` (a new optional `Middleware(name=...)` argument that
  defaults to the class name), and the opt-out applies symmetrically to both
  the request and response phases. A route that declares no exclusions pays no
  extra per-request cost; for a route that does, the filtered chain is computed
  once and reused until the registered middleware set changes. Useful for
  skipping CSRF on webhooks or auth/rate limiting on health and metrics
  endpoints without forking the middleware.
- Parameter markers (`Query`, `Path`, `Body`, `Form`, `File`, `Header`,
  `Cookie`) accept `default_factory`, a zero-argument callable invoked on every
  request the parameter is absent, so each request receives its own value. Use
  it for mutable defaults - `Query(default_factory=list)` - in place of a
  shared `Query(default=[])`, which is constructed once and aliased across all
  requests. `default` and `default_factory` are mutually exclusive. Veloce also
  emits a startup warning when a marker's static default is a `list`, `dict`,
  or `set`, pointing at `default_factory`.
- `FilesKeyError` gives a descriptive message when a missing key is looked up
  on `request.files` while the application runs with `debug=True`. The message
  names the most common cause: a field submitted as a plain form value because
  the form lacked `enctype="multipart/form-data"`, a JSON request body where no
  uploaded files exist, or a request with no multipart body at all. It
  subclasses `KeyError`, so existing handlers that catch the lookup miss keep
  working unchanged, and it is only raised in debug mode - production lookups
  keep the plain `KeyError`. Importable from the top-level `veloce` package.

- `encode_jwt` and `decode_jwt` sign and verify compact JSON Web Tokens using
  the HMAC-SHA2 family (`HS256`/`HS384`/`HS512`) with no external dependency.
  The `algorithms` allow-list passed to `decode_jwt` is required - there is no
  default - and `alg: "none"` is rejected unconditionally; the signature is
  always verified before the payload JSON is decoded. `decode_jwt` validates
  `exp` and `nbf` (with optional `leeway`) and can additionally check
  `audience`, `issuer`, and a `require` list of claim names, returning a
  read-only `Claims` mapping. Each failure raises a distinct subclass of
  `JWTError`: `ExpiredSignatureError`, `ImmatureSignatureError`,
  `InvalidSignatureError`, `InvalidAudienceError`, `InvalidIssuerError`,
  `MissingClaimError`, `UnsupportedAlgorithmError`, and `InvalidTokenError`.
  All are importable from the top-level `veloce` package. RSA/EC algorithms are
  out of scope.

- `make_reset_token` and `check_reset_token` issue storage-free,
  self-invalidating password-reset links. They bind an opaque caller-supplied
  state fingerprint (typically user id plus password hash) into a signed,
  expiring token built on `Signer`; when the fingerprint changes - the password
  is reset or the user logs in - the old token stops validating, so no
  server-side record of issued tokens is needed. `check_reset_token` returns
  `False` for an invalid, expired, or no-longer-bound token and raises
  `BadResetToken` only on programmer misuse, and accepts `fallback_secrets=[...]`
  to keep honouring tokens signed with a rotated previous secret.

- `Secret` wraps a `str`/`bytes` secret so it resists accidental disclosure:
  `repr`, `str`, f-strings, and `%` interpolation all render `***`, and the
  plaintext only escapes through an explicit `.reveal()`. Equality is
  constant-time, the wrapper is unhashable, and the JSON encoders refuse to
  serialize it (raising `TypeError`). Importable from the top-level `veloce`
  package.

- `CSPMiddleware` emits a `Content-Security-Policy` (and/or
  `Content-Security-Policy-Report-Only`) header with an optional fresh
  per-request nonce. `policy` and `report_only_policy` each accept a string
  template containing the literal `{nonce}` placeholder, or a directive mapping
  where the `'nonce'` source is substituted with the generated nonce. Read the
  nonce inside a handler or template with `csp_nonce(request)`; it is
  materialized lazily on first read, so a request that never embeds one pays no
  extra cost. `CSPMiddleware` and `csp_nonce` are importable from the top-level
  `veloce` package.

- `ConditionalGetMiddleware` evaluates `If-None-Match` / `If-Modified-Since`
  against a buffered `GET`/`HEAD` response and downgrades a matching request to
  `304 Not Modified` with an empty body (RFC 9110 Sec. 13). With `auto_etag`
  (the default) it also synthesizes a weak `ETag` for a buffered, non-empty
  `200` that lacks one. Register it after `GZipMiddleware` so a synthesized
  ETag reflects the compressed bytes; `StreamingResponse` bodies are not
  buffered for synthesis. Importable from the top-level `veloce` package.

- `instrument_access_log` registers an instrumentation hook that emits one
  access-log record per finished request - text or JSON (`json=True`) - sourced
  from the same low-cardinality `RequestMetrics` record the tracing bridge uses,
  so logs aggregate on the route template rather than the concrete path. It
  bootstraps a default handler on the `veloce.access` logger, gates on
  `logger.isEnabledFor` so a muted log does zero serialization work, and is
  registered instead of `LoggingMiddleware`. Importable from the top-level
  `veloce` package.

- `veloce.metrics.instrument_with_prometheus` exports a request counter
  (`{prefix}_requests_total`, labelled by method, route template, and status)
  and a request-duration histogram (`{prefix}_request_duration_seconds`) from
  the app's instrumentation hook. The route label is always the matched
  template; an unmatched 404/405 collapses to a constant `"<unmatched>"` label,
  so an attacker-controlled path cannot explode cardinality. The exporter is
  registry-agnostic (pass `registry=...` to isolate apps) and records series
  only - serving `/metrics` stays the application's job. It is an optional
  integration installed with `pip install veloceframework[metrics]`, and
  raises an `ImportError` with an install hint when `prometheus_client` is
  absent.

- Path converters for temporal and decimal types: `{x:date}`, `{x:time}`,
  `{x:datetime}`, `{x:timedelta}`, and `{x:decimal}` coerce the matched segment
  to `datetime.date` / `datetime.time` / `datetime.datetime` /
  `datetime.timedelta` / `decimal.Decimal` respectively. A `Z` suffix on a
  datetime/time is accepted (normalized to `+00:00`); `timedelta` accepts a
  full ISO 8601 duration with at least one component (`P1DT2H`) as well as
  Python's `str(timedelta)` form (`1:00:00`, `1 day, 2:00:00`) so a real
  `timedelta` round-trips through `url_for`, while a bare number is a route
  miss. A value the converter rejects is a 404, consistent with the existing
  converters.

- `StaticFiles(precompressed=True)` serves a precompressed sibling
  (`app.css.br` / `app.css.gz`) when the client advertises a matching
  `Accept-Encoding`, setting the appropriate `Content-Encoding` while keeping
  the original file's `Content-Type`. The variants must be generated ahead of
  time (serve-only; never compresses on the fly), `br` is preferred over `gzip`
  on a quality tie, and ETag/conditional/range handling keys off the bytes
  actually sent. Off by default, as it adds one `stat` per request when
  enabled.

- API-key schemes (`APIKeyHeader`, `APIKeyQuery`, `APIKeyCookie`) now send a
  `WWW-Authenticate` challenge on the `401` raised for a missing credential
  (when `auto_error` is on). The bare `APIKey` token is emitted by default;
  pass `realm="..."` to emit `WWW-Authenticate: APIKey realm="..."`
  (RFC 9110 Sec. 11.6.1).

- `dump_cookie` and `Response.set_cookie` / `Response.delete_cookie` accept
  `prefix="host"` or `prefix="secure"` to add the RFC 6265bis Sec. 4.1.3
  cookie-name prefix (`__Host-` / `__Secure-`) and enforce its invariants:
  both require `secure=True`, and `"host"` also requires `path="/"` and no
  `domain`. `Response.delete_cookie` also gains `partitioned=` so a
  CHIPS-partitioned cookie can be deleted under matching attributes.

- `SessionMiddleware` and `ServerSessionMiddleware` accept `domain=`,
  `cookie_prefix=` (`"host"`/`"secure"`), and `partitioned=` (CHIPS) to scope,
  name-prefix, and partition the session cookie. The middlewares validate the
  prefix and CHIPS preconditions at construction (`partitioned=True` requires
  `secure=True` and `samesite="none"`), raising `ValueError` on a
  misconfiguration.

- `GZipMiddleware` compresses streaming responses chunk-by-chunk through a
  single deflate stream, so a streamed body no longer has to be buffered to be
  compressed. Chunks at or above `min_stream_chunk_offload` bytes (32 KiB by
  default) are offloaded to the thread pool; latency-sensitive types
  (`text/event-stream` by default, via `latency_sensitive_types`) are passed
  through uncompressed so server-sent events are not merged or delayed.

- `ServerSentEvent` gains a `comment` field for SSE comment lines (colon-
  prefixed lines the client ignores). `data` is now optional, so a
  comment-only event can be emitted; a multi-line comment is split into one
  `: ` line per segment. `EventSourceResponse` accepts `ping_comment=...` to
  set the text of the keep-alive heartbeat frame.

- The `TestClient` request methods (`post`, `put`, `patch`, `delete`, and
  `request`) accept `stream=...` to feed the request body as multiple ASGI
  `http.request` chunks instead of a single frame, exercising handlers that
  consume the body incrementally. `stream` accepts a sync `Iterable` or an
  `AsyncIterable` of `bytes`/`str` chunks and takes precedence over
  `json`/`data`/`content`/`files`.

- `Signal.asend` dispatches a signal asynchronously, the async counterpart of
  `send`. Both `asend` and `send_robust_async` now await coroutine-returning
  receivers concurrently rather than one after another, while sync receivers
  still run inline.

- `TemplateResponse`, `render_template`, `stream_template`, and `get_template`
  accept a list of candidate template names and render the first one that
  exists on disk, so a specific template can fall back to a generic one.
  `TemplateResponse` also accepts `media_type=...` to override the response
  `Content-Type` and `background=...` (a callable, `BackgroundTask`, or
  `BackgroundTasks`) to attach a background task.

- `jsonable_encoder` and the orjson default now serialize `re.Pattern`,
  `ipaddress` address/interface/network objects, `collections.deque`, and
  generators. Other final-type scalars encode through a dedicated table
  instead of leaking internals through the `vars(obj)` fallback.

- Model Context Protocol integration under `veloce.contrib.mcp`, exposing a
  Veloce app's handlers as MCP tools callable by an AI agent over JSON-RPC 2.0.
  Register an MCP-only tool with `@app.mcp_tool(description=...)`, or expose an
  existing route by passing `expose_as_mcp_tool=True` and `mcp_description=...`
  on `@app.get` / `@app.post` / etc. Each tool's input JSON Schema is derived
  from the handler signature, reusing the OpenAPI schema generation; `Depends()`
  parameters resolve through the same dependency machinery routes use, with an
  `MCPContext` standing in for the HTTP `Request` (mirroring WebSocket DI),
  including `yield`-style teardown and `Security` support. `app.mount_mcp(
  transport="stdio")` serves the registered tools over stdin/stdout for
  subprocess use, handling the `initialize`, `tools/list`, and `tools/call`
  methods. Mutating verbs (`POST`/`PUT`/`DELETE`/`PATCH`) are never
  auto-exposed - they require the explicit `expose_as_mcp_tool=True` opt-in -
  and every exposed handler must carry a non-empty `mcp_description`, enforced
  at registration. Blueprint-exposed routes are namespaced by the blueprint
  name, and per-tool calls fire the existing `app.add_instrumentation` hooks.
  `MCPContext` is importable from the top-level `veloce` package; the server
  and transport classes live under `veloce.contrib.mcp`. The implementation is
  a from-spec minimal JSON-RPC handshake with no new hard dependency.
  `mount_mcp` serves inside the app's lifespan, so `on_startup` handlers and the
  lifespan context manager run before the first tool is served and the matching
  shutdown runs after. An exposed route runs inside the normal request context
  (`current_app`, `g`, and the `request` proxy are bound); the app's request
  middleware (`process_request`) and `before_request` hooks run first, in the
  HTTP order, and a middleware or hook that returns a response short-circuits
  the call (running `teardown_request`) and becomes the tool result, surfaced as
  an error for a `4xx`/`5xx` status. The synthetic request carries the wrapped
  route's real HTTP method and rule path, so handler/dependency/hook branching
  on `request.method` / `request.path` matches the HTTP path; a client-supplied
  parameter declared inside a `Depends` dependency (including a body model) is
  advertised in the tool's input schema; and the route's rule `defaults=` fill
  any unsupplied handler argument. Per-call instrumentation records the call's
  real status code (the shaped response's status, `500` for an unhandled error,
  `200` only on success). A dependency typed `MCPContext` receives the per-call
  context. A handler that returns `Response(background=...)` has those background
  tasks run, mirroring the HTTP path. A route returning a streaming/SSE response
  is rejected with a clear error result rather than empty output (a v1
  limitation).

- `ServerSentEvent.json` builds an event whose `data` field is a
  JSON-serialized payload. Pass any JSON-encodable value (`dict`, `list`,
  string, number) and it is serialized once at construction with the
  optional `event`/`id`/`retry` fields forwarded unchanged, so structured
  SSE payloads no longer require a manual `json.dumps` per event. The plain
  `ServerSentEvent(data=...)` constructor is unchanged and remains the raw
  string escape hatch.

- `Depends(..., offload=True)` (and the matching `Security(..., offload=True)`)
  routes a blocking sync dependency through the thread pool instead of calling
  it inline on the event loop, so a dependency that does blocking I/O (a DB
  driver call, `requests.get`) cannot stall other in-flight requests. The
  current context is snapshotted before the executor hop, so request-scoped
  state (`request`, `g`, `flash()`) stays readable inside the worker thread.
  This closes an internal inconsistency in which sync route handlers were
  already offloaded but sync dependencies were not. The flag defaults off, so
  trivial pure-function dependencies keep their zero-overhead inline call, and
  it is ignored for coroutine, sync-generator, and async-generator
  dependencies, which already have their own execution model.

- `CSRFMiddleware(trusted_origins=...)` adds an Origin-first verification
  stage that runs before the double-submit check on state-changing
  requests. The request's own origin (`scheme://host[:port]`, sourced
  from the ASGI scope rather than spoofable headers) is always trusted;
  additional callers are listed as full origins, with a leading-dot host
  (`"https://.example.com"`) matching that host and any subdomain. A
  present-but-mismatched `Origin` header is a hard 403; when `Origin` is
  absent the stage falls back to `Referer` on https requests only, while
  plain-HTTP requests with no Origin defer to double-submit. Double-submit
  still always runs as a second factor. This closes the cookie-injection /
  related-domain CSRF class that pure double-submit cannot defend.
  Omitting `trusted_origins` keeps the previous double-submit-only
  behaviour unchanged.

- Write-side backpressure on the built-in serving path. The native
  `HttpProtocol` now implements `pause_writing`/`resume_writing` and arms the
  transport's write-buffer high-water mark in `connection_made`, exposing an
  `await protocol.drain()` that the streaming and SSE response paths await after
  each chunk. A producer outrunning a slow client is throttled at the transport
  buffer instead of growing the event loop's write buffer without bound. The
  high-water mark defaults to 256 KiB and is configurable via the
  `WRITE_BUFFER_HIGH_WATER` config key. `drain()` is a no-op until the buffer
  crosses the mark, so the common keep-alive path is unaffected; the ASGI path
  (where the server owns flow control) is unchanged.
- `async_send_file` top-level helper - the async counterpart of `send_file`.
  It takes the same arguments but reads the file in an executor (via
  `FileResponse.from_path`), so it never blocks the event loop. Prefer it
  over `send_file` from `async def` handlers. Exported from the top-level
  `veloce` package.
- `Veloce.send_static_file_async` - async variant of `send_static_file` that
  serves a file from `app.static_folder` without blocking the event loop.
- `WebSocket` now supports the async-context-manager protocol. `async with ws:`
  closes the connection with a normal-closure 1000 on a clean exit. If the
  block exits via an exception, `__aexit__` defers closing to the dispatcher's
  error handling so the mapped close code (e.g. 1008 policy violation, 1011
  internal error) is sent rather than a normal-closure 1000.
- `WebSocket` idle-receive timeout. `WebSocket(..., idle_timeout=<seconds>)`,
  `WebSocket.from_asgi(..., idle_timeout=<seconds>)`, and the new
  `WebSocket.set_idle_timeout(<seconds>)` setter bound how long a blocking
  receive (`receive`, `receive_text`, `receive_bytes`, `receive_json`, and the
  `iter_*` loops) waits for the next message. Opt-in; the default `None`
  preserves the previous unbounded behaviour. On timeout the connection performs
  a clean RFC 6455 close with `1001 Going Away` and the receive raises
  `WebSocketDisconnect`, so the handler unwinds as on a peer-initiated close. A
  per-call `timeout` still applies, and whichever deadline is smaller wins. The
  window bounds each complete message (under ASGI the server delivers complete
  messages and owns ping/pong; the raw-transport path measures it the same way).
  The value must be a finite positive number of seconds or `None`.
- Inbound WebSocket TEXT frames are now validated as UTF-8 at the raw-transport
  parser boundary (RFC 6455 Sec. 8.1) using an incremental validator that
  catches a bad byte on the first offending fragment of a fragmented message.
  An invalid payload closes the connection with `1007 Invalid Frame Payload
  Data` instead of surfacing a raw `UnicodeDecodeError` at `receive_text()`
  time. Binary frames are unaffected.
- Received WebSocket Close frames are parsed and validated (RFC 6455 Sec. 5.5.1
  / Sec. 7.4): the status code is range-checked (a code below 1000, a reserved
  code such as 1004/1005/1006, or an unassigned code below 3000 closes with
  `1002 Protocol Error`) and the reason is UTF-8-validated (`1007` on failure).
  The peer's close code and reason are exposed on `WebSocket.close_code`
  (`int | None`) and `WebSocket.close_reason` (`str`), populated on both the
  raw-transport and ASGI paths, and the raised `WebSocketDisconnect` now carries
  the peer's close code. An empty Close payload records `1005` ("no status
  received") without putting it on the wire.
- Proactive WebSocket heartbeat for the raw-transport path.
  `WebSocket(..., heartbeat=<seconds>)` and `WebSocket.from_asgi(..., heartbeat=)`
  accept an opt-in liveness interval; after `accept()` a timer sends an
  application PING carrying a monotonically increasing token every interval and
  expects the peer to answer with a PONG (or send any other frame) before the
  next tick. Any inbound byte defers the next probe, so busy connections send no
  needless pings. Two consecutive idle windows with no matching PONG drop a
  silently-dead peer (NAT/load-balancer black-hole) and record `1006` on
  `WebSocket.close_code` without putting the reserved code on the wire. The
  public `WebSocket.start_heartbeat()` arms the timer for hand-built
  connections. Opt-in; the default `None` preserves the previous behaviour, and
  the value is inert in ASGI mode where the server owns ping/pong.
- `Response.check_preconditions(request)` enforces the write-side `If-Match`
  precondition (RFC 9110 Sec. 13.1.1), raising `PreconditionFailed` (412) when
  the request's `If-Match` does not match the response's ETag under the strong
  comparison (Sec. 8.8.3.1) - the lost-update guard. `If-Match: *` is satisfied
  when a current representation exists (an ETag is present); with no `If-Match`
  header the response is returned unchanged. Opt-in and separate from
  `make_conditional`, so existing read-side 304 flows are unaffected.
- `StaticFiles` now honours the write-side preconditions `If-Match` and
  `If-Unmodified-Since`, returning `412 Precondition Failed` per RFC 9110
  Sec. 13.1.1 / 13.1.4 (precedence per Sec. 13.2.2: `If-Match` first). Because
  Veloce serves weak file ETags, a concrete `If-Match` against a static file
  fails closed (only `*` succeeds); clients needing optimistic concurrency on
  static assets should use `If-Unmodified-Since`. The read-side `If-None-Match`
  / `If-Modified-Since` 304 behaviour is unchanged.
- `SessionMiddleware` and `ServerSessionMiddleware` accept `vary_on_cookie`
  (default `True`) and `persist_on_status` constructor keywords. The former
  controls the new `Vary: Cookie` emission (below); the latter is a
  `(status_code) -> bool` policy that overrides the default 5xx no-persist
  rule (below).
- `HTTPSRedirectMiddleware` accepts `exempt_paths` and `exempt_acme_challenge`
  constructor keywords. `/.well-known/acme-challenge/` is exempt by default
  (RFC 8555 Sec. 8.3: the HTTP-01 challenge must be reachable over plain HTTP
  for certificate issuance/renewal); pass `exempt_acme_challenge=False` to drop
  that default, or `exempt_paths=("/health/", ...)` to exempt other prefixes.
- `StaticFiles` and `Veloce.mount_static` accept a `must_exist` keyword
  (default `True`) that validates the served directory exists and is readable
  at construction, raising `ValueError` instead of silently 404-ing every
  asset. Pass `must_exist=False` to downgrade the check to a warning for the
  dev flow that creates the directory after wiring the app.
- `UploadFile.headers` is now populated from the part's MIME headers (a
  case-insensitive `Headers` view) instead of always being empty, so a handler
  can read e.g. `upload.headers["Content-Transfer-Encoding"]`.
- `JSON_ERRORS_VERBOSE` config key (default `False`): surfaces the verbose JSON
  decoder reason in the 400 response body; falls back to `DEBUG` when unset.

- `Veloce.add_instrumentation` accepts an optional `exclude_routes` set of
  matched route *templates* (e.g. `{"/health", "/metrics"}`); a request whose
  route template is in the set skips that hook. The filter is applied in the
  core delivery loop on the low-cardinality template, so every consumer -
  tracing, metrics, access logs, custom - honours the same exclusion with no
  per-request regex and no path-normalisation bypass. `instrument_with_otel`
  and `instrument_with_prometheus` thread the same `exclude_routes` parameter
  through. With no exclusions configured the dispatch path is unchanged.

- `RequestMetrics` gains an `error_type` field carrying the class name of the
  exception that produced a `5xx` when an *unhandled* raised exception turned
  into a server error (the debug traceback, the generic `500`, or a propagated
  exception); it is `None` for every other outcome, including a `5xx`
  deliberately returned without raising. Only the class name is carried - never
  the message or the exception instance.

- `instrument_with_otel` accepts an optional `on_span(span, metrics)` callback
  to enrich each emitted span with custom attributes or events; it runs inside
  the bridge's `try`/`finally` and a raised callback is suppressed so it cannot
  break the response. When a `5xx` came from a raised exception the bridge now
  records `RequestMetrics.error_type` as the OpenTelemetry `error.type` span
  attribute.

- `instrument_with_otel(app, live=True)` adds an opt-in *live* tracing mode. In
  addition to the default backdated server span (recorded after the request
  finishes), live mode installs an ASGI-layer wrapper that opens a real
  `SpanKind.SERVER` span at request start and attaches it to the OpenTelemetry
  context (`set_span_in_context` + `context.attach`) for the duration of the
  handler, so spans the handler creates - and outbound-call spans - are children
  of the server span and the trace tree is correct. The context token is
  detached and the span ended in a `finally`, so the token is always balanced
  and never leaked even when the handler raises, and the per-request token model
  is concurrency-safe (each request attaches and detaches its own token). The
  span name and attributes are filled in by the bridge's enrichment hook, which
  runs inside dispatch while the live span is current, so it reuses the same
  route-template naming, cardinality guards, `error.type` attribution, `on_span`
  enrichment and `exclude_routes` filtering as the backdated mode. Unlike the
  backdated mode, live mode times a streaming response end to end (the span ends
  after the body drains). The implementation uses only the guarded OpenTelemetry
  API plus context attach/detach - no `opentelemetry.instrumentation` contrib
  dependency. The default backdated mode is unchanged and stays zero-overhead.

- Password hashing gains rehash-on-login primitives. `needs_rehash(stored)`
  reports whether a stored verifier was produced with a configuration weaker
  than the current defaults - a non-default method (a PBKDF2 hash migrating to
  scrypt) or scrypt cost parameters below the current `n`/`r`/`p`.
  `verify_and_needs_update(stored, candidate)` returns `(ok, needs_update)` so
  an application can verify a password and, on success, transparently re-derive
  it at the current work factor while the plaintext is still in hand;
  `needs_update` is always `False` on a failed verify.
  `verify_and_needs_update_async` offloads the verify to a thread. The existing
  `verify_password` signature is unchanged. All three are exported from the
  top-level `veloce` package.

### Changed

- Yield-dependency teardowns no longer swallow their own failures.
  `run_teardowns` still runs every teardown in reverse registration order even
  when one raises, but the failures are now collected and re-raised together as
  a `BaseExceptionGroup` (PEP 654) - chained from the request exception when the
  request itself failed - instead of being logged and discarded. A broken
  teardown (a failed transaction commit or rollback, say) is therefore
  observable. Each failure is still logged as it happens, and the HTTP and
  WebSocket dispatch paths log the aggregated group rather than letting it break
  the response cycle. A clean teardown chain allocates nothing and raises
  nothing. On Python 3.10 (no exception groups) the first failure is re-raised,
  chained, after the rest are logged.

- A `multipart/form-data` request with a missing `boundary` parameter, or a
  boundary that violates the RFC 2046 grammar (empty, longer than 70
  characters, illegal characters, or a trailing space), now raises `400 Bad
  Request` instead of silently parsing to an empty form.
- Application startup now drives the lifespan context manager and the dev
  event-loop watchdog through a single `AsyncExitStack`, so a startup handler
  that raises unwinds exactly the resources already acquired (in reverse) rather
  than leaving a partially-started app with an un-exited lifespan CM or a running
  watchdog. Shutdown runs every `on_shutdown` handler even when one raises and
  re-raises all teardown failures together as a `BaseExceptionGroup`
  (Python 3.11+; a single failure is re-raised as-is, with older versions
  attaching the rest as notes), instead of stopping at the first failure.

- `app.got_first_request` now flips to `True` on the first dispatch regardless of
  whether any `before_first_request` hooks are registered, so it faithfully
  reports whether a request has been handled. Previously the flag only flipped
  when such hooks existed.

- `Veloce.run()` and the gunicorn worker now perform a two-phase graceful
  shutdown: every live connection is first quiesced - it finishes the request it
  is already dispatching and then closes at the request boundary rather than
  being cancelled mid-pipeline - before the existing bulk task wait/cancel runs
  as a hard-timeout fallback. Accepted requests are no longer abruptly cut off
  during drain.

- The ASGI lifespan shutdown branch now reports teardown failures to the server
  via the spec's `lifespan.shutdown.failed` message (with a full traceback)
  instead of letting the exception escape `__call__`, mirroring the existing
  `lifespan.startup.failed` handling.
- A user-registered exception handler that itself raises no longer escapes
  request dispatch uncaught. The secondary failure is logged with the handler's
  name and the request path, and a standard 500 response is returned, so a buggy
  error handler degrades gracefully in production. When `PROPAGATE_EXCEPTIONS`
  is in effect (or implicitly under `DEBUG` + `TESTING`), the handler exception
  is re-raised as before so the bug remains visible in tests and development.
- Independent dependencies now resolve concurrently regardless of their
  position in the handler signature. The resolver batches every parallel-safe
  `Depends()` into topological waves computed once at registration, so two
  independent dependencies separated by an ordinary parameter - for example
  `a = Depends(...)`, `q: int = Query(...)`, `b = Depends(...)` - run together
  instead of one after the other. Dependencies sharing a cached callable are
  placed in successive waves so the cache is filled once and reused, never
  raced; `Security()`-scope dependencies and `yield` dependencies continue to
  resolve inline in declaration order, preserving scope and teardown semantics.

- The unknown-object fallback in `jsonable_encoder` and the orjson default now
  drops private (underscore-prefixed) attributes from the structurally-derived
  `vars(obj)` namespace, so an object encoded by reflection no longer leaks
  library/ORM bookkeeping such as `_sa_instance_state`. A class may opt back in
  to including private attributes by setting `__json_include_private__ = True`.

- A request carrying multiple `Cookie` headers (permitted by RFC 6265) now has
  them merged with `; ` before parsing, so every cookie is read. The
  single-header fast path is unchanged.

- `ProxyFix` splits `Forwarded` / `X-Forwarded-*` element and pair lists on
  delimiters outside quoted strings, so a quoted comma or semicolon in a
  directive value (e.g. `host="a,b"`) no longer fakes an extra hop.

- `Accept` content negotiation now honours media-type parameters (RFC 9110
  Sec. 12.5.1). A parameterized media range such as `application/json;profile=x`
  only matches a value carrying that parameter, a bare range still matches a
  parameterized value, and `best_match` ranks candidates by `(q-value,
  specificity)` so a parameterized or fully-specified match beats a wildcard at
  equal quality and a more specific `q=0` range overrides a broader accept.
  Each option's type/subtype/parameters are decomposed once at parse time. The
  `q` parameter separates the q-value from media-type parameters; accept
  extensions after `q` are ignored. Non-MIME `Accept-*` headers are unchanged.

- `Response.content_disposition` (and `send_file`/`FileResponse` filenames)
  now emit an ASCII quotable name verbatim as `filename="..."` - spaces and
  punctuation preserved, only `\` and `"` escaped per RFC 9110 Sec. 5.6.4 - and
  emit a non-ASCII or non-quotable name only as the RFC 5987
  `filename*=UTF-8''...` form, with no lossy legacy `filename=` slot. A CR/LF in
  the name is rejected.

- The OpenAPI schema and Swagger UI page now document a header parameter under
  its hyphenated wire name when the parameter marker converts underscores
  (e.g. a `x_request_id` header is documented as `x-request-id`), matching the
  name the resolver reads. The Swagger UI config object is HTML-safe escaped
  before being embedded inline in the page's `<script>` block.

- `url_for` / `url_path_for` now validate each substituted path parameter
  through the converter declared on the route before building the URL. A value
  the radix matcher would never accept - `url_for('item', id='abc')` on
  `/items/{id:int}` - raises at call time (`ValueError` from the router, wrapped
  in `BuildError` by `Veloce.url_for`) instead of returning a dead
  `/items/abc`. Validation reuses the route's existing converter `match()` in
  O(1) per parameter, derived from the route template on first reverse and
  cached, so reverse and forward stay symmetric with no regex re-execution.
  Parameters with no typed converter (bare `{name}` or a raw-regex segment such
  as `{id:[0-9]+}`) keep accepting any stringifiable value.
- `WebSocket.receive_text()` / `receive_bytes()` skip the `asyncio.wait_for`
  wrapper when neither a per-call `timeout` nor a connection `idle_timeout` is
  set (the common case), awaiting the ASGI receive directly. Measured on uvloop,
  this cuts Veloce's own per-message receive+send overhead from ~1.65us to
  ~0.70us per echo; the timeout/idle path is unchanged.
- `DefaultJSONProvider` reads the `JSON_SORT_KEYS` and
  `JSONIFY_PRETTYPRINT_REGULAR` config flags once when the provider is first
  instantiated (on first `app.json` access) and caches the resulting orjson
  option bitmask, instead of re-reading `app.config` on every `dumps()` call.
  Set these flags before the first `app.json` access; mutating them afterwards
  no longer affects the already-instantiated provider. Per-call `sort_keys` /
  `indent` overrides are unaffected.
- `jsonify()` now serialises through the active app's JSON provider
  (`app.json.response()`) when called inside a request, instead of re-reading
  `app.config` on every call. This makes `jsonify()` and `app.json.dumps()`
  share one source of truth, so the two JSON paths cannot diverge when the
  config flags are mutated at runtime. Outside a request context `jsonify()`
  still falls back to the plain `JSONResponse` defaults.
- `RequestIDMiddleware` now generates a fresh request ID when the incoming
  request-ID header is missing **or empty**. Previously a present-but-empty
  header value (e.g. `X-Request-ID:`) was propagated verbatim as the empty
  string; it is now replaced with a generated UUID.
- Request-lifecycle signals (`request_started`, `request_finished`,
  `got_request_exception`, `request_tearing_down`) are now dispatched
  unconditionally instead of being guarded by `has_receivers_for()`. `Signal.send()`
  short-circuits when there are no subscriptions, so the guard only duplicated the
  subscriber scan; removing it means a single live-scan that both fires receivers
  and prunes dead weakrefs.
- `GzipMiddleware` parses `Accept-Encoding` with a fast path for the common
  parameterless token list (e.g. `gzip, deflate, br`), falling back to the full
  q-value-aware parse only when the header contains a `;`. Behaviour is unchanged.
- `SessionMiddleware` measures the rendered `Set-Cookie` size using the string
  length directly for all-ASCII cookies (the common case) and only encodes to
  `latin-1` for non-ASCII content. The size enforced and the `UnicodeEncodeError`
  for code points above U+00FF are unchanged.
- Bearer-token extraction precomputes the lowercased scheme prefix for the
  default `Bearer` scheme, removing a per-request string construction on
  HTTP Bearer and OAuth2/OpenID authenticated paths. A custom scheme name is
  unaffected.
- The session middlewares now emit `Vary: Cookie` (RFC 9110 Sec. 12.5.5) on
  any response whose handler accessed the session (read via `request.session`
  or mutated it), so a shared proxy/CDN keyed on URL alone cannot serve one
  user's session-personalized body to another. A session-independent response
  - one whose handler never touched `request.session` - stays cacheable even
  for a logged-in client. Pass `vary_on_cookie=False` to opt out. `Session`
  gains an `accessed` flag, set by `request.session`, to drive this.
- The session middlewares no longer persist a modified session on a 5xx
  response by default - a failed request should not write a half-mutated
  session (neither the signed Set-Cookie nor the server-store write/delete).
  Pass `persist_on_status=<callable>` to override (e.g. `lambda s: s != 503`).
  Persistence on 2xx/3xx/4xx is unchanged.
- `app.debug` is now a property bound to `config["DEBUG"]`, so the attribute and
  the config key are a single source of truth - setting either (including after
  construction) is reflected by every debug-gated code path.
- Malformed-JSON request bodies now produce a stable `400 Invalid JSON body`
  instead of leaking the verbose orjson decoder reason (byte offsets derived
  from attacker-controlled input). The verbose reason is logged and available
  on `BadRequest.debug_detail`, and is returned in the body only under `DEBUG`
  or `JSON_ERRORS_VERBOSE`. The synchronous `get_json()` path now raises
  `BadRequest` (400) like the async `json()` path, rather than the raw decoder
  error (which surfaced as a 500).
- Responses with a bodiless status (1xx, 204, 205, 304) no longer advertise the
  framework-default `Content-Type` over an empty body, and the body is stripped
  on both the ASGI and native (`Response.encode`) emit paths (the native path
  previously emitted the full body for these statuses). `Content-Length: 0` and
  any handler-set `Content-Type` are preserved.

### Fixed

- Scope-aware dependency caching. A `Security()` dependency whose sub-graph
  reads `SecurityScopes` is now cached per active scope set, so the same auth
  callable referenced with different scopes in one request
  (`Security(auth, scopes=["read"])` and `Security(auth, scopes=["read",
  "write"])`) resolves twice with the correct scopes instead of returning the
  first cached result against the wrong scope set. Plain `Depends` and any
  `Security()` dependency that does not read its scopes keep the identity-only
  cache key, so they still resolve once per request.

- `add_middleware(MiddlewareClass, name="...")` now applies the exclusion-name
  override after construction instead of forwarding `name` into the subclass
  constructor. A user `Middleware` subclass whose `__init__` does not accept a
  `name` keyword previously raised `TypeError` when registered with `name=`;
  it can now be named and targeted by `exclude_middleware=[...]` like any
  built-in. The override is set on the built instance, so passing `name=` to a
  built-in (which still accepts the keyword) yields the same final name.

- `Veloce.add_instrumentation` works as a decorator with arguments:
  `@app.add_instrumentation(exclude_routes={"/health"})`. `hook` is now
  optional; when omitted the call returns a decorator that registers the
  wrapped function with the captured `exclude_routes`. The plain
  `add_instrumentation(hook, ...)` call and the no-parenthesis
  `@app.add_instrumentation` decorator are unchanged.

- `Veloce.add_instrumentation` now enforces the setup lock: registering an
  instrumentation hook after the app has started serving raises `SetupError`
  (relaxed under DEBUG/TESTING and the in-memory `TestClient`), consistent with
  route and hook registration. The per-request `_instrumentation` list is
  iterated by concurrent dispatch, so late registration could otherwise race
  in-flight requests. `instrument_with_otel` / `instrument_with_prometheus`,
  which register during setup, are unaffected.

- Per-route middleware exclusion (`exclude_middleware`) is now symmetric across
  the request and response phases. The exclusion set is keyed on the route
  matched at dispatch entry - the same route the request phase uses - so the
  exact set of middleware that ran `process_request` is the set that runs
  `process_response`. A `before_request` hook that rewrites the path to a route
  with a different `exclude_middleware` no longer changes which middleware run
  for that request, preventing an unbalanced chain where a middleware's
  per-request setup ran without its teardown.

- OpenAPI dual-schema generation now compares the full schema, including every
  nested `$defs` entry, before folding a serialization variant onto its
  validation twin. Previously only the top-level root was compared, so a parent
  model with an identical root but a nested model carrying serialization-only
  fields (a `computed_field`, a read-only alias) lost its distinct `-Output`
  response schema even with `separate_input_output_schemas=True`.

- A duplicate-route replace under the `warn` or `override` policy now removes the
  replaced route's reverse (`url_for`) entry when the winning route uses a
  different `name=`. Previously `url_for(old_name)` kept resolving to a route no
  longer present in the dispatch table.

- `instrument_with_otel` is now idempotent. Calling it more than once on the
  same app (a re-imported factory, a test fixture, a per-worker bootstrap)
  previously registered a second span-emit hook, so every request produced two
  `SpanKind.SERVER` spans - over-counted traces and doubled export cost. A
  redundant call now emits a `RuntimeWarning` and returns the already-registered
  hook instead of appending a duplicate. The dedup state lives on the app's hook
  list, so two apps in one process each get their own bridge.

- `Signal.asend()` now waits for every async receiver to finish before it
  returns or raises. When several async receivers were dispatched and one raised
  early, the non-robust gather re-raised the first failure immediately while the
  other already-scheduled tasks kept running in the background, so `await
  asend(...)` could return before dispatch actually completed and a later
  receiver could touch request-scoped state after teardown. All async receivers
  now run to completion and the first exception, in receiver order, is re-raised
  afterwards. `send_robust_async`'s per-receiver result contract is unchanged.

- `StaticFiles(precompressed=True)` now returns `406 Not Acceptable` instead of
  the uncompressed asset when no acceptable compressed sibling exists and the
  client rejected the identity coding (for example `Accept-Encoding: identity;q=0,
  br;q=0, gzip;q=0`). Per RFC 9110 Sec. 12.5.3 identity is acceptable by default
  unless excluded by `identity;q=0`, or by `*;q=0` without a more specific
  identity entry re-enabling it. Requests that leave identity acceptable, and
  handlers with `precompressed=False`, are unaffected.

- `CSPMiddleware` now suppresses its default policy when a route already set a
  `Content-Security-Policy` (or `-Report-Only`) header under any letter case.
  Header field names are case-insensitive (RFC 9110 Sec. 5.1) but `Response`
  headers are a plain dict, so a lowercase route override previously failed the
  existence check and a second CSP header shipped; browsers intersect multiple
  CSP headers, silently narrowing the route's intended policy.

- `instrument_with_prometheus(group_status=True)` (the default) now collapses
  the status code into its class bucket - `200` -> `"2xx"`, `404` -> `"4xx"`,
  `503` -> `"5xx"` - as the `status` label, instead of always recording the
  concrete code. The option was previously a no-op. Pass `group_status=False`
  to keep the concrete code.

- `decode_jwt` now rejects an empty `str`/`bytes` secret with `ValueError`,
  symmetric with `encode_jwt`. An empty HMAC key would otherwise verify tokens
  signed with the empty secret (for example when a secret environment variable
  is unset). The check runs before any token parsing and raises a loud
  `ValueError` - a configuration error - rather than a `JWTError` that an auth
  dependency would translate into a `401`.

- `ConditionalGetMiddleware` no longer downgrades a `StreamingResponse` to
  `304 Not Modified` on a satisfied `If-None-Match` / `If-Modified-Since`. The
  downgrade cleared the buffered body but not the stream, so the 304 would still
  emit the original chunks, which is invalid per RFC 9110. Streamed responses
  now pass through unchanged.

- `StaticFiles` precompressed selection now honours an explicit `q=0` coding
  over an `Accept-Encoding` wildcard (RFC 9110 Sec. 12.5.3). Previously
  `Accept-Encoding: br;q=0, *;q=1` served the `.br` sibling even though Brotli
  was explicitly rejected; an explicit `q=0` now excludes the coding, with the
  wildcard q-value used only for codings not explicitly listed. A new
  `AcceptHeader.quality_explicit` method exposes this explicit-over-wildcard
  q-value resolution. Coding tokens are compared case-insensitively (RFC 9110
  Sec. 8.4.1), so `Accept-Encoding: BR` matches an explicit `br` entry and
  `Br;q=0` rejects it; `AcceptHeader.quality` matches media types and codings
  case-insensitively for the same reason.

- A `WebSocket.send_text` / `send_bytes` to a peer that has gone away under an
  ASGI server now raises `WebSocketDisconnect` instead of a raw transport
  `OSError` / `ConnectionError` (broken pipe, connection reset), so handlers
  catch the same disconnect exception on every transport.

- `jsonable_encoder(..., exclude_none=True)` now drops `None`-valued keys from
  plain dicts, lists/tuples/sets of dicts, and dict-typed fields nested inside a
  Pydantic model, at every depth. Previously `exclude_none` was honoured only
  for a top-level `BaseModel`'s own fields and silently ignored for any plain
  mapping, so `jsonable_encoder({"a": None, "b": 1}, exclude_none=True)` returned
  `{"a": None, "b": 1}` instead of `{"b": 1}`. The flag is now threaded through
  the dict, sequence, set, dataclass, and model re-encode branches consistently.
- JSON response serialisation now handles `set`/`frozenset`, `pathlib.Path`,
  `decimal.Decimal`, `bytes`, and arbitrary objects instead of raising
  `TypeError`. `DefaultJSONProvider.dumps`, `JSONResponse`, and `jsonify`
  pass a single-object fallback as orjson's `default=` hook, so the common
  path keeps orjson's native C-speed encoding while unsupported leaves are
  converted (sets become sorted lists, `Path`/`Decimal` map to their scalar
  form, `bytes` decode UTF-8 with replacement, other objects use `vars()`
  then `str()`). The hook is also exported as `veloce.encoders.orjson_default`.
- The WebSocket handshake now uses the correct RFC 6455 magic GUID
  (`258EAFA5-E914-47DA-95CA-C5AB0DC85B11`) when computing
  `Sec-WebSocket-Accept`. The previous value produced an accept token no
  conformant client accepts. This only affected raw-transport handshakes
  (under an ASGI server such as uvicorn the server performs the handshake), so
  it was latent until exercised directly.
- `HttpProtocol.connection_made` now checks the transport by capability
  (`write` + `pause_reading`) instead of `isinstance(asyncio.Transport)`.
  uvloop's transport implements the full-duplex interface but is not an
  `asyncio.Transport` subclass, so the previous check rejected every connection
  under uvloop - meaning `Veloce.run()` was broken on Linux whenever uvloop was
  installed. Half-duplex transports are still rejected.
- The raw HTTP/1.1 server's early request-size guard now uses the **first**
  `Content-Length` value when a malformed request carries duplicate
  `Content-Length` headers, matching the previous header-scan behaviour.
- A request-lifecycle signal whose only remaining receiver was a dead weakref
  (its bound-method owner garbage-collected) is no longer stranded in the
  subscriber list. Because the previous `has_receivers_for()` guard never pruned,
  such entries accumulated; dispatching `send()` directly now drops them.
- `HTTPBasic` / `HTTPDigest` now escape the `realm` in the `WWW-Authenticate`
  challenge as an RFC 7235 / RFC 7230 Sec. 3.2.6 quoted-string (backslash-escaping
  `"` and `\`) instead of percent-encoding it with `urllib.parse.quote`. A realm
  such as `testrealm@example.com` is emitted literally rather than as
  `testrealm%40example.com`. A realm containing CR/LF/NUL or other control
  characters now raises `ValueError` at construction.
- Non-latin-1 response header values are now RFC 2047 MIME-encoded (an ASCII
  `=?utf-8?b?...?=` token) on both the HTTP/1.1 (`Response.encode`) and ASGI
  emit paths, instead of raising `UnicodeEncodeError` on the HTTP/1.1 path and
  emitting raw UTF-8 (mojibake once re-decoded as latin-1) on the ASGI path.
  ASCII and latin-1-representable values are emitted verbatim.
- `GZipMiddleware` weakens a strong `ETag` to `W/...` after compressing the
  body, since compression changes the bytes on the wire and a strong validator
  (RFC 9110 Sec. 8.8.1) must denote byte-identical representations. Already-weak,
  absent, or malformed (non-quoted) tags are left untouched.
- WebSocket frames with a non-zero RSV bit (RFC 6455 Sec. 5.2 - Veloce
  negotiates no extension) or a stray continuation frame with no message in
  progress (Sec. 5.4) are now rejected with a `1002` protocol-error close,
  instead of being silently accepted / dropped.
- A raw-transport WebSocket `receive_*()` called after the peer closed between
  messages now raises `WebSocketDisconnect` carrying the recorded close code
  (e.g. `1001`/`1006`) instead of a default `1000`.
- ETag generation (`Response.add_etag`, file/StaticFiles ETags) passes
  `usedforsecurity=False` to `hashlib.md5`, so it no longer raises on FIPS
  Python builds. The ETag bytes are unchanged.
- Integer-valued `Decimal` values now serialize as JSON integers rather than
  floats (`Decimal('1')` is `1`, not `1.0`), preserving exact digits for large
  whole numbers; values outside orjson's 64-bit integer window fall back to a
  string instead of losing precision.
- `ServerSentEvent` rejects a CR/LF in the `event` field and a CR/LF/NUL in the
  `id` field at construction (a NUL in `id` silently breaks Last-Event-ID
  reconnection per the WHATWG SSE spec), instead of silently stripping them.
- The router rejects a path that binds one parameter name twice
  (`/{id}/x/{id}`) with a clear `ValueError` at registration, replacing a silent
  capture clobber on the radix path and an opaque `re` error on the regex path.

### Security

- HTTP Basic authentication rejects an RFC 7617-malformed credential that lacks
  the `username:password` colon (previously accepted as an empty-password
  login).
- `dump_cookie` rejects a cookie name that is not a valid RFC 6265 token (e.g.
  containing a space or `;`) or that collides with a cookie-attribute keyword
  (`Path`, `Max-Age`, ...), preventing malformed or attribute-injecting
  `Set-Cookie` headers.

- `safe_join` now rejects any path segment that names a Windows reserved device
  (`CON`, `PRN`, `AUX`, `NUL`, `COM1`-`COM9`, `LPT1`-`LPT9`, `CONIN$`,
  `CONOUT$`, including extension/trailing-dot/space aliases such as `COM1.txt`
  and `COM1.`) when running on Windows, so a request like `static/COM1` can no
  longer reach `os.stat` and hang a worker on a device handle. The check is
  gated on `os.name == "nt"`, so POSIX deployments are unaffected and pay no
  per-request cost. `secure_filename` gains the `CONIN$`/`CONOUT$` aliases for
  consistency.

- `URL.from_request` validates the `Host` header against the RFC 3986 Sec. 3.2.2
  host grammar (reg-name unreserved/sub-delims, bracketed or bare IPv6 literal)
  and the port against the 1-65535 range before deriving `host`, `netloc`,
  `base_url`, and `url_root`. A malformed `Host` (e.g. `evil.com/path?x`, an
  `@`-userinfo form, an embedded CR/LF, or a non-numeric port) now falls back to
  the safe default host instead of flowing into absolute-URL construction, so a
  Host-injection payload can no longer poison the URLs the framework builds.

## [0.3.0] - 2026-06-01

### Fixed

- `StaticFiles` `If-Range` evaluation now follows RFC 9110 Sec. 13.1.5: an
  entity-tag validator must match under the **strong** comparison function and
  an HTTP-date validator must match **exactly** (not the "earlier-than-or-equal"
  test). Previously a weak ETag or an older-than-mtime date could still
  authorize a `206` range resume; those validators now correctly fall through
  to a full `200`. Veloce emits weak file ETags, so a safe range resume is via
  the exact `Last-Modified` date.

## [0.2.0] - 2026-05-31

Request bodies now stream. The raw HTTP/1.1 server dispatches a handler as
soon as the request line and headers are parsed and feeds the body in as it
arrives over a bounded, backpressured queue, instead of buffering the whole
body in memory before dispatch. Peak per-connection body memory is now the
queue bound rather than the full upload, so many concurrent large uploads no
longer scale memory with body size.

### Added

- **CLI plugin commands via entry points.** The `veloce` command now discovers
  third-party subcommands advertised under the `veloce.commands` entry-point
  group. A distribution exposes one with, in its `pyproject.toml`:

  ```toml
  [project.entry-points."veloce.commands"]
  deploy = "mypkg.cli:register"
  ```

  where the target is a callable handed the argparse subparsers action to add
  one subcommand. Discovery is lazy: a plugin is imported and executed only
  when its command is the one selected on the command line, so `veloce`,
  `veloce --version`, and `veloce --help` never import or run plugin code.
  Plugins are isolated from the core: a plugin that fails to import, does not
  resolve to a callable, raises while registering, leaves no runnable command,
  or whose name collides with a built-in is reported with a warning and
  skipped. A plugin that partially registers a parser before failing is rolled
  back, so the built-in commands always remain usable.
- **OpenTelemetry tracing bridge (`veloce.otel.instrument_with_otel`).** A new
  optional integration emits one `SpanKind.SERVER` span per finished
  non-streamed request, driven off the existing `app.add_instrumentation` hook.
  The span is named for the matched route template; when no route matched (a
  `404` for an unknown path or a `405` for a disallowed method) it falls back to
  a low-cardinality method-based name (`"HTTP GET"`) **only for a recognised HTTP
  method** — an arbitrary/attacker-controlled verb collapses to the constant
  `"HTTP other"` so the span name can never explode cardinality. The concrete
  request path is never used as a span name. Each span carries
  `http.request.method` (the real method, per OTel semconv), `http.route` (only
  when a route matched), `http.response.status_code`, and a `duration_ms`
  attribute; a `5xx` status marks the span error. Streamed response *bodies*
  (`StreamingResponse`, `EventSourceResponse`, a chunked `FileResponse`) are not
  traced: the body is emitted on the ASGI send path after the instrumentation
  hook fires, so the available timing/status would predate stream completion and
  miss a mid-stream failure; such records are skipped. A `HEAD` request never
  iterates its body, so it is traced normally even on a streaming route. Only the
  OpenTelemetry API is required — the application supplies its own SDK,
  `TracerProvider`, and exporter. Install with `pip install veloceframework[otel]`;
  `import veloce` continues to work without the extra. The span is recorded
  retroactively from the request's metrics record: its `end_time` is the
  wall-clock instant captured the moment dispatch returned (before any other
  instrumentation hook or `request_finished` receiver runs, so a slow earlier
  hook cannot shift it) and its `start_time` is that end minus the measured
  duration, so the exported span covers the real request window. It continues an
  inbound **W3C distributed trace**: the inbound `traceparent` / `tracestate`
  headers are carried on the metrics record and the bridge extracts a parent
  context from them, so a request arriving with an upstream trace joins it (same
  `trace_id`, parented under the caller's span); absent those headers the span is
  a clean root. Extraction happens in the span-emit path, which runs on every
  dispatch outcome — including a request short-circuited by an earlier
  `before_request` hook — so trace continuation never depends on hook ordering. It is never parented
  under the ambient OpenTelemetry context active when the hook fires. This is a
  *server-span* bridge — it continues inbound traces and emits one span per
  request, but does not inject context into outbound calls or wrap handler
  execution for child spans, and (as above) does not trace streamed response
  bodies.
- `RequestMetrics` now carries a `streamed` flag (set when the response body is a
  streaming iterator), an `end_time_ns` field (the wall-clock end captured
  before any hook runs), and an opaque `parent_context` (the inbound
  `traceparent` / `tracestate` headers, carried so a tracing bridge can
  continue a distributed trace; the core never interprets it). Instrumentation
  hooks that need accurate end-of-request timing can skip streamed records and
  anchor timing to `end_time_ns`.
- The built-in HTTP/1.1 server's keep-alive and slowloris read timeouts are
  now configurable through `app.config`: `KEEP_ALIVE_TIMEOUT` (idle-connection
  timeout) and `REQUEST_TIMEOUT` (per-request read budget). Defaults are
  unchanged at 75 and 30 seconds respectively.
- The built-in HTTP/1.1 server now honours `Expect: 100-continue`: a request
  carrying that header is answered with an interim `100 Continue` once its
  headers are parsed, clearing the client to send the body. The interim is
  suppressed for HTTP/1.0 clients and when the declared `Content-Length`
  already exceeds `MAX_CONTENT_LENGTH` (the request is rejected with `413`
  instead).
- **Hybrid routing: radix fast path with a regex fallback.** Routes the radix
  tree cannot express now match through a compiled-regex fallback consulted only
  on a tree miss, so patterns such as a parameter sharing a segment with static
  text (`/v{version:int}/api`), multiple parameters in one segment
  (`/files/{name}.{ext}`), a raw regex converter (`/items/{id:[0-9]+}`), or a
  greedy `:path` converter followed by a suffix (`/{p:path}/edit`) are now
  supported. Classification happens once at registration; the radix fast path is
  unchanged and pays nothing when no regex route is registered — the tree always
  wins over the fallback. Regex routes participate in `url_for`, allowed-method
  reporting (405/OPTIONS), `include_router` merging (with name prefixing), and
  OpenAPI schema generation (exposed with an OpenAPI-style path, e.g.
  `/items/{id}`).
- **Optional gunicorn worker (`veloce.workers.VeloceWorker`).** An advanced,
  POSIX-only alternative to running under uvicorn: gunicorn manages the process
  pool while each worker drives Veloce's own `HttpProtocol` directly on an
  asyncio event loop, with no uvicorn or ASGI shim in the request path. gunicorn
  is an optional dependency installed via the new `gunicorn` extra
  (`pip install veloceframework[gunicorn]`); importing Veloce never requires it,
  and the worker raises a clear `ImportError` with an install hint if
  instantiated without gunicorn present. Run with
  `gunicorn your_module:app -k veloce.workers.VeloceWorker`. uvicorn remains the
  recommended production default. See the Deployment guide.
- **SSE keep-alive heartbeat.** `EventSourceResponse(..., ping=<seconds>)` emits a
  comment frame (`: ping`) whenever no event is produced within the interval, so
  idle connections survive proxy and load-balancer read timeouts. The heartbeat
  applies to both the ASGI streaming path and the raw-socket transport; the
  in-flight pull is preserved across each idle window rather than being
  cancelled. `ping` must be a finite positive number of seconds — zero,
  negative, `NaN`, and `Infinity` are rejected with `ValueError` at
  construction. Without `ping` the behaviour is unchanged.
- **Template streaming.** `Jinja2Templates.stream(name, context)` returns Jinja's
  chunk iterator instead of a fully-rendered string, and the module-level
  `stream_template(template_name, **context)` mirrors `render_template` against
  the current app's `Jinja2Templates`. Wrap the result in `StreamingResponse` to
  return a large body without buffering it in memory. `stream_template` is
  exported from the top-level package.
- **`.env` auto-load in the CLI.** `veloce run`, `veloce shell`, and
  `veloce custom` accept `--env-file PATH` (default: auto-discover `.env` in the
  current directory) and `--no-env-file`. Matching `KEY=VALUE` pairs are written
  to the environment before the app module is imported, so import-time config
  sees them. A real environment variable always wins; an explicit `--env-file`
  that is missing is an error, while an absent auto-discovered `.env` is silently
  skipped. An auto-discovered `.env` that exists but cannot be read (permission
  denied, a directory in its place, and similar) is reported as an error rather
  than skipped. For `veloce custom`, `--env-file` / `--no-env-file` are parsed
  on either side of the app reference — including the space-separated
  `--env-file PATH` form before the app — and before the `--` separator that
  forwards the remaining arguments to the app's Click group.
- **`Namespace` signal factory.** `veloce.signals.Namespace` returns named
  `Signal` instances, caching one per name so independent parts of an application
  can share a signal by agreeing on its name.

### Docs

- Expanded the documentation guide with pages for configuration, error
  handling, blueprints, parameters, templates, static files, sessions,
  Flask-style helpers, security schemes, passwords, signing, file uploads,
  class-based views, background tasks, server-sent events, and signals. The
  built-in middleware table now lists every shipped middleware class.

### Changed

- The raw HTTP/1.1 response-head construction shared by `Response.encode()`,
  `StreamingResponse.encode()`, and `EventSourceResponse.stream_to()` now flows
  through a single internal helper, `veloce._internal._encode_response_head`. The
  helper builds the status line, applies framework default headers only when the
  caller has not supplied a same-named header (case-insensitive), and performs
  CR/LF/NUL validation on both default and caller-supplied header values plus the
  `Set-Cookie` split. Subclasses that override these encode paths and previously
  copied the header-building loop should route through the helper to inherit the
  case-insensitive default suppression.

- In debug mode, an unhandled exception now renders a styled, read-only HTML
  traceback page **for clients that prefer HTML** (a browser, via the `Accept`
  header); curl / CLI / programmatic clients (`*/*`, no `Accept`, or an explicit
  `text/plain` preference) keep the plain-text traceback unchanged. The page shows the
  exception type and message, each frame's file path, line number and function
  name, and a short source-context window read from `linecache`; every
  interpolated value is HTML-escaped. Chained exceptions (`raise ... from`,
  implicit context), per-exception `__notes__`, PEP 654 exception groups
  (`ExceptionGroup` / `BaseExceptionGroup`, including those raised by
  `asyncio.TaskGroup`), and the offending source line and caret for
  `SyntaxError`/`IndentationError`/`TabError` are all preserved, matching the
  detail of the plain-text traceback that non-HTML clients still receive; an
  exception whose `__str__` raises is rendered with a safe placeholder rather
  than crashing the renderer. The page is a read-only traceback viewer — there
  is no evaluating console, no frame-local inspection over the wire, and no
  code-eval endpoint — and remains gated behind `debug=True` only. The
  production error response is unchanged.
- Parameter-only handlers (the request plus scalar path/query parameters, with
  no dependencies) now resolve through a straight-line resolver generated and
  compiled once on first dispatch and cached on the route's plan, replacing the
  per-request slot-dispatch loop. Measured ~10–12% faster dispatch on param-heavy
  routes without dependency injection; routes using `Depends`, body models, list
  params, or websocket/background/response injection are unchanged — they use the
  existing resolver.
- The compiled resolver now also handles synchronous parameter markers —
  `Query()`, `Path()`, `Header()`, and `Cookie()` (including their list-typed
  forms) — inlining the source lookup and coercion the per-request interpreter
  performed, plus the `validate()` constraint check for scalar markers.
  Behaviour matches the interpreter exactly, including that list-typed markers
  collect their values without running per-item `validate()`. Measured ~16–22%
  faster dispatch on routes whose parameters are these markers. `Body()`,
  `Form()`, and `File()` markers continue to use the interpreter, since their
  source is read asynchronously and cannot be reached from the compiled
  function.
- **BREAKING — request body access is now asynchronous.** To stream a body the
  framework can no longer hand the handler a fully-formed bytes attribute, so
  the body accessors are now awaitables:
  - `request.body` (attribute) → `await request.body()` (method).
  - `request.text` (property) → `await request.text()`.
  - `request.get_data(...)` is now `await request.get_data(...)`.
  - `request.json()` and `request.form()` were already `async` — no change for
    callers that already `await` them.
  - `request.data` remains a **synchronous** property for the buffered case,
    but raises `RuntimeError` if accessed while the body is still streaming and
    has not been drained yet; use `await request.body()` on the async path.
  - `request.get_json()` remains synchronous and works once the body is
    buffered; it raises a clear error if the body has not been read yet.

  Migration: add `await` to `request.body`, `request.text`, and
  `request.get_data(...)` call sites, and make the enclosing handler/util
  `async`. Code that already used `await request.json()` /
  `await request.form()` needs no change.

- **`async for chunk in request.stream()` is now true streaming on the raw
  HTTP/1.1 server.** Chunks are yielded as they arrive off the socket rather
  than sliced from an already-buffered body, so a handler that only iterates
  `stream()` (e.g. writing an upload straight to disk) processes an arbitrarily
  large body with bounded memory. The in-memory ASGI and `TestClient` paths
  pre-fill the body, so `stream()` there yields the complete body as before.

- **`MAX_CONTENT_LENGTH` is enforced against the streamed running total** (and
  the declared `Content-Length`) and still returns `413` — now rejecting an
  over-large upload mid-stream instead of only after the whole body is
  buffered.

### Fixed

- `StaticFiles` now consults `If-Range` before honoring a `Range` request
  (RFC 9110 Sec. 13.1.5). A `Range` accompanied by a stale validator — an
  `If-Range` ETag that no longer matches, or an HTTP-date older than the file's
  mtime — is served as a full `200` instead of a `206` slice, so a client
  resuming a download with an outdated validator can no longer splice bytes
  from a newer file version.
- `GZipMiddleware` no longer compresses partial-content responses. A `206`
  response, or any response carrying a `Content-Range`, is passed through
  uncompressed; previously the body was gzipped while `Content-Range`,
  `Accept-Ranges`, and `ETag` continued to describe the uncompressed
  representation, producing a protocol-invalid response.
- `async def` template context processors now contribute on the async render
  path. `Jinja2Templates.render_async` awaits coroutine-returning context
  processors instead of discarding them, so values they provide appear during
  async rendering (the sync render path is unchanged).
- `StreamingResponse` and `EventSourceResponse` no longer emit a duplicate
  `Content-Type` (or `Connection`) header on the raw HTTP/1.1 transport when the
  caller supplies that header name in non-title case. The default headers were
  merged with a case-sensitive `dict` update, so a `headers={"content-type":
  "text/csv"}` override left the framework default in place alongside the
  caller's value. The default is now suppressed by a case-insensitive name
  comparison, matching the base `Response.encode()` behaviour. The common case
  (no override, or a title-case override) is unchanged.
- The buffered ASGI response path no longer emits a duplicate `Content-Length`
  (or `Content-Type`) header. It now checks the response headers
  case-insensitively and sends the framework default only when the response
  does not already carry that header, so a value set by middleware — such as
  the compressed length written by `GZipMiddleware` — appears exactly once and
  wins. This matches the raw HTTP/1.1 `Response.encode()` path. Responses with
  no explicit `Content-Length`/`Content-Type` are unchanged, and
  `Content-Encoding`, `Vary`, and per-cookie `Set-Cookie` headers still pass
  through. Strict HTTP clients that reject duplicate `Content-Length` now
  accept gzip-compressed responses.
- OpenAPI schemas for non-body parameters (query, path, header, cookie) and
  `Form()` fields now match what the request resolver can actually deliver.
  These values arrive over the wire as raw strings, so a parameter annotated
  with a **bare** Pydantic model documents `{"type": "string"}` rather than a
  `$ref`, and the resolver parses that string as a JSON document into the
  model (`?tag={"name":"x"}` for `tag: Tag = Query()`), so a matching value
  resolves rather than returning a 422. A model nested inside a
  `list`/`dict`/`set` or a union is **not** JSON-decoded by the resolver, so
  the schema does not advertise the model's fields there: a `list`/`set` of a
  model documents `items: {"type": "string"}`, and a model member of a union is
  dropped. A `dict[K, V]` parameter documents a bare `{"type": "object"}`
  regardless of the value type — a non-body dict is not wire-addressable (it
  422s on a JSON-object string and has no repeated-param form), so typed
  `additionalProperties` would advertise a shape the resolver rejects. A union
  whose members are all models likewise documents a bare object. A
  multi-member union is documented by the branch a string value can actually
  reach under Pydantic's smart coercion: a union that includes `str` or
  `bytes` (`int | str`, `Optional[int | str]`) documents `{"type": "string"}`,
  since string input always lands on that member; a union of non-string,
  non-model members (`int | float`, `UUID | int`, `date | datetime`) documents
  an `anyOf` over them, since the resolver genuinely resolves the string to
  one of those typed branches. Pydantic models carried as a structured JSON
  body belong in `requestBody`, where they are still resolved to a `$ref`.
- **Hybrid router: unknown converters in regex-routed paths now raise at
  registration.** A bare-word converter typo in a path that forces regex
  routing — `/v{version:bogus}/api`, or in a later segment such as
  `/v{version:int}/{id:bogus}` — previously slipped through as literal regex and
  matched the text `bogus`. Every bare-word spec across all segments of a regex
  route is now validated against the converter set and raises `unknown path
  converter` at registration, the same as whole-segment placeholders.
- **Hybrid router: registered custom converters in regex-routed segments now
  raise at registration.** A custom converter — `register_converter("slug", …)`
  — used in a segment that forces regex routing (`/v{name:slug}/api`) previously
  miscompiled into a regex matching the literal text `slug`, so `/vslug/api`
  matched while `/vabc/api` did not. A custom converter's `match()` has no regex
  representation, so such routes now raise at registration instead of silently
  misbehaving. Custom converters spanning a whole segment (`/posts/{name:slug}`)
  remain radix routes and are unaffected.
- **Hybrid router: regex-route parameters are now coerced like radix-route
  parameters.** A built-in converter on a regex route (`/v{n:int}/x`,
  `:float`, `:uuid`, `:any(...)`) now yields the coerced Python value (e.g.
  `3`, not `"3"`) instead of the raw matched string. Bare `{name}` and raw
  regex (`{id:[0-9]+}`) groups remain strings.
- **Hybrid router: regex-route converters now enforce the same rejection
  semantics as radix routes.** A built-in converter's guards — notably the
  `:int` digit cap — are now applied to regex-route matches: when the converter
  rejects its group, the route is treated as a full miss (404) and the next
  route is tried, instead of leaking the raw string through to the handler.
  Previously `/v{n:int}/x` matched an over-long value (e.g. a 21-digit number)
  and passed it as `str`, while the equivalent radix route `/x/{n:int}`
  correctly rejected it. Allowed-method reporting honors the same rejection, so
  an over-long `:int` yields a 404 rather than a 405.
- **Hybrid router: raw regex converter specs may now contain braces.** Patterns
  with brace quantifiers — `/x/{id:[0-9]{2}}` or `/x/{id:\d{2}}` — are parsed
  balance-aware and compile correctly instead of raising a group-name error.
  OpenAPI path reduction and `url_for` handle these specs too.
- **Hybrid router: `strict_slashes=False` is honored on regex routes.** A regex
  route registered with `strict_slashes=False` now accepts the missing or extra
  trailing slash in both `match()` and allowed-method reporting, matching tree
  routes.
- **OpenAPI: a tree route shadows an overlapping regex handler in the schema.**
  When a radix route and a regex route reduce to the same OpenAPI path and
  method, the schema now describes the tree handler — the dispatch winner —
  rather than the regex handler that never runs for that path.
- **gunicorn worker (`veloce.workers.VeloceWorker`) now honours TLS.** When
  gunicorn is started with `--certfile`/`--keyfile` (`cfg.is_ssl`), the worker
  builds a server SSL context from gunicorn's config and passes it to
  `create_server`, instead of handing the bound sockets to asyncio with no TLS
  and silently serving cleartext. If the certificate chain is missing or cannot
  be loaded the worker fails fast with a `RuntimeError` rather than downgrading
  an HTTPS deployment to cleartext. The default context is routed through
  gunicorn's documented `ssl_context(config, default_ssl_context_factory)` hook,
  so a configured TLS customization (minimum TLS version, mTLS tweaks, ciphers)
  is honoured instead of ignored.
- **gunicorn worker stops when the master dies.** The heartbeat loop now also
  checks arbiter liveness (the worker's parent pid changing after a fork-reparent)
  and shuts the worker down instead of leaving it orphaned if the gunicorn master
  goes away.
- **gunicorn worker honours `--max-requests`.** The worker now counts completed
  requests and clears `alive` once the count reaches gunicorn's `max_requests`
  (with any `max_requests_jitter` already folded in by gunicorn), so worker
  recycling works as documented; previously the counter was never incremented and
  recycling never fired. Counting is driven by a new optional
  `HttpProtocol.on_request_complete` hook that is unset (and free) on the
  uvicorn / `Veloce.run()` path. The per-connection serve loop now also consults
  an optional `HttpProtocol.should_keep_serving` predicate at each request
  boundary, so once `max_requests` clears `alive` a connection with queued or
  pipelined requests stops at the boundary and closes instead of draining the
  rest of its queue past the limit before the worker restarts.
- **gunicorn worker no longer leaks a listener on a partial multi-bind failure.**
  When gunicorn hands the worker more than one bound socket, the worker creates
  one asyncio server per socket. If a later bind failed, an already-created
  listener stayed live while the worker proceeded into shutdown. The worker now
  closes every listener created so far before re-raising, so a failed startup
  leaves no live listener behind.

### Internal

- Source-wide style and constant-centralization pass (behavior-preserving).
  Modules follow a canonical layout, loggers use `getLogger(__name__)`, and
  duplicated MIME types, HTTP header names/values, status codes, and
  ASGI/WebSocket protocol tokens are now defined once in `veloce._constants` /
  `veloce._protocol_constants` / `veloce.status` and imported, reducing
  case/typo drift. Docstring em-dashes were normalized to ASCII hyphens. No
  runtime behavior changed.
- The raw protocol serves pipelined requests through a per-connection FIFO loop
  (responses preserved in request order), drains-and-discards any body a handler
  leaves unread so keep-alive connections cannot be corrupted by leftover bytes,
  and pauses socket reads when the body queue fills, resuming as the handler
  drains it.

### Security

- `StreamingResponse(content_type=...)` with a CR/LF or NUL in the value is
  rejected on the raw HTTP/1.1 encode again. `content_type` is a public
  constructor argument that flows into the default response headers; the shared
  `_encode_response_head` helper now CR/LF/NUL-validates default header values,
  closing a response-splitting / header-injection path (e.g.
  `content_type="text/csv\r\nEvil: 1"`) that briefly emitted the injected line
  when the encode paths were consolidated.
- `application/x-www-form-urlencoded` bodies are now capped at the same
  `MAX_FORM_PARTS` field limit (default `1000`) already enforced for multipart
  forms. A body exceeding the cap raises `413 Request Entity Too Large` instead
  of being parsed in full, closing a memory/CPU exhaustion vector for bodies
  whose total size is within `MAX_CONTENT_LENGTH`. Forms under the cap parse
  unchanged; set `MAX_FORM_PARTS = None` to disable the limit.

## [0.1.4] - 2026-05-25

Post-v0.1.3 audit batch: verified findings from a per-file framework
audit (validated by a second-pass agent with cross-file grep proofs;
false positives discarded). Four security fixes, four correctness
fixes, six performance wins, eight duplication consolidations, and
API surface cleanup.

### Security

- **Multipart UTF-8 decode is now strict.** Header fields and form
  values whose bytes are not valid UTF-8 raise `BadRequest` (400)
  instead of silently passing through `errors="replace"` and producing
  collidable field-name strings. The fuzz suite's
  `test_fuzz_multipart_corrupted_valid_body_never_crash` allowlist
  expanded to permit `BadRequest` alongside the existing
  `RequestEntityTooLarge` DoS-cap rejection — both are controlled
  rejections, neither is a crash.
- **`HTTPBasic` realm now URL-encoded in `WWW-Authenticate`.** Matches
  the existing `HTTPDigest` behaviour. A realm containing `"`, `\r`,
  `\n`, or `\` no longer produces a malformed challenge header.
- **`HTTPBasic` catches only the exceptions `b64decode`/`decode` can
  raise** — `binascii.Error`, `ValueError`, `UnicodeDecodeError` —
  instead of bare `except Exception` that would have masked unrelated
  bugs as 401s.
- **`SecurityHeadersMiddleware` `hsts_include_subdomains` default is
  now `False`.** Casually enabling HSTS on a multi-subdomain host
  should not silently pin every subdomain. Explicit opt-in still
  works (`hsts_include_subdomains=True`).

### Changed

- `jsonable_encoder` no longer recurses infinitely on a self-referential
  object graph; it raises `ValueError("Circular reference detected...")`
  instead. Internal `_seen: set[int]` parameter tracks visited container
  IDs; leaf-only call graphs allocate no extra state.
- `jsonable_encoder` leaf scalars now dispatch through a `type ->
  encoder` dict (`str`, `int`, `float`, `bool`, `None`, `UUID`,
  `Decimal`, `datetime`/`date`/`time`/`timedelta`) instead of a 14-deep
  `isinstance` cascade. Subclasses fall through to the existing
  isinstance arms so behaviour is unchanged.
- `_dispatch_request` merges injected-response Set-Cookie headers via a
  new private `Response._append_set_cookie_header(raw_value)` helper
  instead of the inline `+ "\r\nSet-Cookie: " +` concatenation. The
  same helper is now what `Response.set_cookie()` calls internally,
  giving the codebase one canonical home for the Q44 multi-cookie
  join format.
- `Response.add_vary` delegates dedup to `HeaderSet` instead of
  re-implementing the case-insensitive ordered-set logic inline.
- `signing.py` module docstring now correctly describes the nested
  HMAC algorithm (`HMAC-SHA256(HMAC-SHA256(secret, salt), payload.ts)`)
  the code has always implemented, rather than the previous misleading
  `HMAC-SHA256(secret + salt, payload.ts)` text.
- `Router.url_for` regex compiled once at module top
  (`_URL_FOR_PARAM_RE`) instead of re-compiled on every call.
- Conditional request properties — `access_control_request_headers`,
  `if_modified_since`, `if_unmodified_since`, `if_match`, `if_none_match`,
  `if_range` — now cache their parsed value on `Request` slots so the
  five header re-parses per repeated access become one.
- `contrib/openapi.py` schema generation memoizes per-handler
  `inspect.signature` + `get_type_hints` via a
  `WeakKeyDictionary` (`_handler_intro`), so the four call sites that
  used to each re-introspect every route now share a single per-handler
  read.
- `contrib/templating.py::_sync_app_jinja_helpers` memoizes per
  `(env, app, filter/global/test counts)`; rendering a template no
  longer redoes the three loops + globals copy on every render.
- `middleware/compression.py` wraps the gzip executor offload in
  `contextvars.copy_context().run(...)` so any future ContextVar read
  inside the compressor sees request-scoped values. Matches the pattern
  already in `_call_handler` and the per-request executor sites in
  `app.py`.
- `middleware/security.py::TrustedHostMiddleware` now delegates its
  Host-header port stripping to the shared `_extract_host` helper in
  `_internal.py` instead of the inline IPv6-aware branches that
  duplicated it.
- `middleware/sessions.py::ServerSessionMiddleware._clear_session_cookie`
  centralises the three identical `response.delete_cookie(...)` calls
  for the revocation paths.
- `security/api_key.py` extracts `_APIKeyBase`; `APIKeyHeader`,
  `APIKeyQuery`, `APIKeyCookie` now share `__init__` + `__call__` and
  differ only in a `_source_attr` class var. Public surface unchanged.
- `cli.py` introduces `_require_app_attr(app, attr, hint)`; the four
  `if not hasattr(app, ...)` guards in `_cmd_shell` / `_cmd_custom` /
  `_cmd_routes` / `_cmd_check` now share one error-message shape.
- `testclient.py` extracts module-level `_build_request_headers` and
  `_apply_set_cookie_to_jar`; `TestClient` and `AsyncTestClient` both
  call them instead of carrying two byte-identical copies of the
  header-merge and cookie-jar update logic.
- `contrib/staticfiles.py` directory-index and file paths now share a
  `_is_under_root(real_path)` method that uses `os.path.commonpath`,
  replacing the `startswith(root + sep)` vs `commonpath` inconsistency.
- `middleware/logging.py` falls through to the parent logger hierarchy
  via `hasHandlers()` so adding `LoggingMiddleware` on top of a
  `logging.basicConfig`-configured app no longer attaches a duplicate
  handler. `setLevel(INFO)` is now unconditional so the start-time
  capture in `process_request` is always armed.
- `serving/protocol.py` logger name unified to
  `veloce.serving.protocol` (was a mix of `veloce.protocol` and
  `veloce.serving`).
- Six raw `inspect.iscoroutinefunction` call sites (`app.py`,
  `background.py`, `dependency.py`, `views.py`, two in
  `_handler_plan.py`) now route through the memoized
  `_internal._is_async_callable` wrapper.
- `routing/__init__.py` re-exports `Converter` and `register_converter`
  (already top-level-exported via `veloce/__init__.py`); the asymmetry
  is gone.
- `http/__init__.py` re-exports `Cookies`, `QueryParams`,
  `parse_multipart_form`, and `HeaderSet`. `HeaderSet` is the return
  type of `Response.vary` / `Response.allow`, so users receiving one
  from a property can now construct one through the documented public
  path.

### Removed

- Dead constants `K_DEFAULT` and `K_NONE` from `_handler_plan.py` —
  declared but never instantiated.

### Docs

- `Blueprint.register_blueprint` docstring clarifies that nested-route
  endpoint names are stored as `<child.name>.<handler>` on the parent
  and only pick up the `<parent.name>.` prefix at app registration
  (the eventual `<parent>.<child>.<handler>` shape was already
  produced; the previous wording implied it was applied immediately).

## [0.1.3] - 2026-05-23

Security + correctness batch. Fifteen issues from the second external
review of the published `veloceframework==0.1.2` wheel — eleven
correctness bugs, three security fixes, and a polish bundle.

### Security

- **CSRF token now rotates on demand** (#90). `CSRFMiddleware` no longer
  reuses an anonymous session's CSRF cookie after the user authenticates
  — that was a session-fixation pathway. Call the new
  `from veloce.middleware import rotate_csrf_token; rotate_csrf_token(request)`
  helper at the end of any login / logout / permission-elevation
  handler; `process_response` then mints a fresh signed token cookie.
- **`verify_password` refuses tampered scrypt cost parameters** (#85).
  A tampered stored hash carrying `N=2` would previously verify in
  microseconds, defeating scrypt's work floor. The verifier now
  rejects any stored hash whose `N` is below `2**14`, `r` is below 1,
  or `p` is below 1. The floor is hardcoded against a documented
  v0.1.3 minimum (not the live `_SCRYPT_N` default) so a future
  tune-up of the default does not retroactively invalidate hashes
  generated by earlier Veloce releases.
- **Swagger UI and ReDoc loaded with Subresource Integrity hashes** (#83).
  `/docs` and `/redoc` now pin specific patch versions
  (`swagger-ui@5.18.2`, `redoc@2.1.5`) and load with `integrity=` +
  `crossorigin="anonymous"`. A CDN compromise can no longer inject
  arbitrary JavaScript onto either page. Swagger UI moved from
  `unpkg.com` to `cdnjs.cloudflare.com` because cdnjs publishes the
  SRI hashes alongside their library mirror.

### Fixed

- **`FileResponse(path)` warns on the event loop** (#79). The sync
  constructor previously did an `open(path).read()` on the running
  loop, stalling every concurrent request for the duration of the
  read. The constructor now emits a `DeprecationWarning` when called
  with a loop running and points callers at `await
  FileResponse.from_path(path)` (or `asyncio.to_thread(...)` from sync
  code). The next major bump will tighten this to a hard error; for
  now the established sync helpers (`send_file`, `send_static_file`)
  keep working unchanged.
- **`Veloce()`'s first-request lock binds to the running loop**, not
  the loop current at construction (#80). The lock is allocated lazily
  inside `_run_request` the first time it is needed, so `Veloce()`
  instantiated at module scope and driven by `TestClient` (or any
  other later-spawned loop) no longer raises
  `RuntimeError: ... attached to a different loop`.
- **`TestClient.receive()` returns `http.disconnect` past end-of-body**
  (#81). The previous implementation awaited a fresh
  `asyncio.Event().wait()` that was never set, hanging legitimate ASGI
  middleware that read past `more_body=False`.
- **TestClient multipart escapes name + filename** (#82). The encoder
  now rejects `\r\n` in field/file names and backslash-escapes embedded
  `"` so a malformed name cannot break the MIME boundary or inject
  header fields.
- **Response body assignment invalidates the encode cache** (#89). A
  middleware that wrote `response.body = ...` after a prior `.encode()`
  used to emit stale bytes plus the wrong `Content-Length`. The `body`
  slot is now a property whose setter clears the cached `_encoded`
  blob.
- **Error handlers can receive the real failing request** (#86).
  `Veloce.handle_http_exception(exc, request=...)` and
  `handle_user_exception(exc, request=...)` now accept an optional
  request parameter; in-request callers pass the real one, out-of-band
  callers (background tasks, CLI hooks) still get a synthetic `GET /`.
- **`Router._merge_node` preserves route constraints** (#88).
  `include_router` no longer silently drops `subdomain`, `host`,
  `defaults`, `callbacks`, `openapi_extra`, `trailing_slash`, or
  `tolerant_slash` when merging a sub-router. Each was reconstructed
  without those fields previously.
- **`RateLimitMiddleware` periodic sweep is lock-guarded** (#84). Two
  concurrent requests entering the sweep window cannot both rebuild
  the bucket dict. Single-threaded asyncio already serialised this
  block, but a future async cache backend that introduced an `await`
  inside the sweep would have opened a race; the lock is defensive.
- **Four runtime guards stop relying on `assert`** (#87 + follow-up).
  `router.py:487` (radix-tree converter pin), `app.py:950` (middleware
  decorator type check), `serving/protocol.py:91` (asyncio
  full-duplex transport check), and `testclient.py:738`
  (WebSocket-accept message-type check) were all stripped under
  `python -O`. Replaced with explicit `raise` so the guard survives
  optimised deployments.

### Changed

- **`_pydantic_to_schema` logs schema-generation failures at WARNING**
  (#91) instead of silently degrading the affected model to
  `{type: object}`. The fallback still applies so `/docs` keeps
  rendering, but developers now have a log trail to diagnose the
  underlying issue. Set `logging.getLogger("veloce.openapi").setLevel(DEBUG)`
  to capture the full traceback.
- **`RateLimitMiddleware` docstring documents the in-process scope**
  (#93). The class is single-process by design; multi-worker
  deployments will see an effective limit of roughly `N × max_requests`
  per window. The docstring now points readers at nginx `limit_req` or
  a Redis-backed implementation for cross-worker accuracy.

### Polish

Issue #92 — six low-severity items from the same review batch:

- **L1**: `urlencode` hoisted to module top of `routing/router.py` so
  the `url_for` hot helper does not re-import on every external link.
- **L2**: redundant inline `import orjson` removed from
  `Response.get_json` (already imported at module top).
- **L4**: `TestClient` ASGI scope `raw_path` now UTF-8 encoded instead
  of ASCII, so tests that hit a non-ASCII URL no longer crash with
  `UnicodeEncodeError`. Applies to both the HTTP and the WebSocket
  scope builder.
- **L5**: `Session.update({})` no longer marks the session modified.
  Defensive empty-mapping calls stop forcing a `Set-Cookie` on every
  request. Iterator and generator arguments still flip `modified` —
  they can't be inspected for emptiness without consuming them, and
  the consumption already happened in `super().update`.
- **L6**: missing API key returns `401 Unauthorized` instead of `403
  Forbidden` (RFC 7235 §3 — 401 means "authentication required", 403
  means "authenticated but not authorised"). Applies to
  `APIKeyHeader`, `APIKeyQuery`, and `APIKeyCookie`.

## [0.1.2] - 2026-05-23

First post-release iteration. Closes four rough edges surfaced by the
end-to-end smoke test of the published `veloceframework==0.1.1` wheel.

### Changed

- **`Request.json()` is now `async`** (#74). Previous releases shipped
  `json()` as a synchronous method while `form()` was already `async`;
  the asymmetry broke the `await request.json()` idiom Starlette,
  FastAPI, and Quart callers reach for first. Migration: any call site
  that wrote `request.json()` now writes `await request.json()`. The
  Flask-flavoured `request.get_json()` stays synchronous so Flask
  muscle-memory continues to work.
- `pyproject.toml` runtime dependencies extended with `uvicorn[standard]`,
  `jinja2`, and `click` (#77). They were declared only in the dev
  dependency group on 0.1.1, so a fresh `pip install veloceframework`
  left users without `uvicorn` on the path and without `jinja2` for the
  templating helpers the docs point at. WebSocket support stays opt-in
  through the new `veloceframework[ws]` extra (`pip install
  veloceframework[ws]`) so REST-only deploys do not pull the
  `websockets` library.
- `veloce.__version__` is now derived from package metadata via
  `importlib.metadata.version("veloceframework")` (#75), with a literal
  fallback for editable installs without materialised metadata. The
  hand-maintained constant in `__init__.py` could and did drift from
  the wheel's `pyproject.toml` version (`0.1.0` vs `0.1.1` on the
  previous release); deriving from metadata makes the two impossible
  to disagree.

### Added

- `render_template`, `render_template_string`, and `Jinja2Templates`
  are now exported from the top-level `veloce` package (#76). The
  helpers always lived under `veloce.contrib.templating`; surfacing
  them at the root matches the Flask muscle-memory that the rest of
  the Veloce API preserves (`g`, `flash`, `current_app`,
  `before_request`, `redirect`, `make_response`, `abort`, `url_for`).

## [0.1.1] - 2026-05-23

Metadata-only release. No code, behaviour, or dependency changes
against 0.1.0 — this version exists solely to correct the maintainer
email recorded in the PyPI package metadata.

### Changed

- `pyproject.toml`: `authors` and `maintainers` email corrected from
  `revanthravella@gmail.com` to `lokeshtallapaneni@gmail.com`. The PyPI
  v0.1.0 metadata is immutable, so the fix lands as 0.1.1; users on
  v0.1.0 will pick up the corrected metadata on the next
  `pip install --upgrade veloceframework`.

## [0.1.0] - 2026-05-23

First public release. Veloce is published to PyPI as `veloceframework`;
the import name `veloce` is unchanged.

### Highlights

- Async-first ASGI core with a hand-written radix-tree router, custom
  request/response pipeline, in-memory `TestClient`, and a dependency
  injection system that resolves precompiled plans (`HandlerPlan`) at
  registration time so the per-request hot path performs no reflection.
- Feature surface covers Flask 3.x and FastAPI parity for the workflows
  most apps reach for first — blueprints, dependency injection, OpenAPI
  generation, Jinja templating, WebSockets, sessions, signals, and a
  complete Werkzeug-shape request/response API.
- Performance contract: comparative benches in `benchmark.py` show
  3-5x throughput vs equivalent FastAPI handlers and 4-7x vs Flask on
  the JSON-hello and path-param hot paths.

### Added

The entries below were authored during the `[Unreleased]` window and
ship as part of this release.

### Changed

- **Per-request dispatch +21-39 % (profile-driven DSA pass).** Walked the
  json-hello / path-param hot path under `cProfile` and applied seven
  targeted shaves, each attributed to a measured delta:
  * `Veloce._dispatch_request` defers `DependencyResolver()` until a
    non-trivial route demands it. Trivial-plan routes (no injected
    params, no dependencies) never construct the resolver — saves the
    resolver allocation + two attribute writes per static-GET request.
  * `DependencyResolver.__init__` no longer allocates a throwaway
    `dict` + `WeakKeyDictionary` for `_overrides` / `_override_subplans`;
    they default to module-level empty sentinels and the dispatcher
    swaps in the real instances only when overrides exist.
  * `Request.headers` is now a lazy property backed by `_headers_raw`
    (raw ASGI `(bytes, bytes)` tuples). The `CIMultiDict` + per-tuple
    `latin-1` decode is built only on first read. The hot path never
    reads `request.headers`, so 2-3 us / req of work was being burned
    on every dispatch.
  * `_run_response_middleware` is gated at the main hot-path return
    so the no-op coroutine + await is skipped when no middleware is
    registered (avoids ~940 ns / req of frame setup).
  * `_asgi_app` reads `scope["path"]` / `scope["query_string"]` via
    subscript (ASGI mandates both keys), skipping `dict.get` default
    handling.
  * Built-in `Content-Type` strings (`application/json`,
    `text/html; charset=utf-8`, `text/plain; charset=utf-8`,
    `application/octet-stream`) and small `Content-Length` values
    (0-2047) hit precomputed bytes caches; the per-request
    `_reject_header_crlf(...).encode()` + `str(n).encode()`
    allocations are skipped on cache hit.
  * `Response._stream` joins `__slots__` initialised to `None`; the
    `is_streamed` / `freeze` / `iter_encoded` / `iter_chunked` /
    `cache_control` lookups become a direct slot load instead of a
    `getattr(..., None)` walk.
  In-loop bench (`bench/hot_dispatch_bench.py`) median of 3 runs:
  static GET 68.1k → 94.8k req/s (+39 %),
  path-param GET 54.5k → 66.0k (+21 %),
  POST 64-byte body 62.0k → 77.2k (+25 %).
  cProfile total time for 16k mixed dispatches dropped 1.51 s → 0.59 s
  (~2.56×).
- **Stdlib `json` dropped in favour of `orjson` at the remaining two
  sites.** `Config.from_prefixed_env`'s default `loads` is now
  `orjson.loads`; `Config.from_file`'s default `load` is a new tiny
  `_orjson_load(fp)` adaptor (orjson has no file-object loader).
  `Swagger UI` HTML render emits `swagger_ui_parameters` and
  `swagger_ui_init_oauth` via `orjson.dumps(...).decode()`. orjson
  produces compact JSON (no space after `:`); the on-wire format
  for embedded literals is now `"key":value` rather than `"key": value`
  — the whitespace was never part of any contract and the JS parser
  consumes both identically. Behaviour-equivalent for valid JSON;
  catch sites unchanged because `orjson.JSONDecodeError` is a
  `ValueError` subclass.
- **Per-request dispatch ~+17 %.** Profile-driven pass over the in-loop
  ASGI hot path: `_setup_openapi` gated at call sites so the no-op
  branch costs one attribute read instead of a frame; `_endpoint_blueprint`
  no longer parsed three times per request when no blueprint hooks are
  registered; `Headers`, the `current_app`/`current_request` contextvars,
  and the `request_started`/`request_finished` signals hoisted to module
  top instead of being re-imported per request; single-chunk request
  body fast-path skips the `body_parts` list + `b"".join`;
  `_reject_header_crlf` inlined as three short-circuited `in` checks;
  `Signal.has_receivers_for` short-circuits on empty subscriber list;
  `_run_teardowns` await skipped when no yield-dependencies registered.
  In-loop bench (`bench/hot_dispatch_bench.py`): static GET 62.5k →
  72.8k req/s (+16.5 %), path-param 47.8k → 57.0k (+19.4 %), POST 64-byte
  body 54.8k → 64.2k (+17.2 %).
- **Router micro-ops.** `Router.match` tries the raw method on
  `handlers.get` before `method.upper()` (RFC-conforming clients send
  uppercase already); `_match_node` flattens static-only descent into a
  `while` loop when the current node has no param/wildcard alternatives,
  shaving one Python frame per static segment; single-param-child path
  skips the rollback `del` (no alternative to back off to);
  `FloatConverter.match` checks `"e" / "E"` directly instead of
  allocating `value.lower()`.
- **Per-request rate-limit O(N) → O(1).** `RateLimitMiddleware` switches
  from per-request list comprehension to `collections.deque` +
  amortised `popleft`. Periodic eviction sweep now mutates in place
  with a snapshot-then-recheck guard so an append racing with the
  sweep is not silently dropped.
- **Override-dependency sub-plan cache hoisted to the app.** Each
  request's fresh `DependencyResolver` shares
  `Veloce._override_subplans`, eliminating the per-request `build_plan`
  + triple `inspect.is*function` probe on override hits. The cache is
  cleared when `dependency_overrides` is reassigned.
- **Mount-prefix slash precomputed.** `_mounted_apps` /
  `_asgi_mounts` now store `(prefix, prefix + "/", app)` so dispatch
  doesn't reallocate `prefix + "/"` per request per mount.
- **Exception-handler signature cache.** `_call_exc_handler` memoises
  `(wants_request, wants_exc)` flags per handler in a
  `WeakKeyDictionary`, eliminating the `inspect.signature` walk per
  raised exception.
- **`jsonable_encoder` primitives short-circuit.** The
  `None | str | int | float | bool` branch is hoisted to the top of
  the dispatch so leaf calls hit it before any of the heavier
  `isinstance` checks.

### Fixed

- **ETag drift between StaticFiles and FileResponse.** `StaticFiles._compute_etag`
  now delegates to `veloce.http.response._file_etag` so a static handler
  and `FileResponse` over the same file emit identical ETags and
  validate identically against `If-None-Match`. Signature changed
  from `(path, mtime)` to `(path, size, mtime)` — `_`-prefixed and
  therefore private, but flagged for subclassers.
- **WebSocket ASGI-mode unbounded queue.** `WebSocket.from_asgi`
  builds `_receive_queue` with `maxsize=DEFAULT_RECV_QUEUE_MAXSIZE`
  instead of an unbounded queue. The queue is unused in ASGI mode
  today; the bound prevents a footgun if future changes start feeding
  it.

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
- `WebSocket.origin` accessor returns the handshake `Origin` header
  (or `None`); `WebSocket.check_origin(allowed)` returns `True` only
  when the origin is on the allow-list. Normalisation
  (`.rstrip("/").lower()`) and wildcard (`"*"`) semantics match the
  registered-once `WebSocketOriginMiddleware`, so allow-lists are
  interchangeable between the two APIs. `Origin: null` (sandboxed
  iframes / `file://`) is rejected. The pair lets handlers reject
  Cross-Site WebSocket Hijacking before `accept()` — the WebSocket
  handshake is plain HTTP, so Same-Origin Policy and CORS do not apply.
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
- `WebSocket.receive_text` / `receive_bytes` / `receive_json` enforce the
  same handshake state machine as their `send_*` siblings: calling them
  before `accept()` raises `RuntimeError` (was: hung on an empty queue)
  and calling them after `close()` raises `WebSocketDisconnect`.
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

- Radix-tree param-child lookup is now O(1) at registration via a
  `(param_name, converter_type)` sidecar index (CL40 / CL41). The
  ordered list remains the source of truth at match time so traversal
  semantics are unchanged. `_split_path` also drops its `strip("/")`
  pass — the empty-string filter on the split already handles
  leading, trailing, and consecutive slashes.
- Independent sibling `Depends()` slots now resolve in parallel via
  `asyncio.gather` when safe (CL10). The resolver looks ahead for
  contiguous `K_DEPENDS` siblings and dispatches them concurrently
  unless the run contains a `Security()` scope-pushing slot, a
  yield-style dependency, or two siblings sharing a `use_cache=True`
  callable — those cases preserve the sequential semantics that
  protect the resolver's shared cache, security-scope stack, and
  teardown ordering. Handlers with multiple I/O-bound deps now wait
  for `max(durations)` instead of `sum(durations)`.
- Blueprint hooks (`before_request`, `after_request`,
  `teardown_request`) are now bucketed by blueprint at registration
  (CL22). Dispatch reads the bucket for the matched route's endpoint
  prefix instead of iterating every blueprint's gated wrapper and
  doing a `startswith` no-op on each — eliminates the O(B·H)
  per-request overhead for apps with many blueprints.
- `StaticFiles` streams files at or above `STREAM_THRESHOLD` (1 MiB
  default) via `StreamingResponse` instead of buffering the whole
  body (CL4). Worker RSS no longer grows by the file size for the
  duration of a large download. Range requests still buffer the
  slice, which is already bounded by the client.
- `Request.mimetype` and `Request.mimetype_params` now share a single
  cached parse (CL14): the first access populates `_parsed_ct` with a
  `(mimetype, params)` tuple; subsequent reads on either property hit
  the cache. Handlers that touch `content_type` repeatedly (form
  parsers, validators) stop re-splitting the same string per access.
- `_find_exception_handler` now memoises the MRO walk per exception
  type. The cache is cleared whenever a new error handler is
  registered so a fresh registration takes effect for previously
  cached subclasses.
- `SignedSerializer.loads` does a single `token.split(".", 2)`
  instead of `token.count(".") != 2` + `token.split(".")`. The
  early-validation path is now one pass over the string.
- `LoggingMiddleware` short-circuits when the logger has the access
  level disabled — both the `time.monotonic()` clock read on entry
  and the duration calculation on response.
- `SessionMiddleware._cookie` / cookie composition use
  `"; ".join(parts)` instead of a chain of `+=` concatenations,
  cutting intermediate string allocations.
- `StaticFiles` directory listing reads `is_dir` from `os.scandir`'s
  cached dirent — saves a per-entry `os.path.isdir` syscall.
  Behaviour note: symlinks inside a listed directory are now classified
  via the symlink itself, not its target — a symlink to a subdirectory
  renders as a plain entry rather than a directory entry. This avoids
  advertising symlink targets in the listing and matches the
  symlink-safety stance the static handler already takes.
- `Response.encode()` no longer rebuilds the header dict on every
  response: the three framework defaults (`Content-Type`,
  `Content-Length`, `Connection`) are emitted inline only when the
  caller has not supplied them. A case-insensitive check at the same
  time removes a latent duplicate-header bug where a user-supplied
  `"content-type"` (lowercase) would land alongside the default
  `"Content-Type"` in the encoded line. Reason phrase comes from the
  module-level `{code: phrase}` map already added for `Response.status`.
- `StaticFiles` now satisfies the existence + stat with a single
  executor `os.stat` call, classifying file/dir from `st_mode` —
  the previous request path issued `isfile` and then a second `stat`
  for size/mtime, doubling executor round-trips.
- WebSocket `_send_frame` hands the header + payload to the transport
  as two separate buffers via `writelines` instead of
  `bytearray.extend(data)` + `bytes(frame)`. On a 64 KiB frame that
  saves a 64 KiB memcpy on the way out.
- SSE: single-line event payloads skip the `data.split("\n")` list
  allocation; chunked SSE writes use `transport.writelines` instead
  of concatenating size-line + body + trailer into one fresh bytes
  per chunk.
- SSE status-line reuses the response-module `_STATUS_PHRASES` table
  rather than `from http import HTTPStatus` + `HTTPStatus(code).phrase`
  on every stream startup.
- `http_date(None)` (the per-response `Date:` header) caches the
  RFC 9110 IMF-fixdate to one-second resolution — `formatdate()` ran
  once per response despite the output only changing once a second.
- App-level hook dispatch (`teardown_request`, `teardown_appcontext`,
  `_call_handler`) reads `iscoroutinefunction` from a memoised cache
  keyed by `id(fn)`. Hooks register once and are dispatched many times;
  the inline `inspect.iscoroutinefunction(...)` walk on every request
  is replaced by a dict lookup.
- `CORSMiddleware` precomputes `", ".join(self.allow_methods)`,
  `", ".join(self.allow_headers)`, and `", ".join(self.expose_headers)`
  at construction. Per-response preflight emission now hits the
  precomputed strings instead of rebuilding them on every cross-origin
  reply.
- Cookie-based `SessionMiddleware` drops the
  `json.dumps(sort_keys=True)` mutation tripwire (computed on entry
  *and* on exit) in favour of the `Session.modified` flag the
  `Session` container already maintains — saves two full
  serialisations on every request that traverses the middleware.
- WebSocket frame unmasking is now bulk-XOR via `int.from_bytes` /
  `to_bytes` over the tiled mask, replacing a Python-level per-byte
  loop. Saves measurable CPU on any frame past a handful of bytes
  (WebSocket payloads are usually hundreds to KiB-sized).
- `Response.status` reads the reason phrase from a module-level
  `{code: phrase}` map built once at import time, instead of
  constructing an `HTTPStatus(code)` IntEnum-walk on every access.
- `email.utils.parsedate_to_datetime` is now imported at module load
  in `veloce.http.request` instead of re-imported inside four
  conditional-request hot properties.
- `StaticFiles` caches `mimetypes.guess_type` per file path
  (bounded LRU, 512 entries) — `guess_type` was running its full MIME
  table walk on every static hit.
- `safe.secure_filename` uses a module-level compiled regex for the
  underscore-run collapser, removing the per-call `re` cache lookup.
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
