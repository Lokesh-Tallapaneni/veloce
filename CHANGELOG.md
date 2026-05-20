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

### Performance

- Per-request reflection eliminated from the hot path: handler
  signatures are inspected once at registration into a frozen
  resolution plan.
