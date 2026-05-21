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

### Changed

- A handler that returns a bare `str` now defaults to
  `Content-Type: text/html; charset=utf-8` (previously `text/plain`), so a
  bare-`str` return and `make_response(str)` produce the same media type.

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
