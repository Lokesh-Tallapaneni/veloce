---
description: Mount Veloce sub-applications, arbitrary ASGI apps, StaticFiles, and WSGI apps at a path prefix with app.mount, and understand lifecycle fan-out and prefix-overlap rules.
tags: [mounts, asgi, wsgi, routing]
---

# Sub-applications and mounts

[`app.mount`](../reference/application.md#veloce.Veloce.mount) attaches a second application
under a path prefix on the parent app. The mounted thing can be another
[`Veloce`](../reference/application.md#veloce.Veloce) instance, any ASGI application, or a
[`StaticFiles`](../reference/templating.md#veloce.StaticFiles) handler. This lets you
compose a large service from independently-built pieces that each own a subtree
of the URL space.

## Mounting a Veloce sub-application

Build the child like any other app, then mount it at a prefix on the parent. The
prefix is stripped before the child matches, so the child's routes are written
relative to its own root.

```python title="app.py"
from veloce import Request, Veloce

admin = Veloce()


@admin.get("/users")
async def list_users(request: Request):
    return {"users": ["ada", "alan"]}


app = Veloce()
app.mount("/admin", admin)


@app.get("/")
async def index(request: Request):
    return {"app": "main"}


if __name__ == "__main__":
    app.run()
```

A request to `/admin/users` is dispatched into the `admin` sub-app, which sees
the path as `/users`. A request to `/` is handled by the parent.

```python
from veloce import Request, TestClient, Veloce

admin = Veloce()


@admin.get("/users")
async def list_users(request: Request):
    return {"users": ["ada", "alan"]}


app = Veloce()
app.mount("/admin", admin)

client = TestClient(app)

resp = client.get("/admin/users")
assert resp.status_code == 200
assert resp.json() == {"users": ["ada", "alan"]}
```

A mounted Veloce sub-app is dispatched **through the parent's request
pipeline** — the parent matches the prefix, builds a sub-request scoped to the
remaining path, and hands it to the child's request handler. The child's own
routing, dependency injection, and error handling all run.

!!! note
    A Veloce sub-app is matched before the parent's own routes and static
    handlers during dispatch. The parent does not need a route registered for
    the prefix; the mount owns the whole subtree under it.

## Lifecycle fan-out to mounted Veloce apps

A mounted Veloce sub-app does not receive its own ASGI `lifespan` cycle, because
it is driven through the parent. Instead the parent **fans its lifecycle out**
to every mounted Veloce child:

| Phase | Behaviour |
| --- | --- |
| Startup | The parent runs its own startup, then each child's startup in registration order. |
| Shutdown | Children are torn down newest-first, before the parent's own `on_shutdown` handlers. |
| Mid-fan-out failure | If a child's startup fails mid-fan-out, the already-started children are unwound in reverse so nothing leaks. |
| Duplicate mount | The same child instance mounted under more than one prefix is started and shut down exactly once (deduplicated by identity). |

```python title="app.py"
from veloce import Request, Veloce

reports = Veloce()


@reports.on_startup
async def open_pool():
    reports.state.pool = "connected"


@reports.on_shutdown
async def close_pool():
    reports.state.pool = None


@reports.get("/daily")
async def daily(request: Request):
    return {"pool": request.app.state.pool}


app = Veloce()
app.mount("/reports", reports)


if __name__ == "__main__":
    app.run()
```

The child's `on_startup` and `on_shutdown` hooks (and any `lifespan` context
manager it declares) run as part of the parent's startup and shutdown, so the
child's resources are ready before the first request and released on exit.

!!! note
    Mount the sub-app **before** the parent starts. Mounting is a setup-time
    operation; a child mounted after the parent has already started will not have
    its startup driven. The in-memory [`TestClient`](../reference/testing.md#veloce.TestClient)
    runs the parent's startup on construction, so build the full mount tree
    before creating the client.

## Mounting an arbitrary ASGI app

Any ASGI application — a micro-framework, an instrumentation shim, a GraphQL
endpoint — can be mounted. A non-Veloce ASGI mount is dispatched at the **ASGI
layer**, not through the parent pipeline: the matched prefix is moved from the
scope's `path` onto its `root_path`, so the mounted app sees a normal
root-relative request.

```python title="app.py"
from veloce import Request, Veloce


async def asgi_health(scope, receive, send):
    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-type", b"text/plain")],
        }
    )
    await send({"type": "http.response.body", "body": b"ok"})


app = Veloce()
app.mount("/health", asgi_health)


@app.get("/")
async def index(request: Request):
    return {"app": "main"}


if __name__ == "__main__":
    app.run()
```

A mounted ASGI app owns its entire prefix subtree — a native route registered
under the same prefix is unreachable. The mount receives `http` and `websocket`
scopes only.

!!! warning "ASGI mounts run only under an ASGI server"
    ASGI mounts are dispatched in Veloce's ASGI entry point, which is exercised
    by an ASGI server (uvicorn, hypercorn) and by the in-memory `TestClient`.
    The native [`app.run()`](../reference/application.md#veloce.Veloce.run) transport
    dispatches Veloce sub-apps and static handlers, but does not route into
    arbitrary ASGI mounts. Serve a tree that uses ASGI mounts through an ASGI
    server.

!!! warning "ASGI mounts get no lifespan"
    The parent fans its lifecycle out to mounted *Veloce* sub-apps only. A
    non-Veloce ASGI mount is never sent the ASGI `lifespan` events, so it must
    not depend on `lifespan.startup` / `lifespan.shutdown` for its setup or
    teardown. Acquire its resources another way (lazily on first request, or
    from the parent's own startup hook).

## Mounting StaticFiles

A [`StaticFiles`](../reference/templating.md#veloce.StaticFiles) handler looks ASGI-shaped
but speaks Veloce's internal `handle` protocol, so `mount` special-cases it: the
mount prefix becomes the handler's serving prefix and it is registered as a
static handler rather than an ASGI mount.

```python title="app.py"
from veloce import StaticFiles, Veloce

app = Veloce()
app.mount("/assets", StaticFiles(directory="static"))


if __name__ == "__main__":
    app.run()
```

For the common case of serving one directory, prefer
[`app.mount_static`](../reference/application.md#veloce.Veloce.mount_static), which builds
the `StaticFiles` handler for you. See [Static files](static-files.md) for the
full handler options.

## Mounting a WSGI app

Veloce has no built-in WSGI bridge. To mount a WSGI application (Flask, Django,
a bare `def app(environ, start_response)`), wrap it in an ASGI-to-WSGI adapter
and mount the resulting ASGI app. The
[`a2wsgi`](https://pypi.org/project/a2wsgi/) package provides one:

```bash
pip install a2wsgi
```

```python title="app.py"
from a2wsgi import WSGIMiddleware

from veloce import Request, Veloce


def legacy_wsgi_app(environ, start_response):
    start_response("200 OK", [("content-type", "text/plain")])
    return [b"served by WSGI"]


app = Veloce()
app.mount("/legacy", WSGIMiddleware(legacy_wsgi_app))


@app.get("/")
async def index(request: Request):
    return {"app": "main"}


if __name__ == "__main__":
    app.run()
```

`WSGIMiddleware` is an ASGI app, so it follows every ASGI-mount rule above: it
receives `http` scopes with the prefix moved to `root_path`, gets no `lifespan`
events, and must be served through an ASGI server.

!!! warning "WSGI calls block a worker thread"
    WSGI is a synchronous protocol. The adapter runs the WSGI app in a thread so
    it does not block the event loop, but a slow WSGI request still occupies a
    worker thread for its full duration. Keep WSGI mounts for legacy endpoints,
    not high-throughput paths.

## Prefix-overlap rules

Mount prefixes must not overlap. Registering a prefix that is equal to, nested
under, or a parent of an existing mount raises `ValueError` at mount time,
because overlapping mounts would shadow each other in an order-dependent way.

```python title="app.py" hl_lines="9"
from veloce import Veloce

admin = Veloce()
reports = Veloce()

app = Veloce()
app.mount("/admin", admin)
app.mount("/admin/reports", reports)  # raises ValueError: overlaps "/admin"
```

The overlap check spans both Veloce sub-app mounts and ASGI mounts: an ASGI
mount at `/admin` and a Veloce mount at `/admin/users` conflict the same way.
Bare prefixes are normalised first — a trailing slash is stripped and a leading
slash is added if missing, so `mount("admin/", ...)` and `mount("/admin", ...)`
register the same prefix and therefore collide.

```python
from veloce import TestClient, Veloce

app = Veloce()
app.mount("/admin", Veloce())

raised = False
try:
    app.mount("/admin/reports", Veloce())
except ValueError:
    raised = True

assert raised

client = TestClient(app)
```

!!! note
    Passing something that is neither a `Veloce` app, a `StaticFiles` instance,
    nor an ASGI callable to `mount` raises `TypeError` at registration time
    rather than failing on the first request.

## Next steps

- Compose routes within a single app instead of mounting — see [Routing](routing.md)
  and blueprints.
- Run startup and shutdown work for the parent and its sub-apps — see
  [Lifespan events](lifespan.md).
- Serve a static directory at a prefix — see [Static files](static-files.md).
- Sit the whole tree behind a reverse proxy — see [Behind a proxy](behind-a-proxy.md).
- Full signatures are in the [API reference](../reference/index.md).
