---
description: >-
  An honest comparison of Veloce and Starlette — where each framework excels,
  what ships in core vs requires assembly, and when to pick one over the other.
tags: [comparison, starlette, architecture, asgi]
---

# Veloce vs Starlette

Starlette and Veloce are both ASGI-native Python frameworks. Starlette is a
lightweight toolkit designed as a foundation for other frameworks (FastAPI is
built on it). Veloce is a batteries-included framework that ships its own
implementations of everything from routing to dependency injection.

This page steelmans both sides: where Starlette is the better fit, where Veloce
adds value, and the trade-offs in between.

## At a glance

| Area | Veloce | Starlette |
|------|--------|-----------|
| **Philosophy** | Batteries included — one install covers DI, validation, OpenAPI, sessions, CSRF, JWT, and more | Minimal toolkit — assemble what you need from the ecosystem |
| **Routing** | Radix tree with typed converters (`int`, `float`, `uuid`, `path`, custom) | Sorted list of regex-compiled routes |
| **Dependency injection** | Built-in `Depends`, `Security`, `SecurityScopes`, `yield` teardown | None — add manually or use a framework like FastAPI |
| **Request validation** | Pydantic v2 for query / path / header / cookie / body, structured 422 errors | None built in |
| **OpenAPI** | OpenAPI 3.1, Swagger UI, ReDoc out of the box | None — Starlette provides the building blocks, not the schema |
| **Sessions** | Signed cookies + server-side backend, secret rotation, `permanent_lifetime` | Signed cookie middleware only |
| **CSRF** | Built-in middleware | Not provided |
| **JWT** | `encode_jwt` / `decode_jwt` in core | Not provided — use PyJWT or similar |
| **Templating** | Jinja2 with `url_for`, `g`, `current_app` globals, async render, context processors | Jinja2 helper module |
| **Test client** | In-memory, drives the ASGI surface directly — no sockets, no `httpx` | Wraps `httpx` for real HTTP |
| **Flask helpers** | `g`, `flash`, `Blueprint`, `current_app`, `session`, `abort` | None |
| **Dispatch model** | Handler signatures inspected once at registration (`HandlerPlan`); no per-request reflection | Routes matched per request via compiled regex list |
| **Python version** | 3.10+ | 3.9+ |
| **Maturity** | Pre-1.0, actively developed | Stable, widely deployed, proven in production |

## Where Starlette excels

**Minimal footprint.** Starlette deliberately provides routing, request/response
classes, WebSocket support, and middleware hooks — then stops. If your project
only needs an ASGI routing layer and you plan to assemble everything else
yourself, Starlette's small surface is an advantage, not a gap.

**Ecosystem and stability.** Starlette has been production-stable since 2018. It
powers FastAPI and is battle-tested at scale across thousands of projects. The
risk surface of a mature, widely-audited codebase is lower than a newer one.

**Foundation for frameworks.** If you are _building_ a framework rather than an
application, Starlette is a better starting point. It is designed as a
composable layer, not an opinionated end product.

**Broader Python support.** Starlette supports Python 3.9+; Veloce requires
3.10+.

## Where Veloce adds value

**Typed dependency injection without FastAPI.** Veloce's `Depends`, `Security`,
and `SecurityScopes` ship in core. You get typed DI, `yield`-style teardown,
and security scopes without adding FastAPI on top of Starlette.

**OpenAPI from handler signatures.** Routes produce OpenAPI 3.1 schemas
automatically — Swagger UI and ReDoc are served without configuration. With
Starlette alone, building an OpenAPI document requires a separate library or
manual schema construction.

**Batteries for real applications.** Sessions with server-side backends, CSRF
middleware, JWT encode/decode, password hashing, rate limiting, signed cookies
with secret rotation, and Flask-style helpers (`g`, `flash`, `Blueprint`) all
ship in one install. With Starlette, each requires a third-party package or
manual implementation.

**Radix-tree routing.** Veloce's router uses a radix tree with typed path
converters. Route matching is a tree walk, not a linear scan through compiled
regex patterns. Custom converters plug into the tree at registration time.

**Precompiled dispatch.** Handler signatures are inspected once at route
registration and compiled into a `HandlerPlan`. The per-request hot path
performs no reflection — no `inspect.signature`, no `get_type_hints`. This
design is described in detail in
[Reflection-free request dispatch](../blog/reflection-free-dispatch.md).

**In-memory test client.** Veloce's `TestClient` drives the ASGI surface
directly in-process. There is no socket, no `httpx` round-trip, and no separate
server process — tests run faster and the failure surface is smaller.

## When to pick Starlette

- You want a **minimal ASGI toolkit** and will assemble DI, validation, and
  OpenAPI yourself (or use FastAPI).
- You are **building a framework** on top of an ASGI layer, not an application.
- You need **Python 3.9** support.
- You need the **ecosystem stability** of a framework with years of production
  history and a large user base.
- Your application already uses **FastAPI** — FastAPI is built on Starlette, so
  you already have it.

## When to pick Veloce

- You want **typed DI, OpenAPI, sessions, CSRF, and JWT** without layering
  FastAPI on Starlette or adding multiple third-party packages.
- You want **Flask-style helpers** (`g`, `flash`, `Blueprint`, `current_app`)
  alongside an async-first pipeline.
- You want the **dispatch hot path** to perform no per-request reflection.
- You want an **in-memory test client** that drives the ASGI surface without
  sockets.
- You are building an **application**, not a framework, and you prefer a single
  coherent stack over assembling parts.

## Code comparison

### A route with dependency injection

=== "Veloce"

    ```python
    from veloce import Depends, Veloce

    app = Veloce()

    def get_db():
        db = connect()
        try:
            yield db
        finally:
            db.close()

    @app.get("/items")
    async def items(db=Depends(get_db)):
        return db.fetch_all("SELECT * FROM items")
    ```

=== "Starlette"

    ```python
    # Starlette has no built-in DI — you manage it manually.
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    def connect():
        ...

    async def items(request: Request):
        db = connect()
        try:
            rows = db.fetch_all("SELECT * FROM items")
            return JSONResponse(rows)
        finally:
            db.close()

    app = Starlette(routes=[Route("/items", items)])
    ```

### OpenAPI documentation

=== "Veloce"

    ```python
    from veloce import Veloce

    app = Veloce(title="My API", version="1.0.0")

    @app.get("/hello")
    async def hello() -> dict:
        return {"message": "world"}

    # Swagger UI at /docs, ReDoc at /redoc — zero configuration.
    ```

=== "Starlette"

    ```python
    # Starlette does not generate OpenAPI schemas.
    # Use a separate library or build the schema manually.
    ```

## See also

- [Comparison matrix](../comparison.md) — Veloce vs FastAPI, Flask, Starlette, and Django
- [Migrating from FastAPI](migrating-from-fastapi.md) — line-by-line divergence map
- [Why Veloce exists](why-veloce-exists.md) — design motivations
