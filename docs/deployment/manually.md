---
description: Serve a Veloce app three ways — the native app.run() server, the veloce run CLI, and uvicorn — with host/port binding, ssl_context, and workers.
tags: [deployment, server, uvicorn, cli]
---

# Run a server manually

A Veloce app is a plain ASGI application, so it serves the same three ways:
the built-in [`app.run()`](../reference/application.md#veloce.Veloce.run) server, the
[`veloce run`](#the-veloce-run-cli) CLI, and any ASGI server such as
uvicorn. This page covers when to reach for each and how to set the host,
port, TLS context, and worker count.

| Way to serve | Best for | Multiple processes |
| --- | --- | --- |
| `app.run()` | Local development, single-binary deploys. | No (single process). |
| `veloce run app:app` | One command in dev or a plain install. | Under uvicorn only. |
| `uvicorn app:app` | Production. | Yes (`--workers N`). |

## The native `app.run()` server

`app.run()` starts Veloce's own asyncio HTTP server with no extra
dependency. It is meant for local development; it logs a reminder of that
on startup.

```python title="app.py"
from veloce import Veloce

app = Veloce()


@app.get("/")
async def index():
    return {"message": "Hello from Veloce"}


if __name__ == "__main__":
    app.run(port=8000)
```

Run it with `python app.py`. The server binds `127.0.0.1` by default, so it
is reachable only from the local machine.

!!! warning "The built-in server is for development"
    `app.run()` targets the development inner loop, not production traffic.
    For anything internet-facing, serve the app under [uvicorn](#serving-under-uvicorn)
    or the gunicorn `VeloceWorker`. See [Server workers](workers.md).

### Binding host and port

`host` and `port` control the listening socket. The default `host` is
`127.0.0.1` (localhost only); pass `bind_all=True` to listen on every
interface (`0.0.0.0`) instead.

```python
app.run(host="127.0.0.1", port=8000)   # localhost only (the default host)
app.run(bind_all=True, port=8000)      # all interfaces, i.e. 0.0.0.0
```

`host` and `bind_all=True` are mutually exclusive — passing both raises
`ValueError` so an all-interfaces bind is never selected silently.

!!! warning "Binding all interfaces exposes the dev server"
    `bind_all=True` (`0.0.0.0`) exposes the development server to every
    reachable network, including remote attackers on a public network. Use
    it only in trusted environments, and never together with `debug=True` —
    debug tracebacks leak source and internals to anyone who can reach the
    port.

### Enabling TLS for local testing

Pass an `ssl.SSLContext` as `ssl_context` to serve HTTPS locally. The
context is handed straight to the event loop; left `None` (the default)
the path is byte-for-byte plain HTTP.

```python title="app.py"
import ssl

from veloce import Veloce

app = Veloce()


@app.get("/")
async def index():
    return {"secure": True}


if __name__ == "__main__":
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain("cert.pem", "key.pem")
    app.run(port=8443, ssl_context=context)
```

!!! note "Terminate production TLS upstream"
    `ssl_context` is for local HTTPS testing. In production, terminate TLS
    at uvicorn or a reverse proxy rather than in the app. See
    [HTTPS concepts](https.md).

### Workers

The built-in server is single-process and does not pre-fork. `workers`
must be `1` (the default); any other value raises `ValueError`.

```python
app.run(port=8000, workers=1)   # the only accepted value
```

!!! warning "`workers` must be 1 on the native server"
    For multiple processes, run under `uvicorn module:app --workers N` or
    the gunicorn `VeloceWorker`. The native server rejects `workers > 1`
    rather than silently running one process. See [Server workers](workers.md).

The full signature is:

```python
app.run(
    host=None,             # None -> "127.0.0.1" (or "0.0.0.0" when bind_all=True)
    port=8000,
    workers=1,             # must be 1
    access_log=True,       # print the startup banner ("Listening on ...")
    ssl_context=None,      # an ssl.SSLContext enables HTTPS
    bind_all=False,        # True binds 0.0.0.0; conflicts with host=
)
```

## The `veloce run` CLI

`veloce run` resolves a `module:attribute` reference and serves it. When
uvicorn is installed (the optional `[uvicorn]` extra) the app is handed to
uvicorn; otherwise it falls back to the built-in `app.run()` server, so the
command works on a plain install.

```bash
veloce run app:app --host 0.0.0.0 --port 8000
```

The reference string is the same `module:attribute` form ASGI servers use,
so `uvicorn app:app` accepts it too. The current directory is added to the
import path, so `veloce run app:app` works from the project root without an
editable install.

| Flag | Default | Purpose |
| --- | --- | --- |
| `--host` | `127.0.0.1` | Interface to bind. |
| `--port` | `8000` | Port to bind. |
| `--reload` | off | Auto-reload on code changes (uvicorn only). |
| `--workers` | `1` | Worker processes (uvicorn only). |
| `--log-level` | `info` | Log level passed to uvicorn. |

!!! note "`--reload` and `--workers` need uvicorn"
    Both are uvicorn features. With uvicorn absent, `--reload` raises an
    error pointing at `pip install veloceframework[uvicorn]`, and
    `--workers N` is ignored — the built-in server runs a single process.

Install the extra to get reload, multi-worker, and the recommended
production server:

```bash
pip install veloceframework[uvicorn]
```

## Serving under uvicorn

For production, run the app under a hardened ASGI server. Veloce
implements `__call__(scope, receive, send)`, so uvicorn (or Hypercorn,
Granian, …) serves it directly.

```bash
pip install uvicorn
uvicorn app:app --host 0.0.0.0 --port 8000 --workers 4
```

uvicorn brings worker management, graceful reloads, and TLS termination.
`--workers N` forks N independent processes that share no Python memory —
see [Server workers](workers.md) for the per-worker state table.

When uvicorn sits behind a reverse proxy, add
[`ProxyFix`](../reference/middleware.md#veloce.ProxyFix) so the app trusts the
proxy's `X-Forwarded-*` headers for the real client IP and scheme:

```python title="app.py"
from veloce import ProxyFix, Veloce

app = Veloce()

# One proxy hop in front: trust the last X-Forwarded-For / -Proto entry.
app.add_middleware(ProxyFix, x_for=1, x_proto=1)
```

## Verifying the app serves

You do not need a running server to check the app responds. The in-memory
[`TestClient`](../reference/testing.md#veloce.TestClient) dispatches requests
straight through the ASGI app — no socket, no uvicorn.

```python
from veloce import TestClient, Veloce

app = Veloce()


@app.get("/")
async def index():
    return {"message": "Hello from Veloce"}


client = TestClient(app)

resp = client.get("/")
assert resp.status_code == 200
assert resp.json() == {"message": "Hello from Veloce"}
```

## Next steps

- Run multiple processes and externalise shared state — see [Server workers](workers.md).
- Terminate TLS correctly in production — see [HTTPS concepts](https.md).
- Put the app behind a reverse proxy or container — see [Docker](docker.md).
- Full signatures are in the [API reference](../reference/index.md).
