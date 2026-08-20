---
description: Run code once at startup and shutdown with the lifespan= context manager, on_startup/on_shutdown handlers, and test the cycle with TestClient or lifespan_context().
tags: [lifespan, startup, shutdown, events]
---

# Lifespan and events

A **lifespan** is code that runs once when the application starts and once when
it stops — open a database pool, warm a cache, connect a message broker, then
release all of it on the way down.

Veloce gives you two surfaces: a single
[`lifespan=`](../reference.md#veloce.Veloce) async context manager passed to the
constructor, or the [`on_startup`](../reference.md#veloce.Veloce.on_startup) /
[`on_shutdown`](../reference.md#veloce.Veloce.on_shutdown) handler decorators.

## The lifespan context manager

Write an `async` context manager that does setup before its `yield` and teardown
after it. Veloce calls it with the app instance, so you can stash resources on
[`app.state`](../reference.md#veloce.Veloce) for handlers to read.

```python title="app.py"
import contextlib

from veloce import Request, Veloce


@contextlib.asynccontextmanager
async def lifespan(app: Veloce):
    app.state.pool = await open_pool()
    yield
    await app.state.pool.close()


app = Veloce(lifespan=lifespan)


@app.get("/")
async def index(request: Request):
    return {"pool_open": request.app.state.pool is not None}
```

- Everything before `yield` runs on startup; everything after runs on shutdown.
- The value passed in is the app, so `app.state.<name>` is the natural place to
  hand resources to handlers (read them back as `request.app.state.<name>`).
- The context manager is entered first and exited *last* — after every
  `on_shutdown` handler — so resources it provides outlive the handlers using them.

!!! tip
    Use a single `lifespan=` manager when setup and teardown are paired (a
    resource you open and must close). Reach for `on_startup` / `on_shutdown`
    when you want several independent handlers.

## Startup and shutdown handlers

Register handlers with the decorators. Both `async def` and plain `def` are
accepted; sync handlers are offloaded to a thread so they never block the loop.

```python title="app.py"
from veloce import Veloce

app = Veloce()


@app.on_startup
async def connect_db():
    app.state.db = await connect()


@app.on_shutdown
async def disconnect_db():
    await app.state.db.close()
```

Startup handlers run in registration order. Shutdown handlers run in the reverse
order, so resources tear down newest-first. A shutdown handler that raises does
not abort the others — every handler runs, and the failures are re-raised
grouped once the cycle completes.

### `before_serving` and `after_serving`

[`before_serving`](../reference.md#veloce.Veloce.before_serving) and
[`after_serving`](../reference.md#veloce.Veloce.after_serving) are aliases for
`on_startup` and `on_shutdown` — they register into the same lists. Use whichever
name reads better at the call site.

```python
@app.before_serving
async def warm_cache():
    app.state.cache = await build_cache()


@app.after_serving
async def flush_cache():
    await app.state.cache.flush()
```

### The deprecated `on_event` API

[`on_event`](../reference.md#veloce.Veloce.on_event) (and the imperative
`add_event_handler`) still work but are deprecated.

```python
@app.on_event("startup")
async def legacy_startup():
    ...
```

!!! warning "Deprecated"
    `on_event(...)` and `add_event_handler(...)` are deprecated and scheduled
    for removal in v1.0.0. They emit a `DeprecationWarning`. Use `@app.on_startup`
    / `@app.on_shutdown` instead.

## Partial-startup unwinding

If a startup handler raises part-way through, Veloce unwinds exactly what already
succeeded before re-raising — the lifespan context manager is exited and any
already-run resources are released, in reverse order. A failed startup never
leaves orphaned connections behind.

```python title="app.py"
from veloce import Veloce

app = Veloce()


@app.on_startup
async def open_db():
    app.state.db = await connect()


@app.on_shutdown
async def close_db():
    await app.state.db.close()


@app.on_startup
async def open_broker():
    # If this raises, `open_db` has already run; its resource is unwound
    # before the startup error propagates.
    app.state.broker = await connect_broker()
```

!!! note
    The unwind closes the lifespan context manager and stops internal resources
    (such as the event-loop watchdog) that were acquired during startup. Your own
    `on_shutdown` handlers run only on a *clean* shutdown, not during a failed
    startup — so the lifespan manager remains the right place for paired
    open/close logic that must always be balanced.

## Mounted sub-application lifecycle

A Veloce sub-app mounted with [`mount`](../reference.md#veloce.Veloce.mount) is
dispatched through the parent pipeline and never receives its own ASGI lifespan.
The parent fans its lifecycle out to mounted Veloce children automatically.

```python title="app.py"
from veloce import Veloce

api = Veloce()


@api.on_startup
async def api_startup():
    api.state.ready = True


app = Veloce()
app.mount("/api", api)
```

- Each child's startup runs *after* the parent's own startup handlers.
- Children are torn down newest-first, *before* the parent's `on_shutdown`
  handlers — so a shared resource the parent closes is still live while each
  child releases work against it.
- The same child mounted under several prefixes is started and stopped once
  (deduped by identity).

!!! note
    Only mounted **Veloce** sub-apps get this fan-out. A plain ASGI app mounted
    with `mount` owns its own lifecycle and is left untouched.

## Testing the lifecycle

[`TestClient`](../reference.md#veloce.TestClient) runs startup when it is
constructed and shutdown when it is closed, so values set by your startup
handlers are available to the requests you make. Use it as a context manager to
guarantee shutdown runs.

```python title="test_app.py"
import contextlib

from veloce import Request, TestClient, Veloce


@contextlib.asynccontextmanager
async def lifespan(app: Veloce):
    app.state.started = True
    yield
    app.state.started = False


app = Veloce(lifespan=lifespan)


@app.get("/")
async def index(request: Request):
    return {"started": request.app.state.started}


with TestClient(app) as client:
    resp = client.get("/")
    assert resp.status_code == 200
    assert resp.json() == {"started": True}
```

### Driving the cycle directly with `lifespan_context()`

[`lifespan_context()`](../reference.md#veloce.Veloce.lifespan_context) returns an
async context manager that runs the full startup sequence on entry and the
shutdown sequence on exit, with no ASGI server or request in the loop. Use it to
embed the app, or to assert on startup state in an async test.

```python title="test_lifespan.py"
import contextlib

from veloce import Veloce


@contextlib.asynccontextmanager
async def lifespan(app: Veloce):
    app.state.pool = "open"
    yield
    app.state.pool = "closed"


app = Veloce(lifespan=lifespan)


async def test_lifespan_runs():
    async with app.lifespan_context():
        assert app.state.pool == "open"
    assert app.state.pool == "closed"
```

!!! note
    `pytest` is configured with `asyncio_mode = "auto"`, so an `async def test_`
    function runs without an explicit marker. The
    [`AsyncTestClient`](../reference.md#veloce.AsyncTestClient) runs the same
    startup/shutdown cycle across its `async with` block when you need full
    request testing on the event loop.

## Additional lifespans

`lifespan=` is one slot owned by the application. A plugin or extension that
needs its own scoped resource registers another with `add_lifespan`, keeping
setup and teardown in a single `try/finally` instead of splitting them across
`on_startup` and `on_shutdown`:

```python
import contextlib

from veloce import Veloce


class BrokerPlugin:
    name = "broker"

    def install(self, app):
        app.add_lifespan(self.lifespan)

    @contextlib.asynccontextmanager
    async def lifespan(self, app):
        self.conn = await connect()
        try:
            yield
        finally:
            await self.conn.close()


app = Veloce()
app.install(BrokerPlugin())
```

Registered lifespans share the application's exit stack, so they inherit its
guarantees: teardown runs in reverse registration order, a failure part-way
through startup unwinds only what was already entered, and teardown errors are
aggregated rather than masking one another.

The application's own `lifespan=` is entered first and exits last, so a
registered lifespan may depend on what it provided.

!!! note "Added in version 0.15"
    `app.add_lifespan()`. Earlier versions offered a single `lifespan=` slot,
    so an extension had to split its resource across two event handlers.

## Next steps

- [Databases](databases.md) — open a connection pool in the lifespan and inject it into handlers.
- [Background Tasks](background-tasks.md) — run work after the response instead of at startup.
- [Testing](testing.md) — the full `TestClient` and `AsyncTestClient` surface.
- Full signatures are in the [API reference](../reference.md).
