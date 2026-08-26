---
description: Add signed-cookie or server-side sessions to a Veloce app with SessionMiddleware and ServerSessionMiddleware, read and write the per-request session, and build a custom SessionStore.
tags: [sessions, cookies, security]
---

# Sessions

A session is a per-user dictionary that survives across requests. Veloce
offers two backends: [`SessionMiddleware`](../reference/middleware.md#veloce.SessionMiddleware)
keeps the whole payload in a signed, timestamped cookie, and
[`ServerSessionMiddleware`](../reference/middleware.md#veloce.ServerSessionMiddleware)
keeps only an opaque id in the cookie and stores the payload server-side.

## A first session

Install [`SessionMiddleware`](../reference/middleware.md#veloce.SessionMiddleware) with a
secret key, then read and write [`request.session`](../reference/requests.md#veloce.Request)
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

[`request.session`](../reference/requests.md#veloce.Request) is a
[`Session`](../reference/sessions.md#veloce.Session), a `dict` subclass that tracks its
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

```python
app.add_middleware(
    SessionMiddleware,
    secret_key="change-me-in-production",
    vary_on_cookie=False,
)
```

A response built from session state is personalised per user, so a shared
cache (a CDN, a reverse proxy) must not serve one user's body to another.
When a handler reads or writes the session, the middleware adds
`Vary: Cookie` to the response (merging with any existing `Vary` value),
which tells caches that the response varies by the request `Cookie` header.
A handler that never touches the session gets no extra `Vary`.

Varying on `Cookie` is the safe default. If a deployment never serves
session-bearing responses from a shared cache — or manages cache-safety another
way — construct the middleware with `vary_on_cookie=False` to turn the automatic
header off for every response it handles. This is an app-wide switch, not a
per-response one; leave it on unless a shared cache is genuinely not in play.

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

## Sliding expiry (idle timeout)

```python
from veloce import Request, SessionMiddleware, Veloce

app = Veloce()
app.add_middleware(
    SessionMiddleware,
    secret_key="change-me-in-production",
    max_age=1800,            # expire after 30 minutes of inactivity
    renew_on_access=True,    # ...measured from the last access, not the last write
)
```

By default a session is only re-written when a handler **modifies** it, so a
read-only request never moves the expiry forward and the session ages out at a
fixed `max_age` from its last write.

Pass `renew_on_access=True` to switch to a sliding idle-timeout: any request
that **reads** the session (via `request.session`) refreshes its expiry on the
way out, so an active user is kept logged in and only an idle gap longer than
`max_age` expires the session.

With the cookie middleware this re-signs the cookie (new server-side timestamp
and `Max-Age`); with
[`ServerSessionMiddleware`](../reference/middleware.md#veloce.ServerSessionMiddleware) it
refreshes the store entry's TTL (through `SessionStore.touch`) and re-stamps the
cookie. The default is `False`, preserving the write-only behavior.

## SessionMiddleware options

[`SessionMiddleware`](../reference/middleware.md#veloce.SessionMiddleware) accepts these
keyword arguments:

| Argument             | Default        | Meaning                                                        |
|----------------------|----------------|----------------------------------------------------------------|
| `secret_key`         | `SECRET_KEY` config | A string, or a list of strings for rotation (first one signs). |
| `cookie_name`        | `"session"`    | Name of the session cookie.                                    |
| `max_age`            | `86400 * 14`   | Cookie lifetime in seconds for normal sessions.               |
| `path`               | `"/"`          | Cookie `Path` attribute.                                       |
| `httponly`           | `True`         | Set the `HttpOnly` attribute.                                 |
| `secure`             | `False`        | Set the `Secure` attribute (HTTPS-only cookie).               |
| `samesite`           | `"lax"`        | `SameSite` attribute — `"lax"`, `"strict"`, or `"none"`.       |
| `domain`             | `None`         | Cookie `Domain` attribute (scope the cookie to a host/subdomains). |
| `cookie_prefix`      | `None`         | `"host"` or `"secure"` — add the `__Host-`/`__Secure-` name prefix. |
| `partitioned`        | `False`        | Set the `Partitioned` (CHIPS) attribute for partitioned storage. |
| `permanent_lifetime` | `86400 * 31`   | Cookie lifetime when `session.permanent` is set.              |
| `max_cookie_size`    | `4093`         | Largest rendered `Set-Cookie` before the cookie is dropped (or chunked). |
| `renew_on_access`    | `False`        | Slide the expiry forward on a read-only access (idle timeout). |
| `chunked`            | `False`        | Split an oversized signed value across numbered cookies and reassemble it. |
| `max_chunks`         | `8`            | Upper bound on chunk cookies; larger sessions are dropped with a warning. |

The constructor is the only source for these. An argument left out takes the
default shown above and does not change again — `app.config` is not consulted,
so what you read here is what the cookie carries, and two session middlewares
can carry different cookies.

`secret_key` is the exception: left out, it is taken from `SECRET_KEY` (also
settable as `app.secret_key`) on the first request. It is the application's
signing key rather than an attribute of this cookie, and `app.secret_key` is
already its only home.

!!! warning "Cookie settings moved out of config"

    `SESSION_COOKIE_SECURE`, `SESSION_COOKIE_NAME`, `SESSION_COOKIE_HTTPONLY`
    and `SESSION_COOKIE_SAMESITE` no longer configure anything. Setting one
    stops the app at startup with an `AuditFailed` naming it, rather than
    letting a cookie you believe is `Secure` quietly travel over plain HTTP.
    Pass `secure=True` to the middleware instead.

So the shortest complete setup is:

```python
app = Veloce()
app.secret_key = "change-me-in-production"
app.add_middleware(SessionMiddleware)
```

Without either a `secret_key=` argument or a configured `SECRET_KEY`, **startup**
fails with `AuditFailed` — before any request is served, so the misconfiguration
cannot reach production. `AuditFailed` subclasses `ValueError`, not
`RuntimeError`.

`cookie_prefix` enforces the [RFC 6265bis](https://datatracker.ietf.org/doc/html/draft-ietf-httpbis-rfc6265bis)
name-prefix invariants: both prefixes require `secure=True`, and `"host"`
additionally requires `path="/"` and `domain=None`. `partitioned=True`
(CHIPS) requires `secure=True` and `samesite="none"`. A violation raises
`ValueError` at construction. `ServerSessionMiddleware` accepts the same
`domain`, `cookie_prefix`, and `partitioned` arguments, and resolves its
own omitted cookie settings from the same config keys.

!!! warning "Set secure=True behind HTTPS"
    `secure=False` is the development default. In production, serve over
    HTTPS and pass `secure=True` so the session cookie is never sent over
    plain HTTP. The `httponly` and `samesite` defaults follow [RFC
    6265](https://www.rfc-editor.org/rfc/rfc6265) cookie-security
    guidance — keep `httponly=True` to keep the cookie out of JavaScript.

The cookie body is signed and timestamped by
[`Signer`](../reference/security.md#veloce.Signer), and `max_age` is enforced
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
    drops the `Set-Cookie` rather than corrupting the session. Pass
    `chunked=True` to instead split the signed value across numbered
    cookies (`session.0`, `session.1`, ...) and reassemble them on the
    next request; `max_chunks` (default 8) bounds the split, so a session
    that needs more chunks is still dropped with a warning. Shrinking or
    deleting a session clears its stale chunk cookies. For large payloads,
    `ServerSessionMiddleware` is usually the better choice — it keeps the
    cookie small and makes sessions revocable.

## Server-side sessions

[`ServerSessionMiddleware`](../reference/middleware.md#veloce.ServerSessionMiddleware)
stores the payload in a [`SessionStore`](../reference/sessions.md#veloce.SessionStore)
and puts only an opaque, unguessable id in the cookie. This keeps the cookie
small and, crucially, makes sessions *revocable* — emptying the session or
deleting its id from the store destroys it server-side immediately.

The default store is a process-local
[`InMemorySessionStore`](../reference/sessions.md#veloce.InMemorySessionStore), which is
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

The same [`Session`](../reference/sessions.md#veloce.Session) API applies — read and
write `request.session`, and the middleware persists changes to the store.

!!! warning "InMemorySessionStore does not scale across workers"
    `InMemorySessionStore` keeps state in one process. A multi-worker or
    multi-host deployment needs a shared backend (Redis, a database)
    implementing the [`SessionStore`](../reference/sessions.md#veloce.SessionStore)
    interface, or each worker will see a different set of sessions.

For multi-worker deployments, the batteries-included
[`RedisSessionStore`](databases.md#redis-sessions-and-rate-limiting) shares
session state across every worker and host. It uses native Redis TTLs for
expiry, sliding renewal, and the race-safe conditional write:

```python
from redis.asyncio import Redis

from veloce import ServerSessionMiddleware, Veloce
from veloce.contrib.redis import RedisSessionStore

app = Veloce()
store = RedisSessionStore(Redis.from_url("redis://localhost:6379/0"))
app.add_middleware(ServerSessionMiddleware, store=store)
```

Install the backend with `pip install veloceframework[redis]`.

`ServerSessionMiddleware` takes the same cookie options as
`SessionMiddleware` (`cookie_name`, `max_age`, `path`, `httponly`,
`secure`, `samesite`, `permanent_lifetime`) plus a `store` argument. It has no
`secret_key` — the cookie carries no signed payload to protect.

`session.permanent = True` works the same way it does on the cookie backend: the
cookie's `Max-Age` **and** the store entry's TTL both switch to
`permanent_lifetime` (31 days by default), so the
entry never expires out from under a cookie the client still holds.

!!! warning "Changed in version 0.18"
    `ServerSessionMiddleware` previously ignored `session.permanent` and used
    `max_age` for every session, so "remember me" silently did nothing and users
    were logged out after 14 days however the lifetime was configured.
    Permanent sessions now live longer than before — pass `permanent_lifetime=`
    to cap it explicitly if that is not what you want.

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
[`SessionStore`](../reference/sessions.md#veloce.SessionStore) and implement its async
methods. The interface is async so a network-backed store does not block the
event loop.

`read`, `write` and `delete` are all required, and a store that omits one is
refused where it is written rather than on the request that first needs it:

```python
class MyStore(SessionStore):
    async def read(self, session_id): ...
    async def write(self, session_id, data, max_age): ...
    # `delete` forgotten

# TypeError: MyStore does not implement SessionStore: delete missing
```

!!! note "Added in version 0.18.0"
    The subclass check. A store that already implements all three is unaffected.

```python
from typing import Any

from veloce import ServerSessionMiddleware, SessionStore, Veloce


class RedisSessionStore(SessionStore):
    # `SessionStore` is slotted, so declare the store's own attributes rather
    # than letting the subclass fall back to a `__dict__`. The shipped
    # `veloce.contrib.redis` backends do the same.
    __slots__ = ("_client",)

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

!!! tip "Override touch for sliding expiry"
    When `renew_on_access=True`, the middleware calls
    `touch(session_id, max_age)` to refresh an existing entry's expiry on a
    read-only access without rewriting its payload. The default reads then
    rewrites the payload; a store with a native TTL-refresh primitive (Redis
    `EXPIRE`, a SQL `UPDATE ... expires_at`) should override `touch` to avoid
    moving the payload.

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

## Minting and reading a cookie outside a request

Sometimes there is no request to hang a session on: a fixture that should start
logged in, a script that hands someone a pre-authenticated link, a test that
wants to see what a response actually set. `SessionMiddleware` exposes the two
halves of its cookie handling for that:

```python
from veloce.middleware.sessions import SessionMiddleware

middleware = SessionMiddleware(secret_key="change-me-in-production")

value = middleware.encode_cookie({"user_id": 7})   # signed cookie value
middleware.decode_cookie(value)                    # {"user_id": 7}
middleware.decode_cookie("forged")                 # None
```

`decode_cookie` returns `None` for a bad signature, a tampered payload, or a
token older than the middleware would accept - it does not raise. It is the
same code the request path runs, including the age ceiling that depends on
whether the session was marked permanent, so a cookie it accepts is a cookie a
request accepts. Building a `Signer` by hand with the same secret does **not**
reproduce that: the salt and the two-tier age check are part of the contract.

## Inspecting an in-memory store

`InMemorySessionStore` reads as well as writes, which is what a session count or
an idle-timeout check needs:

```python
from veloce.sessions import InMemorySessionStore

store = InMemorySessionStore()
await store.write("abc", {"user_id": 7}, max_age=3600)

len(store)                  # 1 - live sessions
"abc" in store              # True
store.expires_at("abc")     # Unix timestamp, or None
list(store)                 # ["abc"]
store.clear()               # revoke everything; returns how many went
```

All of these agree with `read` about what "present" means: an entry past its
expiry is absent whether or not the store has swept it yet, and none of them
evict as a side effect. `expires_at` is the only way to observe a sliding-expiry
refresh, since `touch` deliberately leaves the payload alone.

## Next steps

- [Flask-style helpers](helpers.md) — `session`, `flash`, `g`, and the
  rest of the request-scoped helpers.
- [Middleware](middleware.md) — ordering, function middleware, and the
  full middleware table.
- The [API reference](../reference/index.md) documents the `Signer` that backs
  the session cookie.
