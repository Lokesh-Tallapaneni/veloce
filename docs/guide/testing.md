---
description: Test Veloce apps with the in-memory TestClient and AsyncTestClient — drive HTTP, WebSockets, lifespan, sessions, and dependency overrides without a socket or uvicorn.
tags: [testing, testclient, websockets, pytest]
---

# Testing

Veloce ships an in-memory [`TestClient`](../reference/testing.md#veloce.TestClient) and
[`AsyncTestClient`](../reference/testing.md#veloce.AsyncTestClient). They construct ASGI
scopes directly and call the app's `__call__` surface, so the radix router,
dependency resolver, middleware chain, response encoder, and the lifespan
handshake all run — no socket, no separate server.

Both clients ship with Veloce; there is nothing extra to install.

## Creating a client

`app.test_client()` returns a sync client bound to your application:

```python
from veloce import Veloce

app = Veloce()


@app.get("/ping")
async def ping():
    return {"pong": True}


def test_ping():
    client = app.test_client()
    response = client.get("/ping")
    assert response.status_code == 200
    assert response.json() == {"pong": True}
```

Construct it explicitly when you prefer the class form:

```python
from veloce import TestClient, Veloce

app = Veloce()

client = TestClient(app)
```

!!! note
    `TestClient(app)` runs the app's `startup` lifecycle once at construction,
    so startup hooks have already populated `app.state` before your first call.
    The matching `shutdown` runs on `close()` or context-manager exit.

!!! note "The event loop the client uses"
    The client drives your app on its own event loop. On Windows that is a
    `SelectorEventLoop` rather than the platform default: the in-memory client
    opens no socket and spawns no process, so the default proactor loop's I/O
    completion port is pure overhead on every request.

    Pass your own loop when you need one — in particular for a handler that
    calls `asyncio.create_subprocess_exec`, which on Windows requires the
    proactor loop:

    ```python
    import asyncio

    from veloce.testclient import TestClient

    loop = asyncio.ProactorEventLoop()
    client = TestClient(app, loop=loop)
    ```

    A loop you pass is yours: the client never closes it.

## Making requests

The client mirrors the HTTP verbs and returns a response object exposing these
attributes:

| Attribute        |
| ---------------- |
| `status_code`    |
| `json()`         |
| `text`           |
| `headers`        |
| `content_type`   |
| `cookies`        |

```python
client.get("/items", params={"page": "2"})
client.post("/users", json={"name": "ada", "email": "ada@example.com"})
client.post("/login", data={"username": "ada", "password": "secret"})
client.post("/upload", files={"f": ("data.csv", b"a,b,c")})
client.delete("/items/1")
```

Cookies set by the application persist across calls on the same client. Pass raw
bytes with `content=...`, form fields with `data=...`, and a JSON body with
`json=...` — the matching `Content-Type` is set for you unless you override it.

### Streaming a request body

`post`, `put`, `patch`, and `request` accept `stream=...` to feed the request
body as multiple ASGI `http.request` chunks instead of a single frame, which
exercises handlers that consume the body incrementally. Pass a sync `Iterable`
or an `AsyncIterable` of `bytes`/`str` chunks; `stream` takes precedence over
and excludes `json`/`data`/`content`/`files`:

```python
def chunks():
    yield b"first "
    yield b"second"


client.post("/ingest", stream=chunks())
```

### Following redirects

Redirects are **not** followed by default. Pass `follow_redirects=True` per call,
or set it once on the client:

```python
client = app.test_client(follow_redirects=True)

resp = client.get("/old-path")          # 301/302/303 → final 200
assert resp.status_code == 200
```

!!! note
    301, 302, and 303 rewrite the next hop to `GET` with an empty body (every
    browser does the same); 307 and 308 preserve the method and body. The client
    raises after 10 hops to catch redirect loops.

## Lifespan startup and shutdown

Use the sync client as a context manager to run `startup` / `shutdown` events
around the block:

```python
from veloce import Veloce

app = Veloce()
started = {}


@app.on_startup
async def warm_cache():
    started["ready"] = True


def test_with_lifespan():
    with app.test_client() as client:
        assert started["ready"] is True          # startup ran on enter
        assert client.get("/ping").status_code in (200, 404)
    # shutdown handlers have run here
```

See [Lifespan and events](lifespan.md) for registering startup/shutdown hooks
and the `lifespan=` context-manager form.

## Async tests with `AsyncTestClient`

When the test itself is `async def`, use
[`AsyncTestClient`](../reference/testing.md#veloce.AsyncTestClient) so each request is
awaited on the test's own running loop instead of a private one. It is always
used as an **async context manager** — requests are refused outside `async with`:

```python
from veloce import Veloce

app = Veloce()


@app.get("/ping")
async def ping():
    return {"pong": True}


async def test_ping_async():
    async with app.async_test_client() as client:
        resp = await client.get("/ping")
        assert resp.status_code == 200
        assert resp.json() == {"pong": True}
```

The request methods (`get` / `post` / ...) are coroutines, so each call is
`await`ed. Cookie persistence, redirect following, and the JSON / form / files
body shapes match the sync `TestClient` exactly.

!!! note
    Under pytest with `asyncio_mode = "auto"`, plain `async def test_*`
    functions are collected automatically — do not decorate them with
    `@pytest.mark.asyncio`.

WebSocket testing stays on the sync `TestClient.websocket_connect`.

## Testing WebSockets

`websocket_connect` opens an in-memory WebSocket against an `@app.websocket`
route and returns a context manager. Send and receive text, bytes, or JSON:

```python
from veloce import Veloce, WebSocket

app = Veloce()


@app.websocket("/ws")
async def echo(ws: WebSocket):
    await ws.accept()
    msg = await ws.receive_text()
    await ws.send_text(f"echo: {msg}")


def test_ws_echo():
    client = app.test_client()
    with client.websocket_connect("/ws") as ws:
        ws.send_text("hello")
        assert ws.receive_text() == "echo: hello"
```

### JSON and bytes frames

`send_json` / `receive_json` serialise through the same encoder the framework
uses, and `send_bytes` / `receive_bytes` carry binary frames:

```python
with client.websocket_connect("/ws") as ws:
    ws.send_json({"op": "ping"})
    assert ws.receive_json() == {"op": "pong"}

    ws.send_bytes(b"\x00\x01")
    assert ws.receive_bytes() == b"\x00\x01"
```

### Subprotocols

Pass the offered subprotocols to `websocket_connect`; the negotiated one is on
`accepted_subprotocol` after the handshake:

```python
with client.websocket_connect("/ws", subprotocols=["chat", "v1"]) as ws:
    assert ws.accepted_subprotocol == "chat"
```

!!! warning "Native subprotocol negotiation differs on the `app.run()` path"
    Under an ASGI server (and the in-memory `TestClient`) the handshake can
    select a subprotocol. On the native `app.run()` transport the `101` is sent
    before `accept()` runs, so `accept(subprotocol=...)` raises there. Test
    subprotocol negotiation through the ASGI surface, which `TestClient` drives.

A handler that closes the socket during the handshake (instead of accepting)
raises a `RuntimeError` from `websocket_connect`, so a rejected connection fails
the test loudly rather than hanging.

## Overriding dependencies

Swap real dependencies — databases, auth, external APIs — for fakes by writing
into `app.dependency_overrides`. The key is the original `Depends` callable; the
value is the replacement:

```python
from veloce import Depends, TestClient, Veloce


def get_db():
    raise RuntimeError("real DB not available in tests")


def fake_db():
    return {"users": ["ada"]}


app = Veloce()


@app.get("/users")
async def list_users(db=Depends(get_db)):
    return db["users"]


app.dependency_overrides[get_db] = fake_db

client = TestClient(app)
resp = client.get("/users")
assert resp.status_code == 200
assert resp.json() == ["ada"]
```

`dependency_overrides` is a plain dict. Reset it between tests so an override
does not leak into the next case — `app.dependency_overrides.clear()` (or pop a
single key) in your fixture teardown:

```python
app.dependency_overrides.clear()
```

See [Dependency injection](dependency-injection.md#overriding-dependencies-in-tests)
for the full pattern.

## Mutating the session

When the app has `SessionMiddleware`, `session_transaction()` lets a test seed
the session outside a request. It yields a session dict pre-loaded from the
current cookie; on block exit the session is re-signed and stored in the cookie
jar, so the next request carries it:

```python
from veloce import Veloce
from veloce.middleware.sessions import SessionMiddleware

app = Veloce()
app.add_middleware(SessionMiddleware, secret_key="test-secret-do-not-ship")


@app.get("/whoami")
async def whoami(request):
    return {"user_id": request.session.get("user_id")}


def test_session_seed():
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["user_id"] = 7
    resp = client.get("/whoami")
    assert resp.json() == {"user_id": 7}
```

!!! note
    `session_transaction()` raises `RuntimeError` if the app has no
    `SessionMiddleware` — there is no session to mutate without it.

## Surfacing handler exceptions

By default an unhandled exception in a handler is converted to a `500` response,
just as in production, so the test sees `resp.status_code == 500`. To make the
exception propagate out of the call and fail the test with the real traceback,
enable propagation:

```python
app.config["PROPAGATE_EXCEPTIONS"] = True
```

Propagation is also implied when both `DEBUG` and `TESTING` are enabled.

!!! warning "Veloce uses `PROPAGATE_EXCEPTIONS`, not a TestClient kwarg"
    The Veloce `TestClient` has **no** `raise_server_exceptions`, `backend`, or
    `root_path` constructor argument. Control exception propagation through
    `app.config["PROPAGATE_EXCEPTIONS"]` (or `DEBUG` + `TESTING`), not a client
    flag. The full constructor signature is:

    ```python
    TestClient(app, base_url="http://testserver", follow_redirects=False, loop=None)
    ```

    `loop=` supplies an existing event loop; omitted, the client owns its own.
    `AsyncTestClient` takes the first three — it runs on the caller's loop.

## Next steps

- Wire fakes into your routes — see [Dependency injection](dependency-injection.md).
- Run startup and shutdown hooks under test — see [Lifespan and events](lifespan.md).
- Swap a real database for an in-memory fake — see [Databases](databases.md).
- Full signatures are in the [API reference](../reference/index.md).
