---
description: How Veloce compares with FastAPI, Flask, Starlette, and Django for async Python web development — feature matrix, when to choose each, and migration notes.
tags: [comparison, fastapi, flask, starlette, django, alternative]
---

# Veloce vs FastAPI, Flask, Starlette, and Django

Veloce is a from-scratch async Python web framework. Its routing,
request/response pipeline, dependency injection, OpenAPI generator,
WebSocket layer, and test client are all in-tree — not wrappers around
an existing stack. This page lays out how Veloce overlaps with and
differs from the four most common Python web frameworks.

When a public name overlaps with FastAPI or Starlette (`Depends`,
`TestClient`, `JSONResponse`), Veloce reimplements the semantics rather
than re-exporting upstream code.

## Feature matrix

| Area | Veloce | FastAPI | Flask | Starlette | Django |
|------|--------|---------|-------|-----------|--------|
| Async support | Native, async handlers; sync handlers run on a thread executor | Native, sync via thread executor | Async views via `asgiref` since 2.0 | Native | Async views since 3.1, ORM partial |
| Typed dependency injection | `Depends`, `Security`, `SecurityScopes`, `yield` teardown | `Depends`, `Security`, `SecurityScopes`, `yield` teardown | Extensions only | None built in | None built in |
| Pydantic v2 integration | Built in for query / path / header / cookie / body and `response_model` | Built in | Extensions only | None | Extensions only |
| OpenAPI 3.1 | Built in, served via Swagger UI and ReDoc | Built in (OpenAPI 3.1 since 0.103) | Extensions only | None | Extensions only (drf-spectacular, etc.) |
| WebSockets | Full ASGI surface, DI, subprotocol negotiation, typed iter helpers | Yes, ASGI-native | Via `flask-sock` / extensions | Yes, ASGI-native | Via Django Channels |
| Templating | Jinja2 with `url_for` / `g` / `current_app` globals, async render | Jinja2 via Starlette utilities | Jinja2 built in | Jinja2 helper module | Django template language (DTL), Jinja2 optional |
| Sessions | Signed cookies + server-side backend, `permanent_lifetime`, secret rotation | Via Starlette middleware (signed cookie only) | Signed cookies built in | Signed cookies via middleware | Server-side, DB / cache / cookie backends |
| CSRF | Built-in middleware | Extensions only | Via `flask-wtf` | Extensions only | Built in |
| Flask-style helpers (`g`, `flash`, blueprints) | `g`, `flash`, `get_flashed_messages`, `Blueprint` | None | Built in | None | Different idiom (apps / views) |
| Test client | In-memory `TestClient` driving the real ASGI surface | `TestClient` (Starlette, wraps `httpx`) | `Werkzeug` test client | `TestClient` (wraps `httpx`) | `django.test.Client` (WSGI) |
| Custom routing | Radix tree with typed converters (`int`, `float`, `uuid`, `path`, custom) | Starlette router (sorted list of routes) | Werkzeug routing (map of `Rule` objects) | Sorted list of routes | URLconf list of `path` / `re_path` |
| ASGI / WSGI | ASGI-native | ASGI-native (Starlette) | WSGI-native, ASGI via `asgiref` | ASGI-native | WSGI-native, ASGI optional |
| Python version | 3.10+ | 3.8+ | 3.9+ | 3.9+ | 3.10+ (5.0), 3.12+ (5.2) |

Read each cell as "what ships in the core distribution with no third-party packages installed". Extensions exist for almost every gap, but the cell tracks core surface.

## When to choose Veloce

- You want FastAPI-style typed dependency injection and OpenAPI on top of an async-only request pipeline, without the Starlette layering underneath.
- You want Flask-style helpers (`g`, `flash`, `Blueprint`, `current_app`, `url_for`) and an async pipeline in the same project, without bridging two stacks.
- You want CSRF, sessions, signed cookies, CORS, GZip, ProxyFix, trusted-host, and HTTPS-redirect middleware available out of the box.
- The dispatch hot path matters — Veloce inspects handler signatures once at registration into a `HandlerPlan` and performs no reflection per request. See [Benchmarks](benchmarks.md) for methodology.

## When to choose FastAPI instead

- The project depends on an existing FastAPI plugin, integration, or starter (auth backends, admin UIs, deployment templates) that you do not want to port.
- You need to target Python 3.8 or 3.9.
- You want the larger ecosystem and longer release history that FastAPI currently has.
- Your team is already trained on FastAPI's exact import paths and idioms, and you do not need Flask-style helpers.

## When to choose Flask instead

- The application is primarily synchronous and request volumes are modest. WSGI under Gunicorn is a well-understood deployment surface.
- The project relies on the Flask extension ecosystem (Flask-Login, Flask-SQLAlchemy, Flask-Admin, Flask-Migrate, etc.) and porting those extensions is not worth the gain.
- You do not need OpenAPI or typed request validation in core.
- You want a smaller core surface than Veloce or FastAPI, and you are comfortable adding extensions à la carte.

## When to choose Starlette instead

- You want a minimal ASGI toolkit and you intend to assemble routing, DI, and validation yourself.
- You are building a framework, not an application.

## When to choose Django instead

- The application is content-heavy: admin panel, ORM with migrations, auth, forms, and templates as a single coherent stack.
- You want a long-term-support release cadence and a large package ecosystem aimed at full-stack web apps.
- You do not need a typed, async-first request pipeline at the core.

## Migrating from FastAPI

The public surface overlaps closely. The names `Depends`, `Security`,
`SecurityScopes`, `Body`, `Query`, `Path`, `Header`, `Cookie`, `Form`,
`File`, `UploadFile`, `HTTPException`, `JSONResponse`,
`StreamingResponse`, `FileResponse`, and `TestClient` exist in Veloce
with comparable semantics. Most route handlers move across with only
an import change:

```python
from veloce import Veloce, Depends, HTTPException
```

Differences to watch for:

- Handlers should be `async def`. Sync handlers are accepted and run on a thread executor, but the async path is the supported default.
- `app.state`, `app.config`, and `app.extensions` are Flask-style dicts, not Pydantic settings objects. Pull configuration in at startup and stash it on `app.config`.
- The `TestClient` is in-memory and constructs ASGI scopes directly. There is no `httpx` round-trip and no socket. Tests that need real network behaviour must run uvicorn explicitly.
- The router is a radix tree, not a sorted list of regex routes. Route precedence rules differ for overlapping prefixes — see the [Routing guide](guide/routing.md).

## Migrating from Flask

Veloce keeps the Flask helpers that are async-safe. `g`, `flash`, and
`get_flashed_messages` are backed by `contextvars`, so they are
per-request safe under concurrent handlers. `Blueprint` supports
nesting and URL prefixes. `current_app`, `url_for`, and the Jinja2
globals are wired the same way.

```python
from veloce import Veloce, Blueprint, g, flash, get_flashed_messages
```

Differences to watch for:

- Handlers run on the event loop. Blocking calls (synchronous database drivers, `requests`, `time.sleep`) will stall the loop unless wrapped in `run_in_executor` or replaced with an async equivalent.
- `request` is imported from `veloce`, not from a thread-local module global; pass it as a parameter or use the typed dependency-injection mechanism.
- WSGI middleware does not plug into the ASGI surface directly. Replace it with the equivalent Veloce or ASGI middleware.
- Sessions are signed cookies by default with an optional server-side backend. The signing key lives in `app.config["SECRET_KEY"]`, matching Flask.

## See also

- [Getting started](getting-started.md)
- [Routing guide](guide/routing.md)
- [Dependency injection guide](guide/dependency-injection.md)
- [Benchmarks](benchmarks.md)
