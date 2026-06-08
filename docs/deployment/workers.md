---
description: Run a Veloce app across multiple processes — uvicorn --workers, the gunicorn VeloceWorker that drives the native HttpProtocol, and which app state is per-worker versus shared.
tags: [workers, deployment, gunicorn, uvicorn]
---

# Server workers

A single process serves requests on one CPU core. To use every core on a host
you run several worker processes behind one listening socket. Veloce supports
two multi-process paths: any ASGI server's worker flag (`uvicorn --workers`)
and the gunicorn [`VeloceWorker`](../reference.md),
which lets gunicorn supervise processes while each worker drives Veloce's own
native HTTP/1.1 server with no ASGI shim.

!!! note
    The built-in [`app.run()`](../reference.md#veloce.Veloce.run) server is
    single-process. Passing `workers=` anything other than `1` raises
    `ValueError` — it does not pre-fork. Use one of the paths below for
    multiple processes.

## uvicorn workers

The simplest path is uvicorn's `--workers` flag. uvicorn forks the given number
of worker processes and load-balances accepted connections across them.

```bash
pip install veloceframework[uvicorn]
```

Point uvicorn at the module-level `app` object and ask for one worker per core.

```python title="main.py"
from veloce import Veloce

app = Veloce()


@app.get("/")
async def index(request):
    return {"message": "Hello from Veloce"}
```

```bash
uvicorn main:app --host 0.0.0.0 --port 8000 --workers 4
```

Each worker imports the module fresh and constructs its own `Veloce()`
instance, so no application object is shared between them. This is the
recommended production default.

!!! tip "One worker per container"
    When you deploy in containers, prefer a single worker per container and
    scale by running more containers — the orchestrator handles supervision and
    restarts. See [Docker](docker.md).

## The gunicorn VeloceWorker

`VeloceWorker` is an advanced alternative for stacks already built on gunicorn.
gunicorn owns process supervision (forking, restarts, signals, `--max-requests`
recycling) while each worker runs Veloce's native
[`HttpProtocol`](../reference.md#veloce.Veloce.run) — the same raw HTTP/1.1 and
WebSocket server `app.run()` uses — directly on an asyncio loop, bypassing ASGI
entirely.

```bash
pip install veloceframework[gunicorn]
```

Point gunicorn at the `app` object and select the worker class by import path.

```python title="main.py"
from veloce import Veloce

app = Veloce()


@app.get("/")
async def index(request):
    return {"message": "Hello from Veloce"}
```

```bash
gunicorn main:app -k veloce.workers.VeloceWorker --workers 4 --bind 0.0.0.0:8000
```

gunicorn binds the listening socket in the master and shares it across every
forked worker, so all workers pull from one kernel accept queue. Each worker
runs the app's `startup` lifecycle on boot and its `shutdown` lifecycle on
graceful stop, after draining in-flight requests.

!!! warning "POSIX-only and experimental"
    gunicorn is a POSIX-only optional dependency, so `VeloceWorker` does not run
    on Windows and the integration cannot be exercised there. Validate it on a
    POSIX host under your real gunicorn configuration before relying on it in
    production. uvicorn remains the recommended default.

### TLS under gunicorn

When gunicorn is started with `--certfile` and `--keyfile`, `VeloceWorker`
builds a server SSL context from gunicorn's TLS config and terminates TLS in
the worker.

```bash
gunicorn main:app -k veloce.workers.VeloceWorker \
    --bind 0.0.0.0:8443 --certfile cert.pem --keyfile key.pem
```

!!! warning "Fails fast rather than serving cleartext"
    gunicorn considers TLS enabled when either a cert or a key is set, but a
    usable server context needs the certificate chain. If the `certfile` is
    missing or the chain cannot be loaded, the worker raises `RuntimeError` and
    refuses to start rather than silently serving cleartext over an HTTPS
    deployment.

### Recycling workers

gunicorn's `--max-requests` (with optional `--max-requests-jitter`) recycles a
worker after it has handled that many requests, which bounds memory growth from
slow leaks. `VeloceWorker` honours it: once the count is reached the worker
stops accepting new requests at the next request boundary, finishes in-flight
work, and lets the master replace it.

```bash
gunicorn main:app -k veloce.workers.VeloceWorker \
    --workers 4 --max-requests 10000 --max-requests-jitter 1000
```

## Per-worker versus shared state

Every worker is a separate operating-system process with its own Python
interpreter and its own `Veloce()` instance. Anything held in memory lives in
exactly one worker and is invisible to the others. Plan for this: a value
written on one request may be served by a different worker on the next.

| State | Scope | Notes |
| --- | --- | --- |
| `app.config`, `app.state`, `app.extensions` | Per-worker | Constructed per process; mutations at runtime do not propagate to siblings. |
| [`InMemoryCache`](../guide/caching.md) | Per-worker | Each worker holds its own entries; a key cached on one is a miss on another. |
| [`InMemoryRateLimitBackend`](../guide/middleware.md) | Per-worker | Counters are per-process, so the effective limit is roughly `workers ×` the configured rate. |
| [`InMemorySessionStore`](../guide/sessions.md) | Per-worker | Server-side sessions are not shared; a session created on one worker is absent on another. |
| Background tasks via `app.spawn` | Per-worker | Supervised tasks run on the worker that scheduled them. |
| `RedisCache`, `RedisRateLimitBackend`, `RedisSessionStore` | Shared | Backed by Redis (in `veloce.contrib.redis`), so all workers see one consistent store. |
| Client cookies, signed sessions | Shared | State travels on the request, so every worker reads the same value. |

!!! warning "In-memory state does not survive multiple workers"
    With more than one worker, do not rely on in-memory caches, rate-limit
    counters, or server-side session stores for correctness. Move shared state
    to an external store (Redis, a database) or keep it on the client in a
    signed cookie. With `InMemoryRateLimitBackend` specifically, N workers
    enforce roughly N times the configured limit because each counts
    independently.

!!! tip "Use a shared backend for cross-worker state"
    Swap the in-memory cache, rate-limit, and session backends for their Redis
    equivalents in `veloce.contrib.redis` (`RedisCache`,
    `RedisRateLimitBackend`, `RedisSessionStore`) so every worker reads and
    writes one store. See [Caching](../guide/caching.md).

## Choosing a worker count

A common starting point is one worker per CPU core (a frequently cited formula
is `2 × cores + 1`), then tune against your own latency and memory profile
under load. More workers means more memory, since each holds a full copy of the
app and its per-process state.

```bash
# Inspect the core count, then size --workers to it.
nproc
```

!!! note
    The right number depends on the workload: CPU-bound handlers benefit from
    one worker per core, while I/O-bound async handlers may serve well with
    fewer, since a single async worker already overlaps many in-flight requests
    on one loop.

## Next steps

- [Run a server manually](manually.md) — uvicorn, `veloce run`, and `app.run()` compared.
- [Docker](docker.md) — one worker per container and scaling with the orchestrator.
- [Caching](../guide/caching.md) — swap the in-memory cache for a shared Redis backend.
- [HTTPS concepts](https.md) — terminating TLS at the proxy versus in the worker.
- Full signatures are in the [API reference](../reference.md).
