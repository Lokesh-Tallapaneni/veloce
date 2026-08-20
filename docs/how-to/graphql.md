---
description: >-
  Serve a GraphQL endpoint in Veloce by mounting a GraphQL ASGI app at a prefix with app.mount — the
  sub-application pattern.
tags: [graphql, mount, asgi, sub-applications]
---

# GraphQL

Veloce has no built-in GraphQL layer, and it does not need one. A GraphQL library that exposes an
ASGI application — Strawberry, Ariadne, graphql-core via its ASGI wrapper — mounts straight onto a
Veloce app with [`app.mount`](../reference/application.md#veloce.Veloce.mount).

The GraphQL app owns its prefix subtree and runs at the ASGI layer; your REST routes keep running
through the normal pipeline.

This page covers the mount integration; the GraphQL-library specifics are intentionally minimal.

## Mount a GraphQL ASGI app at a prefix

`app.mount(prefix, app)` attaches any ASGI application at a path prefix. The matched prefix is
stripped from the request `path` and moved onto `root_path`, so the mounted app sees a normal
root-relative request. This example uses [Strawberry](https://strawberry.rocks), whose
`GraphQLRouter` is an ASGI app.

```bash
pip install veloceframework strawberry-graphql
```

```python title="app.py"
import strawberry
from strawberry.asgi import GraphQL

from veloce import Request, Veloce


@strawberry.type
class Query:
    @strawberry.field
    def hello(self) -> str:
        return "Hello from GraphQL"


schema = strawberry.Schema(Query)
graphql_app = GraphQL(schema)

app = Veloce()
app.mount("/graphql", graphql_app)


@app.get("/")
async def index(request: Request):
    return {"app": "main"}


if __name__ == "__main__":
    app.run(port=8000)
```

Requests under `/graphql` are dispatched into the Strawberry app; everything else falls through to
the Veloce router. The mounted app receives the prefix on `root_path`, so Strawberry's own routing
(the GraphQL POST endpoint and its GraphiQL explorer at `GET /graphql`) works as if it were mounted
at the root.

!!! warning "An ASGI mount owns its whole prefix subtree"
    A mounted ASGI app claims **every** path under its prefix. A native Veloce route registered at
    the same prefix — `@app.get("/graphql/health")` — is unreachable, because the mount matches
    first. Register such routes under a different prefix.

## Serve the tree under an ASGI server

ASGI mounts are dispatched in Veloce's ASGI entry point, so the tree must be served by an ASGI
server. The in-memory [`TestClient`](../reference/testing.md#veloce.TestClient) drives that same entry point,
which is why the assertions below work in-process.

```bash
pip install veloceframework[uvicorn]
uvicorn app:app --port 8000
```

!!! warning "ASGI mounts do not run on the native transport"
    The native [`app.run()`](../reference/application.md#veloce.Veloce.run) transport dispatches Veloce sub-apps
    and static handlers, but does not route into arbitrary ASGI mounts. The `app.run()` block above
    starts the server and serves your native routes; to expose the mounted GraphQL endpoint, run the
    app under an ASGI server (`uvicorn`, `hypercorn`) instead.

## The mounted app gets no lifespan

The parent fans its startup and shutdown out to mounted *Veloce* sub-apps only. A non-Veloce ASGI
mount — which a GraphQL app is — is never sent the ASGI `lifespan` events, so it must not rely on
`lifespan.startup` / `lifespan.shutdown` for its setup or teardown.

If your GraphQL layer needs a resource (a database pool, a client), acquire it from the **parent's**
lifecycle and inject it through the GraphQL context, rather than depending on the mount's own
lifespan.

```python title="app.py"
import strawberry
from strawberry.asgi import GraphQL

from veloce import Veloce


@strawberry.type
class Query:
    @strawberry.field
    def ping(self) -> str:
        return "pong"


schema = strawberry.Schema(Query)
graphql_app = GraphQL(schema)

app = Veloce()
app.mount("/graphql", graphql_app)


@app.on_startup
async def open_pool():
    # The parent owns the resource; the GraphQL layer reads it from app.state.
    app.state.pool = object()


@app.on_shutdown
async def close_pool():
    app.state.pool = None


if __name__ == "__main__":
    app.run(port=8000)
```

The parent's `on_startup` / `on_shutdown` hooks run on the parent's own lifecycle, so the resource is
ready before any GraphQL request and released on exit — without the mount needing lifespan events.

!!! note
    A mounted ASGI app receives `http` and `websocket` scopes only. GraphQL subscriptions over
    WebSockets work through the mount, but the mount still gets no `lifespan` cycle.

## Verifying the mount

The in-memory `TestClient` constructs ASGI scopes directly, so it exercises the ASGI-mount path
without a real server. A GraphQL query is a `POST` of a JSON body to the mounted prefix.

```python
import strawberry
from strawberry.asgi import GraphQL

from veloce import TestClient, Veloce


@strawberry.type
class Query:
    @strawberry.field
    def hello(self) -> str:
        return "Hello from GraphQL"


schema = strawberry.Schema(Query)
graphql_app = GraphQL(schema)

app = Veloce()
app.mount("/graphql", graphql_app)


@app.get("/")
async def index(request):
    return {"app": "main"}


client = TestClient(app)

# Native Veloce route, dispatched through the normal pipeline.
assert client.get("/").json() == {"app": "main"}

# GraphQL query, dispatched into the mounted ASGI app.
result = client.post("/graphql", json={"query": "{ hello }"})
assert result.status_code == 200
assert result.json() == {"data": {"hello": "Hello from GraphQL"}}
```

The two requests prove the split: `/` runs through Veloce's router, `/graphql` runs through the
mounted Strawberry app, and neither interferes with the other.

## Next steps

- [Sub-applications and mounts](../guide/sub-applications.md) — the full `app.mount` contract: ASGI vs Veloce mounts, prefix-overlap rules, and WSGI mounting.
- [Lifespan and events](../guide/lifespan.md) — `on_startup` / `on_shutdown` and the `lifespan=` context manager that own parent-side resources.
- [Behind a proxy](../guide/behind-a-proxy.md) — how `root_path` and a stripped prefix interact with mounted applications.
- Full signatures are in the [API reference](../reference/index.md).
