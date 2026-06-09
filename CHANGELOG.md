# Changelog

All notable changes to this project are documented here.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- MCP authorization: `mount_mcp(transport="http", auth=MCPAuth(...))` makes the
  endpoint an OAuth 2.1 resource server — a user-supplied `verify` callable
  validates the bearer token on every request, the RFC 9728 protected-resource
  metadata is served, and a missing/invalid token returns `401` (insufficient
  endpoint scope returns `403`) with a `WWW-Authenticate` challenge. Declarative
  per-tool scopes (`@app.mcp_tool(scopes=...)`, `mcp_scopes=` on exposed routes)
  are enforced against the request principal.
- `Principal` + `current_principal()` / `set_principal()`: a unified authenticated
  identity populated by HTTP auth or the MCP transport, so authorization and
  identity-aware dependencies read one source across both doors.
- `Request.is_mcp` marks a replayed MCP tool/resource call, so auth middleware can
  defer to the transport on agent calls while business middleware runs unchanged.
- MCP Streamable HTTP transport: `app.mount_mcp(transport="http", path="/mcp")`
  mounts the MCP server as a `POST` route, so it can run as a remote/hosted server
  under any ASGI server. A request with `Accept: text/event-stream` is answered with
  an SSE stream of the call's progress/log notifications followed by the JSON-RPC
  response; otherwise a single JSON response. The route is protected by whatever
  middleware and dependencies the app applies to it.
- MCP progress and logging: `MCPContext.report_progress(...)` and
  `MCPContext.log(...)` now send live `notifications/progress` and
  `notifications/message` to the client (progress requires the client's
  `progressToken`); the server handles `logging/setLevel` and advertises the
  `logging` capability.
- MCP per-call timeout: set `app.config["MCP_CALL_TIMEOUT"]` (seconds) to bound each
  tool call, resource read, and prompt render; an overrun is cancelled and surfaced
  as an in-band tool error or a JSON-RPC error. Unset (no timeout) by default.
- MCP prompts: register a reusable prompt template with `@app.mcp_prompt(...)`. The
  callable's parameters become the prompt's arguments and its return (a string or a
  list of role/content messages) becomes the rendered messages; the server answers
  `prompts/list` and `prompts/get`, with `Depends`/`MCPContext` resolved as in a
  tool, and advertises the `prompts` capability when at least one is registered.
- MCP resources: expose a read-only (`GET`/`HEAD`) route as a Model Context
  Protocol resource with `expose_as_mcp_resource=True` and `mcp_resource_uri=...`
  (a static URI, or a URI template such as `users://{user_id}` binding the route's
  path parameters). The server answers `resources/list`, `resources/templates/list`,
  and `resources/read`, replaying the route's dependencies, security, and
  `response_model` through the shared invocation path; it advertises the
  `resources` capability when at least one resource is registered.
- MCP non-text tool content: a tool returning an `image/*` or `audio/*` response
  emits the matching typed MCP content block (base64), and a binary resource read
  returns its bytes as a `blob`.

### Security

- MCP: a pure `@app.mcp_tool` handler error (and the defensive internal-error path)
  surfaces a generic message unless `app.debug` is set, so an exception carrying a
  secret is not returned verbatim to the agent.

## [0.4.0] - 2026-06-08

### Added

- Configurable rate limiting: selectable algorithms (`FixedWindow`,
  `SlidingWindow`, `TokenBucket`), pluggable in-memory or Redis backends, and
  per-route limits via `overrides` or the `@rate_limit` decorator.
- Result caching: the `cached` decorator with `InMemoryCache` and `RedisCache`.
- `veloce.contrib.redis`: `RedisSessionStore`, `RedisRateLimitBackend`, and
  `RedisCache` for state shared across workers.
- msgspec as an opt-in fast validation and serialization backend.
- Model Context Protocol integration (`veloce.contrib.mcp`): tool exposure over
  stdio, protocol-version negotiation, `ping`, route-derived tool metadata, and
  streaming-result tools.
- JSON Web Tokens (`encode_jwt` / `decode_jwt`), storage-free reset tokens
  (`make_reset_token` / `check_reset_token`), and a `Secret` wrapper that resists
  accidental disclosure.
- `CSPMiddleware` (Content-Security-Policy with a per-request nonce and
  report-only mode) and `ConditionalGetMiddleware` (`304` for `If-None-Match` /
  `If-Modified-Since`).
- `CORSMiddleware` gains Private Network Access support and preflight-method
  validation; `CSRFMiddleware` gains Origin verification via `trusted_origins`.
- Middleware ordering with `add_middleware(..., priority=N)` and per-route
  opt-out with `exclude_middleware=[...]`.
- Background-task supervision: `app.supervise(...)` (restart policy) and
  `app.spawn(...)` (app-scoped tasks).
