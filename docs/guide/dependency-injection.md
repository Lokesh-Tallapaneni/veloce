---
description: FastAPI-style Depends() and Security() in Veloce — typed dependency injection with yield teardown, scopes, security schemes, and per-route or app-level deps.
tags: [dependency-injection, depends, security]
---

# Dependency Injection

Dependency injection lets a handler declare what it needs — a database
handle, the current user, a parsed setting — and have Veloce provide it.
Dependencies are resolved from a plan compiled once at registration, so
there is no per-request reflection.

## Declaring a dependency

A dependency is any callable. Wrap it in `Depends()` as a parameter
default:

```python
from veloce import Depends, Request, Veloce

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
async def get_current_user(request: Request):
    token = request.headers.get("authorization", "")
    if not token:
        raise HTTPException(401, "Not authenticated")
    return {"id": 1, "name": "admin"}


@app.get("/me")
async def me(user=Depends(get_current_user)):
    return user
```

## Offloading blocking dependencies

A sync dependency runs inline on the event loop by default, which is ideal for
trivial pure functions. When a sync dependency does **blocking** work — a
synchronous database driver call, `requests.get`, a slow file read — running it
inline stalls every other request handled by the same worker. Pass
`offload=True` to route that one dependency through the thread pool instead:

```python
import requests


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

## Route-level dependencies

When a dependency is needed for its **side effect** (an auth check, say)
and its return value is not used, attach it to the route with
`dependencies=`:

```python
@app.get("/admin", dependencies=[Depends(get_current_user)])
async def admin_panel(request: Request):
    return {"area": "admin"}
```

The same `dependencies=` argument works on a `Router`, applying to every
route it contains, and on `Veloce(...)` for app-wide dependencies.

## `yield` dependencies with teardown

A dependency that `yield`s its value behaves like a context manager
scoped to the request: the code before `yield` is setup, the code after
runs once the response has been produced — even if the handler raised.

```python
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

Multiple `yield` dependencies tear down in reverse order. Every teardown
runs even if an earlier one raises; the failures are then re-raised
together as a `BaseExceptionGroup` (chained from the request exception
when the request itself failed) so a broken teardown — a transaction that
fails to commit or roll back, say — is observable rather than silently
swallowed. The dispatcher logs the aggregated group so a failing teardown
does not break the response cycle.

## Security dependencies

`Security` is a specialised `Depends` for authentication schemes, and
`SecurityScopes` lets a dependency inspect the scopes required by the
route. Veloce ships HTTP Basic/Bearer/Digest, API-key, and OAuth2
schemes under `veloce.security`.

When the same auth callable is required with different scope sets in one
request — `Security(auth, scopes=["read"])` and `Security(auth,
scopes=["read", "write"])` — and it reads `SecurityScopes`, each scope set
resolves independently (the per-request dependency cache keys those entries
by their active scopes). Plain `Depends`, and any `Security` dependency
that does not read its scopes, still resolve once per request.

## Overriding dependencies in tests

`app.dependency_overrides` swaps a dependency for a fake — ideal for
tests that should not touch a real database or network:

```python
def fake_db() -> dict:
    return {"1": {"name": "test"}}


app.dependency_overrides[get_db] = fake_db
```

## See also

- [Requests & responses](requests-responses.md)
- [Middleware](middleware.md)
- [Testing](testing.md#overriding-dependencies)
