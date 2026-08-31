---
description: Typed dependency injection in Veloce — Depends() and Security(), classes as dependencies, sub-dependencies with within-request caching, app-level and parameterized dependencies, and test-time overrides.
tags: [dependency-injection, depends, security, testing]
---

# Dependency injection

Dependency injection lets a handler declare what it needs — a database
handle, the current user, a parsed setting — and have Veloce provide it.
[`Depends`](../reference/dependencies.md#veloce.Depends) marks a parameter as injected;
dependencies are resolved from a plan compiled once at registration, so
there is no per-request reflection.

## Declaring a dependency

A dependency is any callable. Wrap it in `Depends()` as a parameter
default:

```python
from veloce import Depends, Veloce

app = Veloce()

_db: dict = {}


def get_db() -> dict:
    return _db


@app.get("/items")
async def list_items(db=Depends(get_db)):
    return list(db.values())
```

Dependencies may be sync or `async`, and they can themselves request the
`Request` or other dependencies — Veloce resolves the whole chain.

```python title="auth.py"
from veloce import Depends, HTTPException, Request


async def get_current_user(request: Request):
    token = request.headers.get("authorization", "")
    if not token:
        raise HTTPException(401, "Not authenticated")
    return {"id": 1, "name": "admin"}


@app.get("/me")
async def me(user=Depends(get_current_user)):
    return user
```

## Classes as dependencies

A class is a callable, so it can be a dependency directly: calling it runs
`__init__` and the instance is injected. The handler then receives a typed
object instead of a bare value, which gives editor completion on its
attributes.

```python title="app.py"
from veloce import Depends, TestClient, Veloce

app = Veloce()


class Pagination:
    def __init__(self, limit: int = 10, offset: int = 0) -> None:
        self.limit = limit
        self.offset = offset


@app.get("/items")
async def list_items(page: Pagination = Depends(Pagination)):
    return {"limit": page.limit, "offset": page.offset}


client = TestClient(app)

resp = client.get("/items?limit=5")
assert resp.status_code == 200
assert resp.json() == {"limit": 5, "offset": 0}
```

The dependency's own parameters (`limit`, `offset` above) are themselves
resolved — here as query parameters — so a class dependency is a tidy way
to group several related inputs behind one **type annotation**.

### Inferring the class from the annotation

When the dependency callable *is* the parameter's type, the annotation is
redundant. Pass `Depends()` with no argument and Veloce infers the
callable from the annotation — the shorthand for
`page: Pagination = Depends(Pagination)`:

```python
@app.get("/items")
async def list_items(page: Pagination = Depends()):
    return {"limit": page.limit, "offset": page.offset}
```

!!! note
    Inference only applies to a bare `Depends()`. With an explicit
    `Depends(callable)` the argument always wins, even when it differs from
    the annotation.

## Offloading blocking dependencies

A sync dependency runs inline on the event loop by default, which is ideal for
trivial pure functions. When a sync dependency does blocking work — a
synchronous database driver call, `requests.get`, a slow file read — running it
inline stalls every other request handled by the same worker. Pass
`offload=True` to route that one dependency through the thread pool instead:

```python
import requests

from veloce import Depends


def fetch_profile() -> dict:
    # Blocking network call - runs in the thread pool, off the event loop.
    return requests.get("https://example.com/profile").json()


@app.get("/me")
async def me(profile: dict = Depends(fetch_profile, offload=True)):
    return profile
```

Request-scoped state (`request`, `g`, `flash()`) stays readable inside the
offloaded call. The flag is ignored for coroutine, `yield`, and async-generator
dependencies, which already have their own execution model. `Security` accepts
the same `offload=True` argument.

!!! note "Dependencies differ from route handlers here"

    A sync **route handler** is always offloaded to the thread pool — you never
    set a flag. A sync **dependency** runs inline unless you pass `offload=True`.
    The asymmetry is deliberate: a request has one handler but often many
    dependencies, and most dependencies are tiny pure helpers, so offloading
    every one would add a thread hop per dependency for no benefit. Opt in the
    rare dependency that actually blocks.

## Sub-dependencies and caching

A dependency may itself declare dependencies; Veloce resolves the whole
graph before calling the handler. A sub-dependency requested more than
once in a single request runs **once** — its result is cached for the
duration of that request and reused at every site that asks for it.

```python title="app.py"
from veloce import Depends, TestClient, Veloce

app = Veloce()

_CALLS = {"count": 0}


def get_settings() -> dict:
    _CALLS["count"] += 1
    return {"region": "eu"}


def get_region(settings: dict = Depends(get_settings)) -> str:
    return settings["region"]


@app.get("/where")
async def where(
    settings: dict = Depends(get_settings),
    region: str = Depends(get_region),
):
    # `get_settings` is referenced twice but runs once per request.
    return {"region": region, "calls": _CALLS["count"]}


client = TestClient(app)

resp = client.get("/where")
assert resp.json() == {"region": "eu", "calls": 1}  # ran once
```

Caching is keyed on the dependency callable, so the same function reached
through different paths shares one result.

### Parameters declared on a dependency

A dependency may read the wire directly. `Query()`, `Header()`, `Cookie()` and
friends work inside a dependency exactly as they do on a handler, and a missing
required one fails the request with a `422` before the handler runs.

Such a parameter belongs to the route's public contract, so it is published in
the OpenAPI document and in the MCP tool schema alongside the handler's own
parameters — a client generated from the schema sends everything the route
actually requires.

```python title="app.py"
from veloce import Depends, Query, TestClient, Veloce

app = Veloce(title="Catalogue", version="1.0.0")


class Page:
    def __init__(self, number: int, size: int) -> None:
        self.number = number
        self.size = size


def paginate(page: int = Query(default=1, ge=1), size: int = Query(default=20, le=100)) -> Page:
    return Page(page, size)


@app.get("/items")
async def list_items(q: str = "", page: Page = Depends(paginate)):
    return {"q": q, "page": page.number, "size": page.size}


client = TestClient(app)

assert client.get("/items", params={"page": "3"}).json()["page"] == 3
assert client.get("/items", params={"page": "0"}).status_code == 422

published = {p["name"] for p in app.openapi()["paths"]["/items"]["get"]["parameters"]}
assert published == {"q", "page", "size"}
```

Two rules keep the published list honest:

- A parameter the handler declares itself **shadows** a sub-dependency parameter
  of the same name, whatever order they are declared in.
- A dependency reached twice — injected at two sites, or shared by two other
  dependencies — contributes its parameters **once**.

!!! note "Changed in version 0.13"
    Parameters declared on a dependency are published in the OpenAPI document.
    Earlier versions enforced them at runtime but omitted them from the schema.

### Opting out with `use_cache=False`

When a dependency must run freshly at every reference — a fresh token, a
new timestamp, a per-call connection — set `use_cache=False`. That site
re-runs the callable and is excluded from the shared cache.

```python
import time

from veloce import Depends


def now() -> float:
    return time.time()


@app.get("/tick")
async def tick(
    a: float = Depends(now, use_cache=False),
    b: float = Depends(now, use_cache=False),
):
    # Two distinct readings rather than one cached value.
    return {"a": a, "b": b}
```

!!! note
    `use_cache` is per-reference, not per-callable. One `Depends(now)` may
    cache while another `Depends(now, use_cache=False)` in the same request
    re-runs.

## Route-level dependencies

When a dependency is needed for its side effect (an auth check, say)
and its return value is not used, attach it to the route with
`dependencies=`:

```python
from veloce import Depends, Request


@app.get("/admin", dependencies=[Depends(get_current_user)])
async def admin_panel(request: Request):
    return {"area": "admin"}
```

## App-level and router-level dependencies

The same `dependencies=` argument exists on `Veloce(...)`, on a
[`Router`](../reference/routers.md#veloce.Router), and on the individual route, so
a dependency can apply at three widening scopes. App-level dependencies run
on every request, router-level on every route the router carries, and
route-level on that one route. They run outermost-first, with the route's
own dependencies last.

```python title="app.py"
from veloce import Depends, Router, Veloce


def app_dep() -> None:
    ...  # runs for every request


def router_dep() -> None:
    ...  # runs for every route on this router


def route_dep() -> None:
    ...  # runs for this one route


app = Veloce(dependencies=[Depends(app_dep)])

api = Router(prefix="/api", dependencies=[Depends(router_dep)])


@api.get("/ping", dependencies=[Depends(route_dep)])
async def ping():
    return {"ok": True}


app.include_router(api)
```

A router's dependencies are *appended* to — not replaced by — a route's
own `dependencies=`, so both fire and the route-specific ones run last.

## Parameterized dependencies

To configure a dependency at registration time, make the dependency a
**callable instance**: an object with `__call__`. Construct it with the
configuration you want and pass the instance to `Depends()`. Veloce
inspects `__call__` for its sub-dependencies, so the instance can still
receive the `Request` and other injected values.

```python title="app.py"
from veloce import Depends, HTTPException, Request, TestClient, Veloce

app = Veloce()


class RequireHeader:
    def __init__(self, name: str) -> None:
        self.name = name

    def __call__(self, request: Request) -> str:
        value = request.headers.get(self.name)
        if value is None:
            raise HTTPException(400, f"Missing header: {self.name}")
        return value


require_tenant = RequireHeader("x-tenant")


@app.get("/tenant")
async def tenant(value: str = Depends(require_tenant)):
    return {"tenant": value}


client = TestClient(app)

resp = client.get("/tenant", headers={"x-tenant": "acme"})
assert resp.status_code == 200
assert resp.json() == {"tenant": "acme"}
```

Each configured instance is a distinct dependency: `RequireHeader("x-a")`
and `RequireHeader("x-b")` cache independently because the cache keys on
the instance, not the class.

## `yield` dependencies with teardown

A dependency that `yield`s its value behaves like a context manager
scoped to the request: the code before `yield` is setup, the code after
runs *after* the response has been produced — even if the handler raised.

```python
from veloce import Depends


def db_session():
    session = open_session()
    try:
        yield session
    finally:
        session.close()


@app.get("/report")
async def report(session=Depends(db_session)):
    return session.query(...)
```

Multiple `yield` dependencies tear down in reverse order.

Every teardown runs even if an earlier one raises; the failures are then
re-raised together as a `BaseExceptionGroup` (chained from the request
exception when the request itself failed) so a broken teardown — a
transaction that fails to commit or roll back, say — is observable rather
than silently swallowed.

Teardown runs after the response has been built, so by default a teardown
failure does not change what the client receives: the dispatcher logs the
aggregated group and delivers it to
[`got_request_exception`](signals.md) receivers, so an error
tracker subscribed to that signal sees the failure
even though the request returned its normal status. Under
`PROPAGATE_EXCEPTIONS` (or the implicit `DEBUG` + `TESTING` test mode) the
failure re-raises out of dispatch instead, so a test suite fails on a broken
teardown rather than passing on the already-built response.

!!! warning "A 200 response does not prove the teardown ran clean"
    Work done after `yield` — a transaction commit, a connection release —
    happens after the response status is fixed. Monitor
    `got_request_exception` (or run tests with `PROPAGATE_EXCEPTIONS`) to
    catch teardown failures.

!!! note
    The aggregated `BaseExceptionGroup` is raised on Python 3.11+ (PEP 654);
    on 3.10, which has no exception groups, the first failure is re-raised
    chained from the request error instead.

## Security dependencies

[`Security`](../reference/dependencies.md#veloce.Security) is a specialised `Depends`
for authentication schemes, and
[`SecurityScopes`](../reference/dependencies.md#veloce.SecurityScopes) lets a dependency
inspect the scopes required by the route. Veloce ships HTTP Basic/Bearer/Digest,
API-key, and OAuth2 schemes under `veloce.security` — see
[Security schemes](security-schemes.md).

When the same auth callable is required with different scope sets in one
request — `Security(auth, scopes=["read"])` and `Security(auth,
scopes=["read", "write"])` — and it reads `SecurityScopes`, each scope set
resolves independently (the per-request dependency cache keys those entries
by their active scopes). Plain `Depends`, and any `Security` dependency
that does not read its scopes, still resolve once per request.

## Overriding dependencies in tests

[`app.dependency_overrides`](../reference/application.md#veloce.Veloce.dependency_overrides)
is a mutable map from a dependency callable to its replacement. The
resolver consults it on every request, so a test can swap a real
dependency for a fake without touching a database or network:

```python title="test_items.py"
from veloce import Depends, TestClient, Veloce

app = Veloce()

_db = {"1": {"name": "real"}}


def get_db() -> dict:
    return _db


@app.get("/items")
async def list_items(db: dict = Depends(get_db)):
    return list(db.values())


def fake_db() -> dict:
    return {"1": {"name": "test"}}


app.dependency_overrides[get_db] = fake_db

client = TestClient(app)

resp = client.get("/items")
assert resp.status_code == 200
assert resp.json() == [{"name": "test"}]
```

Override by the *original* callable — the key is the dependency you wrote,
not the replacement.

!!! warning "Reset overrides after each test"
    `dependency_overrides` persists on the application instance for the
    process lifetime, so an override left in place leaks into later tests
    that share the same `app`. Remove a single entry with
    `del app.dependency_overrides[get_db]`, or clear them all with
    `app.dependency_overrides.clear()`, in test teardown. Assigning a fresh
    dict (`app.dependency_overrides = {}`) also resets them and drops the
    cached override sub-plans.

## Next steps

- Build authentication on top of `Security` — see [Security schemes](security-schemes.md).
- Read injected values from the request — see [Requests and responses](requests-responses.md).
- Override dependencies as part of a wider test suite — see [Testing](testing.md#overriding-dependencies).
- Full signatures are in the [API reference](../reference/index.md).