- Routing: constrained converter syntax (`{x:converter(arg)}`), `date` / `time` /
  decimal path converters, duplicate-route detection, and the declarative
  `@app.websocket_listener` route.
- `StaticFiles`: precompressed-sibling serving, `html=True` directory indexes,
  and write-side `If-Match` / `If-Unmodified-Since` preconditions.
- WebSockets: native-transport server support on `Veloce.run()`, an idle-receive
  timeout, async-context-manager support, heartbeats, send backpressure, and
  UTF-8 / close-frame validation.
- Server-Sent Events: `ServerSentEvent.comment` and `.json`, bare-value source
  iterators, and a proactive heartbeat.
- Observability: `instrument_access_log` / `log_requests_as_json`, a Prometheus
  exporter (`instrument_with_prometheus`), and an OpenTelemetry bridge with a
  live-tracing mode and an `on_span` hook.
- OpenAPI: separate request/response schemas, identity-keyed components,
  operationId de-duplication, a documented `422` response, and a
  `validate_openapi` flag.
- Sessions: sliding expiry, `domain=` / chunked-cookie options, and
  `Vary: Cookie` on cookie-varying responses.
- Encoder extensibility: a per-call `custom_encoder`, process-level
  `register_encoder`, and broader built-in coverage (`bytes`, `set`/`frozenset`,
  `pathlib.Path`, `re.Pattern`, scalar subclasses).
- Deployment: optional gunicorn `VeloceWorker`, built-in dev-server TLS, an
  ASGI-app `mount`, `.env` loading, a dev event-loop watchdog, and an async
  `TestClient`. `uvicorn` is now an optional extra rather than a hard dependency.
- New top-level exports: `Config`, `Aborter`, `URLRule`, `SetupError`,
  `JSONProvider` / `DefaultJSONProvider` / `config_orjson_options`,
  `get_openapi_schema` / `setup_openapi_routes`, `StaticFiles`,
  `Jinja2Templates`, `log_requests_as_json`, and `async_send_file`.
- Developer documentation: a build-one-app tutorial, a runnable `examples/`
  directory, a databases guide, and a Hypothesis fuzzing harness across the
  parsers, router, signing, and WebSocket paths.

### Changed

- The deprecated `Veloce.on_event()` / `Veloce.add_event_handler()` now target
  removal in `1.0.0`.
- `Veloce.run(workers=...)` raises `ValueError` for any worker count other than
  `1` (the built-in server is single-process).
- Independent dependencies resolve concurrently, and a no-wave `Depends` chain
  compiles to a straight-line async resolver.
- Numerous per-request and schema-generation paths were optimized — a compiled
  feature pipeline, indexed route/encoder lookups, and bounded caches — without
  changing public behavior.
- Route resolution gates its mounted-app, static-handler, and ASGI-mount scans on
  the compiled pipeline flags, skipping each scan when nothing of that kind is
  registered.
- Literal request paths resolve through a registration-time exact-match map in one
  hash lookup instead of a radix-tree walk, falling through to the tree for
  parameterized, wildcard, and slash-redirect routes (literal `match()` ~1.7x
  faster, ~3x on deep literal paths).
- Requests to feature-free apps take a straight-line dispatch fast path: when no
  middleware, request/response hooks, mounts, or url-value preprocessors are
  registered and the matched route is an async trivial or request-only handler
  with no response model, custom response class, non-default status, host or
  subdomain constraint, defaults, or middleware exclusion, the middleware, hook,
  route-resolution, and dependency-resolution orchestration is skipped while
  coercion, `after_this_request` callbacks, background tasks, exception handling,
  and teardown remain shared (~6-8% lower per-request dispatch time on those
  routes, in-process A/B).

### Fixed

- Per-route rate-limit state now rebuilds when routes are added after startup.
- A bodiless status (`1xx`, `204`, `205`, `304`) no longer advertises a body, the
  WebSocket handshake uses the correct RFC 6455 GUID, and a frame with a non-zero
  RSV bit is rejected.
- `HTTPBasic` / `HTTPDigest` escape the `realm`, non-latin-1 header values are
  RFC 2047 encoded, and `decode_jwt` rejects an empty secret.
- JSON serialization handles `set`/`frozenset`, `pathlib.Path`, integer-valued
  `Decimal`, and `exclude_none`; `StaticFiles` precompressed selection returns
  `406` and honours an explicit `q=0`.
- Assorted correctness fixes across OpenAPI dual-schema comparison, scope-aware
  dependency caching, `instrument_with_otel` idempotency, signal delivery, and
  per-route middleware-exclusion symmetry.

### Security

- `LoggingMiddleware` and the access log escape control characters in
  request-derived fields (CWE-117 log forging), and `RequestIDMiddleware`
  sanitizes an inbound request id.
