# Changelog

All notable changes to this project are documented here.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

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
- The built-in development server's WebSocket parser reassembles
  fragmented messages — a `FIN=0` data frame followed by continuation
  frames (RFC 6455 §5.4); control frames may be interleaved without
  disturbing the in-progress message.
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
