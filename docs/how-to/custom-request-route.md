---
description: >-
  Why Veloce has no route_class or request_class hook, and the supported way to customise
  per-request behaviour — middleware, dependencies, request.state, and response classes.
tags: [request, routing, middleware, customisation]
---

# Customising the request or route

Veloce has no per-route "route class" or swappable "request class" hook. Customisation
lives in middleware, dependencies, [`request.state`](../reference/requests.md#veloce.Request),
and the per-route response class instead. This page maps each thing people reach a
route-class hook for onto the supported Veloce mechanism.

!!! warning "There is no `route_class` or `request_class`"
    [`Veloce()`](../reference/application.md#veloce.Veloce) and `add_route` (the call behind
    `@app.get`, `@app.post`, …) take **no** `route_class`, `request_class`, or
    `router_class` argument. The [`Request`](../reference/requests.md#veloce.Request) type is fixed
    and slotted; you cannot substitute a subclass per route. Porting code that subclasses
    `APIRoute` or `Request` to inject behaviour requires rewriting it against one of the
    mechanisms below.

## Run code before and after the handler

The most common reason to subclass a route is to wrap the handler — read the body early,
time the call, mutate the response. Use [`Middleware`](../reference/middleware.md#veloce.Middleware),
whose `process_request` runs before the handler and `process_response` runs after.

```python title="app.py"
from veloce import Middleware, Request, Response, Veloce


class TimingMiddleware(Middleware):
    async def process_request(self, request: Request) -> Response | None:
        request.state.started = True
        return None

    async def process_response(self, request: Request, response: Response) -> Response:
        response.headers["X-Handled"] = "1"
        return response


app = Veloce()
app.add_middleware(TimingMiddleware)


@app.get("/")
async def index(request: Request):
    return {"started": request.state.started}


if __name__ == "__main__":
    app.run(port=8000)
```

`process_request` returning `None` falls through to the handler; returning a
[`Response`](../reference/responses.md#veloce.Response) short-circuits it. `process_response` always
runs and must return the response. Per-request data goes on
[`request.state`](../reference/requests.md#veloce.Request), never on a `request` attribute —
`Request` is slotted and rejects arbitrary attributes.

!!! note
    To handle request and response in a single coroutine, use
    [`BaseHTTPMiddleware`](../reference/middleware.md#veloce.BaseHTTPMiddleware) instead: override
    `dispatch(self, request, call_next)` and `await call_next(request)` exactly once.
    Register it with `app.add_http_middleware(...)` — passing a `BaseHTTPMiddleware`
    subclass to `add_middleware` raises `TypeError`.

## Inject per-request computed values

If a route-class subclass exists only to compute a value and hand it to the handler, that
is a dependency. [`Depends`](../reference/dependencies.md#veloce.Depends) resolves a callable once per
request and passes the result as a parameter.

```python title="app.py"
from veloce import Depends, Request, Veloce


async def request_id(request: Request) -> str:
    rid = request.headers.get("X-Request-ID", "local")
    request.state.request_id = rid
    return rid


app = Veloce()


@app.get("/whoami")
async def whoami(rid: str = Depends(request_id)):
    return {"request_id": rid}


if __name__ == "__main__":
    app.run(port=8000)
```

The dependency receives the same [`Request`](../reference/requests.md#veloce.Request) the handler
sees, so it can read headers, write to `request.state`, and short-circuit by raising
[`HTTPException`](../reference/exceptions.md#veloce.HTTPException). Attach a dependency:

- to one route with `dependencies=[Depends(...)]` on the decorator,
- to a group via a [`Blueprint`](../reference/routers.md#veloce.Blueprint),
- or globally with `Veloce(dependencies=[...])`.

## Carry per-request data

A custom request class is often just a place to stash decoded auth, a tenant, or a DB
session for the duration of the request. That is what
[`request.state`](../reference/requests.md#veloce.Request) is for. Write it in middleware or a
dependency, read it anywhere downstream.

```python title="app.py"
from veloce import Middleware, Request, Response, Veloce


class TenantMiddleware(Middleware):
    async def process_request(self, request: Request) -> Response | None:
        request.state.tenant = request.headers.get("X-Tenant", "public")
        return None


app = Veloce()
app.add_middleware(TenantMiddleware)


@app.get("/")
async def index(request: Request):
    return {"tenant": request.state.tenant}


if __name__ == "__main__":
    app.run(port=8000)
```

`request.state` supports both attribute access (`request.state.tenant`) and dict access
(`request.state["tenant"]`). It is per-request only — it does not persist across requests,
and the keys `session`, `url_rule`, and `proxy_fix_*` are framework-reserved.

!!! warning "State is per-request, not storage"
    Do not treat `request.state` as a cache or database. It is discarded when the request
    finishes. Cross-request data belongs in the session, a database, or
    [a cache](../guide/caching.md).

### Verifying the behaviour

The in-memory [`TestClient`](../reference/testing.md#veloce.TestClient) confirms the middleware and
state wiring end to end without a server.

```python
from veloce import Middleware, Request, Response, TestClient, Veloce


class TenantMiddleware(Middleware):
    async def process_request(self, request: Request) -> Response | None:
        request.state.tenant = request.headers.get("X-Tenant", "public")
        return None


app = Veloce()
app.add_middleware(TenantMiddleware)


@app.get("/")
async def index(request: Request):
    return {"tenant": request.state.tenant}


client = TestClient(app)

assert client.get("/").json() == {"tenant": "public"}
assert client.get("/", headers={"X-Tenant": "acme"}).json() == {"tenant": "acme"}
```

## Customise the response type

There is no route class to override response construction, but there is a real per-route
hook: `response_class`. Pass it on a route to choose how the return value is rendered, or
set `default_response_class` on the app to change it everywhere.

```python title="app.py"
from veloce import HTMLResponse, Veloce

app = Veloce()


@app.get("/page", response_class=HTMLResponse)
async def page():
    return "<h1>Hello</h1>"


if __name__ == "__main__":
    app.run(port=8000)
```

`response_class` accepts any [`Response`](../reference/responses.md#veloce.Response) subclass and
applies only when the handler returns a value to be coerced; returning a `Response`
instance directly bypasses it. See [Requests and responses](../guide/requests-responses.md)
for the full set of built-in response classes.

| Hook | Where | Customises |
| --- | --- | --- |
| `Middleware` / `BaseHTTPMiddleware` | `add_middleware` / `add_http_middleware` | Wrapping every request and response. |
| `Depends(...)` | route, blueprint, or `Veloce(dependencies=)` | Per-request computed values and guards. |
| `request.state` | written in middleware or a dependency | Carrying data through one request. |
| `before_request` / `after_request` | [`@app.before_request`](../reference/application.md#veloce.Veloce.before_request) | Flask-style per-request hooks. |
| `response_class` | route or `default_response_class` | How the return value is rendered. |

## Next steps

- [Middleware](../guide/middleware.md) — the full `Middleware` and `BaseHTTPMiddleware` surface.
- [Dependency injection](../guide/dependency-injection.md) — `Depends`, sub-dependencies, and overrides.
- [Requests and responses](../guide/requests-responses.md) — `request.state`, response classes, and raw access.
- [Migrating from FastAPI](../surpass/migrating-from-fastapi.md) — other behaviours that differ from FastAPI.
- Full signatures are in the [API reference](../reference/index.md).