- Security headers are matched case-insensitively so a handler override is not
  silently replaced; the cookie writer round-trips a literal `%`.
- The native WebSocket server rejects an unmasked client frame, HTTP Basic
  rejects an RFC 7617-malformed credential, and `dump_cookie` rejects a
  non-token cookie name.
- `safe_join` rejects Windows reserved device names, `URL.from_request`
  validates the `Host` header (RFC 3986), and the router rejects a path that
  binds one parameter name twice.

## [0.3.0] - 2026-06-01

### Fixed

- `StaticFiles` now applies RFC 9110 `If-Range` validation correctly before
  serving partial responses.

## [0.2.0] - 2026-05-31

### Added

- Streaming request bodies on the built-in HTTP server, so large uploads no
  longer require buffering the full body before dispatch.
- CLI plugin discovery, `.env` loading, template streaming, SSE heartbeat
  support, OpenTelemetry integration, and a signal namespace helper.
- Hybrid routing for patterns that do not fit the radix tree, plus an optional
  gunicorn worker.
- Broader documentation coverage across configuration, templates, static files,
  sessions, signals, and related framework guides.

### Changed

- Request body access is now asynchronous: `request.body()`, `request.text()`,
  and `request.get_data()` must be awaited.
- `request.stream()` now streams on the raw HTTP path instead of replaying an
  already-buffered body.
- Debug mode renders an HTML traceback page for clients that prefer HTML while
  preserving plain-text tracebacks for CLI and programmatic clients.
- Resolver and response-encoding internals were consolidated and optimized
  without changing the public API.

### Fixed

- Correct handling for `If-Range`, partial-content gzip behavior, async template
  context processors, duplicate response headers, hybrid-router edge cases, and
  gunicorn worker lifecycle/TLS behavior.

### Security

- Restored strict header validation on streamed responses.
- Applied the same form-field limits to URL-encoded bodies as multipart forms.

## [0.1.4] - 2026-05-25

### Changed

- Focused maintenance release covering security hardening, correctness fixes,
  API cleanup, and small internal consolidations.
- Improved encoder behavior, cached more parsed request metadata, and reduced
  duplicated logic across middleware, CLI helpers, templating, and the test
  client.

### Security

- Tightened multipart UTF-8 validation, `HTTPBasic` challenge construction, and
  exception handling around basic-auth parsing.
- Made HSTS subdomain coverage opt-in rather than implicit.

### Removed

- Dropped unused internal constants from the handler-plan implementation.

## [0.1.3] - 2026-05-23

### Changed

- Security and correctness release covering CSRF token rotation, password-hash
  parameter validation, and several framework/runtime fixes.
- Improved diagnostics around OpenAPI schema generation and clarified the
  process-local scope of the built-in rate limiter.

### Fixed

- Addressed loop-affinity issues in `Veloce()`, multipart encoding in the test
  client, stale response-encode caches, router merge behavior, and several
  runtime guards that previously relied on `assert`.

### Security

- Added CSRF token rotation support after login or privilege changes.
- Rejected weak or tampered scrypt parameters during password verification.
- Added SRI protection for Swagger UI and ReDoc assets.

## [0.1.2] - 2026-05-23

### Added

- Top-level exports for `render_template`, `render_template_string`, and
  `Jinja2Templates`.

### Changed

- `Request.json()` became asynchronous for consistency with the rest of the
  request-body API.
- Runtime dependencies were corrected so standard installs include the pieces
  needed for documented framework features.
- `veloce.__version__` now comes from installed package metadata.

## [0.1.1] - 2026-05-23

### Changed

- Metadata-only release correcting maintainer information in the published
  package.

## [0.1.0] - 2026-05-23

### Added

- Initial public release of Veloce as `veloceframework`.
- Core framework surface including the `Veloce` app, radix-tree routing,
  request/response primitives, dependency injection, OpenAPI generation, and
  an in-memory `TestClient`.
- Built-in middleware, sessions, templating, signals, background tasks,
  Server-Sent Events, WebSockets, security helpers, and class-based views.
- CLI commands, static-file support, instrumentation hooks, server-side
  sessions, async password helpers, and the first round of performance-focused
  hot-path improvements.

### Changed

- Set safer defaults and improved consistency across response handling,
  multipart uploads, WebSocket dependency injection, and request streaming.

### Fixed

- Corrected early issues in blueprint registration, SSE encoding, dependency
  coercion, multipart cleanup, session-store race handling, static-file caching,
  logging, and request-scoped resource cleanup.

### Security

- Added incremental request-size enforcement, request timeouts, WebSocket origin
  checks, security headers, signed CSRF tokens, multipart limits, and secure
  deployment audit helpers.
