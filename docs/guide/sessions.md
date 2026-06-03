---
description: Add signed-cookie or server-side sessions to a Veloce app with SessionMiddleware and ServerSessionMiddleware, read and write the per-request session, and build a custom SessionStore.
tags: [sessions, cookies, security]
---

# Sessions

A session is a per-user dictionary that survives across requests. Veloce
offers two backends: [`SessionMiddleware`](../reference.md#veloce.SessionMiddleware)
keeps the whole payload in a signed, timestamped cookie, and
[`ServerSessionMiddleware`](../reference.md#veloce.ServerSessionMiddleware)
keeps only an opaque id in the cookie and stores the payload server-side.

## A first session

Install [`SessionMiddleware`](../reference.md#veloce.SessionMiddleware) with a
secret key, then read and write [`request.session`](../reference.md#veloce.Request)
like a dict:

```python
from veloce import Request, SessionMiddleware, Veloce

app = Veloce()
app.add_middleware(SessionMiddleware, secret_key="change-me-in-production")


@app.get("/count")
async def count(request: Request):
    visits = request.session.get("visits", 0) + 1
    request.session["visits"] = visits
    return {"visits": visits}
```

Each request reads its `visits` counter out of the session cookie, bumps
it, and writes it back. The middleware re-signs the cookie on the way out
only when the session was modified.

!!! warning "Keep the secret key secret"
    The cookie is signed with `secret_key`, not encrypted — clients can
    read the payload, they just cannot forge it. Never commit the key to
    source control, and never store anything you would not put in a
    response body. Load it from an environment variable or a secrets
    manager. See the [OWASP Session Management Cheat
    Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Session_Management_Cheat_Sheet.html).

## The Session object

[`request.session`](../reference.md#veloce.Request) is a
[`Session`](../reference.md#veloce.Session), a `dict` subclass that tracks its
own state. Two attributes drive the middleware:

- `session.new` — `True` when the request arrived without a valid
  session cookie.
- `session.modified` — flips to `True` the first time any mutating
  operation runs. The middleware skips the re-sign and `Set-Cookie`
  entirely when the handler never touched the session.
- `session.accessed` — flips to `True` the first time the handler
  *reads* a session value (`session["k"]`, `session.get(...)`,
  `"k" in session`, iteration). It drives the `Vary: Cookie` header
  (see [Caching and `Vary: Cookie`](#caching-and-vary-cookie)).

Every mutating dict operation is tracked, including `clear()`, `pop()`,
`setdefault()`, `update()`, and the `|=` merge:

```python
from veloce import Request, SessionMiddleware, Veloce

app = Veloce()
app.add_middleware(SessionMiddleware, secret_key="change-me-in-production")


@app.post("/login")
async def login(request: Request):
    request.session["user_id"] = 42
    return {"ok": True}


@app.post("/logout")
async def logout(request: Request):
    request.session.clear()      # empties the session
    return {"ok": True}
```

Emptying the session (so it is falsy) tells the middleware to delete the
cookie on the response.

!!! tip "Mutate in place, not the contents of nested objects"
    `Session` notices `session["key"] = value` but cannot see a mutation
    of a value you already stored — `session["items"].append(x)` does not
    flip `modified`. Re-assign the key, or set `session.modified = True`
    yourself.

## Caching and `Vary: Cookie`

A response built from session state is personalised per user, so a shared
cache (a CDN, a reverse proxy) must not serve one user's body to another.
When a handler reads or writes the session, the middleware adds
`Vary: Cookie` to the response (merging with any existing `Vary` value),
which tells caches that the response varies by the request `Cookie` header.
A handler that never touches the session gets no extra `Vary`.

Occasionally a handler reads the session but returns a body that is *not*
personalised by it — a static asset reached through the session middleware,
say. Call `suppress_session_vary(request)` to skip the automatic header for
that one response so a shared cache may still store it:

```python
from veloce import Request, suppress_session_vary


@app.get("/public-asset")
async def public_asset(request: Request):
    if request.session.get("user_id"):
        ...  # body does not depend on the session
    suppress_session_vary(request)
    return {"public": True}
```

Use it sparingly — varying on `Cookie` is the safe default.

## Permanent sessions

By default the cookie lives for `max_age` seconds (14 days). Set
`session.permanent = True` to switch it to the longer
`permanent_lifetime` (31 days by default):

```python
from veloce import Request, SessionMiddleware, Veloce

app = Veloce()
app.add_middleware(
    SessionMiddleware,
    secret_key="change-me-in-production",
    max_age=3600,                  # 1 hour for normal sessions
    permanent_lifetime=86400 * 30, # 30 days for "remember me"
)


@app.post("/remember")
async def remember(request: Request):
    request.session.permanent = True
    request.session["user_id"] = 42
    return {"remembered": True}
```

The `permanent` flag is stored under a reserved `_permanent` key, so it
persists in the cookie across requests.

## SessionMiddleware options

[`SessionMiddleware`](../reference.md#veloce.SessionMiddleware) accepts these
keyword arguments:

| Argument             | Default        | Meaning                                                        |
|----------------------|----------------|----------------------------------------------------------------|
| `secret_key`         | (required)     | A string, or a list of strings for rotation (first one signs). |
| `cookie_name`        | `"session"`    | Name of the session cookie.                                    |
| `max_age`            | `86400 * 14`   | Cookie lifetime in seconds for normal sessions.               |
| `path`               | `"/"`          | Cookie `Path` attribute.                                       |
| `httponly`           | `True`         | Set the `HttpOnly` attribute.                                 |
| `secure`             | `False`        | Set the `Secure` attribute (HTTPS-only cookie).               |
| `samesite`           | `"lax"`        | `SameSite` attribute — `"lax"`, `"strict"`, or `"none"`.       |
| `permanent_lifetime` | `86400 * 31`   | Cookie lifetime when `session.permanent` is set.              |
| `max_cookie_size`    | `4093`         | Largest rendered `Set-Cookie` before the cookie is dropped.   |

!!! warning "Set secure=True behind HTTPS"
    `secure=False` is the development default. In production, serve over
    HTTPS and pass `secure=True` so the session cookie is never sent over
    plain HTTP. The `httponly` and `samesite` defaults follow [RFC
    6265](https://www.rfc-editor.org/rfc/rfc6265) cookie-security
    guidance — keep `httponly=True` to keep the cookie out of JavaScript.

The cookie body is signed and timestamped by
[`Signer`](../reference.md#veloce.Signer), and `max_age` is enforced
server-side on read — an attacker cannot extend a session by replaying an old
cookie.

### Secret rotation

Pass a list of secrets to rotate the signing key without invalidating
live sessions. The first key signs new cookies; the rest are accepted on
read until existing cookies age out:

```python
from veloce import SessionMiddleware, Veloce

app = Veloce()
app.add_middleware(
    SessionMiddleware,
    secret_key=["new-key", "previous-key"],
)
```

!!! note "Cookie size limit"
    Because the whole session lives in the cookie, a large payload can
    exceed `max_cookie_size` (4093 bytes). When the rendered
    `Set-Cookie` is too large, `SessionMiddleware` logs a warning and
    drops the `Set-Cookie` rather than corrupting the session. For large
    payloads, switch to `ServerSessionMiddleware`.

## Server-side sessions

[`ServerSessionMiddleware`](../reference.md#veloce.ServerSessionMiddleware)
stores the payload in a [`SessionStore`](../reference.md#veloce.SessionStore)
and puts only an opaque, unguessable id in the cookie. This keeps the cookie
small and, crucially, makes sessions *revocable* — emptying the session or
deleting its id from the store destroys it server-side immediately.

The default store is a process-local
[`InMemorySessionStore`](../reference.md#veloce.InMemorySessionStore), which is
fine for a single process and for tests:

```python
from veloce import Request, ServerSessionMiddleware, Veloce

app = Veloce()
app.add_middleware(ServerSessionMiddleware)


@app.post("/login")
async def login(request: Request):
    request.session["user_id"] = 42
    return {"ok": True}
```

The same [`Session`](../reference.md#veloce.Session) API applies — read and
write `request.session`, and the middleware persists changes to the store.

!!! warning "InMemorySessionStore does not scale across workers"
    `InMemorySessionStore` keeps state in one process. A multi-worker or
    multi-host deployment needs a shared backend (Redis, a database)
    implementing the [`SessionStore`](../reference.md#veloce.SessionStore)
    interface, or each worker will see a different set of sessions.

`ServerSessionMiddleware` takes the same cookie options as
`SessionMiddleware` (`cookie_name`, `max_age`, `path`, `httponly`,
`secure`, `samesite`) plus a `store` argument. It has no `secret_key`
(the cookie carries no signed payload to protect) and no
`permanent_lifetime`.

### Revoking a session

Keep a reference to the store to revoke any session by id, and rotate the
id at privilege boundaries with `session.regenerate_id()`:

```python
from veloce import InMemorySessionStore, Request, ServerSessionMiddleware, Veloce

app = Veloce()
store = InMemorySessionStore()
app.add_middleware(ServerSessionMiddleware, store=store)


@app.post("/login")
async def login(request: Request):
    # Rotate the session id on login to defeat session fixation.
    request.session.regenerate_id()
    request.session["user_id"] = 42
    return {"ok": True}
```

Calling `session.regenerate_id()` mints a fresh server-side id on the
next response and drops the old store entry, so a pre-existing
(possibly attacker-planted) id can no longer be replayed against the
elevated session. This is the [session-fixation
defence](https://owasp.org/www-community/attacks/Session_fixation).

## Writing a custom SessionStore

To back sessions with Redis or a database, subclass
[`SessionStore`](../reference.md#veloce.SessionStore) and implement its async
methods. The interface is async so a network-backed store does not block the
event loop:

```python
from typing import Any

from veloce import ServerSessionMiddleware, SessionStore, Veloce


class RedisSessionStore(SessionStore):
    def __init__(self, client: Any) -> None:
        self._client = client

    async def read(self, session_id: str) -> dict[str, Any] | None:
        raw = await self._client.get(f"session:{session_id}")
        if raw is None:
            return None
        import orjson

        return orjson.loads(raw)

    async def write(self, session_id: str, data: dict[str, Any], max_age: int) -> None:
        import orjson

        await self._client.set(f"session:{session_id}", orjson.dumps(data), ex=max_age)

    async def delete(self, session_id: str) -> None:
        await self._client.delete(f"session:{session_id}")


app = Veloce()
# app.add_middleware(ServerSessionMiddleware, store=RedisSessionStore(redis_client))
```

`read` returns the stored payload or `None` when the id is absent,
expired, or revoked. `write` persists the payload to expire after
`max_age` seconds. `delete` revokes an id so a later `read` returns
`None`.

!!! tip "Override replace for atomic writes"
    `SessionStore` also defines `replace(session_id, data, max_age)`,
    used by the middleware to write back an existing session only if it
    still exists — so a request in flight cannot resurrect a session a
    concurrent `delete` removed. The default implementation is a
    non-atomic read-then-write. A store with an atomic conditional write
    (Redis `SET ... XX`, a SQL `UPDATE`) should override `replace` to
    close the check-then-write window.

## Reading the session outside the handler

The top-level [`session`](helpers.md#session) proxy resolves to the
current request's session, so you can read it without threading the
request through every call:

```python
from veloce import SessionMiddleware, Veloce, session

app = Veloce()
app.add_middleware(SessionMiddleware, secret_key="change-me-in-production")


@app.get("/whoami")
async def whoami():
    return {"user_id": session.get("user_id")}
```

See the [Flask-style helpers](helpers.md) guide for the full set of
request-scoped proxies.

## Next steps

- [Flask-style helpers](helpers.md) — `session`, `flash`, `g`, and the
  rest of the request-scoped helpers.
- [Middleware](middleware.md) — ordering, function middleware, and the
  full middleware table.
- The [API reference](../reference.md) documents the `Signer` that backs
  the session cookie.
