---
description: Run a Veloce app under a process manager — a systemd unit, restart-on-crash, pre-start migrations, worker and memory sizing, and graceful shutdown that drains in-flight requests on SIGTERM.
tags: [deployment, systemd, shutdown, operations]
---

# Deployment concepts

A production deployment needs more than a serving command: a process manager to
start the app, restart it on crash, and stop it cleanly.

Veloce drains in-flight requests on `SIGTERM` on every serving path — the native
[`app.run()`](../reference.md#veloce.Veloce.run) server, the gunicorn
[`VeloceWorker`](workers.md), and uvicorn — so a
well-behaved process manager gets a clean handoff for free.

This page covers the operational layer around the serving command itself.

## Running under a process manager

A process manager keeps one long-running command alive: it starts the server,
restarts it if it exits, and signals it to stop on deploy or shutdown. On Linux
the usual choice is systemd; in containers it is the orchestrator (see
[Docker](docker.md)). The contract is the same on either: run one foreground
process, send it `SIGTERM` to stop, and treat a non-zero exit as a crash.

The command the manager runs is just one of the serving paths from
[Run a server manually](manually.md):

```bash
uvicorn app:app --host 0.0.0.0 --port 8000 --workers 4
```

!!! note
    Run the server in the **foreground** under a process manager — do not fork
    or daemonise. The manager tracks the process it started; a server that
    backgrounds itself becomes invisible to restart and signal handling.

## A systemd unit

A minimal unit serves the app, restarts it on crash, and stops it with
`SIGTERM`. Save it as `/etc/systemd/system/myapp.service`.

```ini title="/etc/systemd/system/myapp.service"
[Unit]
Description=My Veloce app
After=network.target

[Service]
Type=simple
User=myapp
WorkingDirectory=/srv/myapp
ExecStart=/srv/myapp/.venv/bin/uvicorn app:app --host 0.0.0.0 --port 8000 --workers 4
Restart=on-failure
RestartSec=2
KillSignal=SIGTERM
TimeoutStopSec=35

[Install]
WantedBy=multi-user.target
```

Enable and start it:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now myapp
```

The key directives:

| Directive | Value | Purpose |
| --- | --- | --- |
| `Restart` | `on-failure` | Restart the server only when it exits non-zero (a crash), not after a clean stop. |
| `RestartSec` | `2` | Wait two seconds before restarting, so a crash loop does not spin. |
| `KillSignal` | `SIGTERM` | Stop the server with `SIGTERM`, which Veloce treats as graceful drain. |
| `TimeoutStopSec` | `35` | Allow the drain window to finish before `SIGKILL`; keep it above the drain timeout. |

!!! warning "Run as a non-root user"
    Set `User=` to a dedicated unprivileged account. A web server reachable
    from the network must not run as `root`. Bind privileged ports (80/443) at
    a reverse proxy instead — see [HTTPS concepts](https.md).

## Restart on crash

`Restart=on-failure` covers the case where the process exits unexpectedly. The
process manager owns this — Veloce does not restart itself, and you should not
add a supervision loop inside the app.

`on-failure` deliberately does **not** restart after a clean `SIGTERM` stop, so
`systemctl stop myapp` does not immediately bounce the service. Use
`Restart=always` only if you want a restart even after a clean exit, which is
rarely what a deploy wants.

!!! note "Crash recovery is not state recovery"
    A restarted process starts with empty in-memory state: caches, rate-limit
    counters, and server-side sessions are gone. Keep anything that must survive
    a restart in an external store. See the [state table](workers.md#per-worker-versus-shared-state).

## Pre-start migrations

Run database migrations as a **separate step before** the server starts, not
inside a startup handler. With multiple workers, a startup handler runs once per
worker, so N workers race to apply the same migration — exactly what you do not
want. A single pre-start command runs the migration once, then the server boots.

Under systemd, `ExecStartPre` runs before `ExecStart` and a non-zero exit aborts
the start, so a failed migration never serves traffic on a half-migrated schema:

```ini title="myapp.service (Service section)"
[Service]
Type=simple
User=myapp
WorkingDirectory=/srv/myapp
ExecStartPre=/srv/myapp/.venv/bin/alembic upgrade head
ExecStart=/srv/myapp/.venv/bin/uvicorn app:app --host 0.0.0.0 --port 8000 --workers 4
Restart=on-failure
```

If your migration logic lives on the app object, expose it as an `app.cli`
command and run it with `veloce custom` before serving:

```python title="app.py"
from veloce import Veloce

app = Veloce()


@app.cli.command()
def migrate():
    """Apply pending database migrations."""
    # A real app would call its migration tool here.
    print("migrations applied")
```

```bash
veloce custom app:app -- migrate
```

`veloce custom` loads the app inside an application context (so `current_app`,
`g`, and `app.config` resolve) and forwards everything after `--` to the app's
Click group.

!!! tip "Gate the deploy on a clean migration"
    Run the migration as its own deploy step (or `ExecStartPre`) and fail the
    deploy if it errors. Serving against a schema the code does not expect is
    worse than a few seconds of downtime.

## Worker and memory sizing

```bash
# Size --workers to the core count.
nproc
```

Each worker is a separate process with its own Python interpreter and its own
`Veloce()` instance, so memory scales roughly linearly with the worker count. A
common starting point is one worker per CPU core; tune against your own latency
and memory profile under load.

Two facts drive sizing:

- **Memory is per-worker.** Total resident memory is roughly `workers ×`
  per-process footprint. On a memory-bound host, fewer workers may be the right
  call even with spare cores.
- **In-memory state does not span workers.** A cache entry, rate-limit counter,
  or server-side session written in one worker is invisible to the others. Move
  shared state to Redis or a database. See the full
  [per-worker state table](workers.md#per-worker-versus-shared-state).

To bound slow memory growth from leaks, recycle workers periodically. The
gunicorn `VeloceWorker` honours `--max-requests`, stopping a worker at the next
request boundary once it has handled that many requests:

```bash
gunicorn app:app -k veloce.workers.VeloceWorker \
    --workers 4 --max-requests 10000 --max-requests-jitter 1000
```

!!! note
    CPU-bound handlers benefit from one worker per core; I/O-bound async
    handlers may serve well with fewer, since a single async worker already
    overlaps many in-flight requests on one event loop. See
    [Server workers](workers.md) for the full comparison.

## Graceful shutdown

```python title="app.py"
from veloce import Veloce

app = Veloce()


@app.on_startup
async def open_pool():
    print("startup: open resources")


@app.on_shutdown
async def close_pool():
    print("shutdown: release resources")


@app.get("/")
async def index():
    return {"message": "Hello from Veloce"}


if __name__ == "__main__":
    app.run(port=8000)
```

Press `Ctrl+C` (or send `SIGTERM`) and the server drains active requests, then
runs `close_pool` before exiting.

When a process manager stops the server it sends `SIGTERM`. Veloce intercepts it
and drains in-flight requests instead of dropping connections mid-response, then
runs the app's `shutdown` lifecycle so resources opened at startup are released.

The native `app.run()` server drains in two phases on `SIGTERM` (or `SIGINT`):

- **Phase one — quiesce.** Every live connection finishes the request it is
  already dispatching and then closes at the request boundary, rather than being
  cancelled mid-pipeline. A connection accepted during shutdown serves at most
  its first request.
- **Phase two — bounded fallback.** Any dispatch still running after a 30-second
  window is cancelled, so a stuck handler can never hang the process forever.

After the drain, the `shutdown` lifecycle runs: `on_shutdown` handlers fire and
the `lifespan=` context manager exits, *after* requests have drained.

!!! note "The drain window and `TimeoutStopSec`"
    The native server allows in-flight dispatch up to 30 seconds before
    cancelling stragglers. Set systemd's `TimeoutStopSec` above that (35 seconds
    in the unit above) so the process manager does not `SIGKILL` the server
    mid-drain.

The same contract holds on the other serving paths:

| Serving path | `SIGTERM` behaviour |
| --- | --- |
| `app.run()` | Two-phase drain, then `on_shutdown` / `lifespan=` teardown. |
| `VeloceWorker` | gunicorn signals the worker; it drains in-flight tasks, then runs the `shutdown` lifecycle. |
| uvicorn | uvicorn drives the ASGI lifespan, so `on_shutdown` / `lifespan=` teardown runs on stop. |

!!! warning "Teardown only runs on a clean shutdown"
    `on_shutdown` handlers and `lifespan=` teardown run on a *graceful* stop,
    not on `SIGKILL` and not when startup itself fails. Give the process its
    drain window, and do not rely on shutdown handlers for state that must
    survive a hard kill.

## Verifying lifecycle teardown

You do not need a running server to confirm startup and shutdown fire. The
in-memory [`TestClient`](../reference.md#veloce.TestClient) runs the `startup`
lifecycle on construction and the `shutdown` lifecycle when its context exits.

```python
from veloce import TestClient, Veloce

app = Veloce()
events = []


@app.on_startup
async def on_start():
    events.append("startup")


@app.on_shutdown
async def on_stop():
    events.append("shutdown")


@app.get("/")
async def index():
    return {"ok": True}


with TestClient(app) as client:
    resp = client.get("/")
    assert resp.status_code == 200
    assert events == ["startup"]

assert events == ["startup", "shutdown"]
```

## Next steps

- Compare the serving commands a unit can run — see [Run a server manually](manually.md).
- Size and supervise multiple processes — see [Server workers](workers.md).
- Run the app in a container and let the orchestrator restart it — see [Docker](docker.md).
- Run startup and shutdown code correctly — see [Lifespan events](../guide/lifespan.md).
- Audit an app before deploying with `veloce check` — see [Deployment overview](../guide/deployment.md).
- Full signatures are in the [API reference](../reference.md).
```
