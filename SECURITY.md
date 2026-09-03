# Security Policy

## Supported versions

Veloce is pre-1.0 (`0.x`). Security fixes land on the latest released
`0.x` version only; there is no back-port window before 1.0.

| Version | Supported |
|---------|-----------|
| latest `0.x` | ✅ |
| older `0.x` | ❌ |

## Reporting a vulnerability

**Please do not open a public issue for a security vulnerability.**

Report it privately through GitHub's **private vulnerability reporting**:
open the repository's **Security** tab and choose **"Report a
vulnerability"**. This creates a private advisory visible only to the
maintainer and to you.

A useful report includes:

- the affected component and version (or commit),
- a description of the issue and its impact,
- a minimal proof-of-concept or reproduction steps,
- any suggested remediation, if you have one.

## What to expect

- **Acknowledgement** within a few days of the report.
- An initial assessment (severity, affected versions) shortly after.
- A fix developed under the private advisory, with a coordinated
  release. You will be kept updated and credited in the advisory and
  changelog unless you ask otherwise.
- Please allow a reasonable disclosure window before any public
  write-up so a fixed release can ship first.

## Scope

In scope: the `veloce` package — routing, the request/response pipeline,
dependency injection, middleware, the signing helpers, and the built-in
development server.

Out of scope: the built-in development server is **not** intended for
production (see `docs/guide/deployment.md`); deployment-hardening of a
production ASGI server (uvicorn, etc.) is that server's responsibility.

## Hardening features the framework ships

The bundled middleware in `veloce.middleware.security` covers the
common hardening primitives — register the ones your deployment
needs:

- **`SecurityHeadersMiddleware`** — `nosniff`, `X-Frame-Options`,
  HSTS, `Referrer-Policy`, an optional CSP. Register it explicitly:
  `app.add_middleware(SecurityHeadersMiddleware(hsts_max_age=31536000))`.
- **`TrustedHostMiddleware`** — `Host`-header allow-list; rejects
  spoofed `Host` headers used to abuse URL generation or cache keys.
- **`HTTPSRedirectMiddleware`** — redirects plain HTTP to HTTPS when
  the framework is behind a TLS-terminating proxy.
- **`WebSocketOriginMiddleware`** — allow-list for the WebSocket
  handshake `Origin` header. The allow-list can't be inferred from the
  app, so register it explicitly when you serve WebSockets.
  See [WebSockets → Origin validation](docs/guide/websockets.md#origin-validation-cswsh-defence)
  for the full walkthrough and the per-handler
  `WebSocket.check_origin(allowed)` alternative.

For the underlying CORS preflight + browser-fetch enforcement (HTTP,
not WebSocket), use `veloce.middleware.cors.CORSMiddleware`.

## Before 1.0

An external security review of the request path is planned ahead of the
1.0 release. Until then, treat the framework as pre-production software.
