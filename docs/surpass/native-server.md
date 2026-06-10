---
description: >-
  Veloce's built-in asyncio server via app.run() — no uvicorn, the HttpProtocol RFC 6455 WebSocket
  handshake, two-phase graceful drain on SIGTERM, and the slowloris, keep-alive, and concurrency
  hardening knobs.
tags: [native, server, asyncio, hardening]
---

# Native server

[`Veloce.run()`](../reference.md#veloce.Veloce.run) starts a from-scratch HTTP/1.1 server built on a
raw `asyncio` transport — no uvicorn, no ASGI layer in the request path. This page covers the `run()`
signature, the native WebSocket handshake, the two-phase graceful drain on shutdown, and the
hardening knobs that bound slowloris attacks, idle connections, and connection floods. It also states
the two ways the native path diverges from running under uvicorn.

```bash
pip install veloceframework
```

!!! warning "The built-in server is for development"
    `app.run()` logs a startup reminder that it is a **development** server. For production, run the
    same app under a hardened ASGI server (`uvicorn module:app`) or the gunicorn `VeloceWorker`. The
    native server is single-process and intentionally minimal; the ASGI path is the supported
    production target.

## Running without uvicorn

```python title="app.py"
from veloce import Veloce

app = Veloce()


@app.get("/")
async def index():
    return {"message": "Hello from the native server"}


if __name__ == "__main__":
    app.run(port=8000)
```

`app.run()` brings up the server on its own event loop. The same `app` object also works as an ASGI
application, so nothing about the app changes between the two paths.

`run()` runs startup lifecycle hooks, creates the listening socket via `loop.create_server`, installs
`SIGINT`/`SIGTERM` handlers, and serves until a signal arrives. On exit it runs the graceful drain and
the shutdown lifecycle hooks. It uses `uvloop` automatically when it is installed and importable;
otherwise it falls back to the stdlib `asyncio` loop.

## The `run()` signature

`run()` takes only the arguments below. There is no `reload`, no `log_level`, and no `--workers` fan-out.

```python title="app.py"
import ssl

from veloce import Veloce

app = Veloce()


@app.get("/")
async def index():
    return {"ok": True}


if __name__ == "__main__":
    app.run(
        host="127.0.0.1",
        port=8000,
        access_log=True,
        ssl_context=None,
        bind_all=False,
    )
```

| Argument | Default | Purpose |
| --- | --- | --- |
| `host` | `None` → `"127.0.0.1"` | Interface to bind. Unset binds to localhost only. |
| `port` | `8000` | TCP port to listen on. |
| `workers` | `1` | Must be `1`; any other value raises `ValueError`. |
| `access_log` | `True` | Print the startup banner (`Listening on ...`). |
| `ssl_context` | `None` | An `ssl.SSLContext` to serve HTTPS; handed to `create_server(ssl=...)`. |
| `bind_all` | `False` | Bind `0.0.0.0` (all interfaces) instead of localhost. |

When `host` is left unset it resolves to `"127.0.0.1"`, so the dev server is reachable only from the
local machine. Pass `bind_all=True` to bind `0.0.0.0`.

!!! warning "`host=` and `bind_all=True` are mutually exclusive"
    Passing both raises `ValueError` to prevent a silent widening of the bind. Binding `0.0.0.0`
    exposes the dev server to **every** reachable network; never combine it with `debug=True`, which
    leaks source and tracebacks. `run()` logs a warning if you bind a non-local host with `debug=True`.

!!! note "`workers` must be `1`"
    The built-in server is single-process and does not pre-fork. `workers != 1` raises `ValueError`.
    For multiple processes, run under `uvicorn module:app --workers N` or the gunicorn `VeloceWorker`.

### Serving HTTPS for local testing

Pass an `ssl.SSLContext` to terminate TLS in the dev server. Left `None`, the serving path is
byte-for-byte identical to plain HTTP — the TLS cost is paid only when a context is supplied.

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

Production should still terminate TLS at uvicorn or a reverse proxy rather than in the application.

## Native WebSocket handshake

On the native path, `HttpProtocol` performs the RFC 6455 handshake itself. When httptools flags a
`Connection: Upgrade` request, the protocol scans the upgrade triplet (`Connection: upgrade`,
`Upgrade: websocket`, `Sec-WebSocket-Key`), matches a `@app.websocket` route, sends the
`101 Switching Protocols` response with the computed `Sec-WebSocket-Accept`, and diverts the byte
stream into the WebSocket frame parser.

```python title="app.py"
from veloce import Veloce, WebSocket

app = Veloce()


@app.websocket("/ws")
async def ws(websocket: WebSocket):
    await websocket.accept()
    await websocket.send_text("connected")
    async for message in websocket.iter_text():
        await websocket.send_text(f"echo: {message}")


if __name__ == "__main__":
    app.run(port=8000)
```

The same handler runs unchanged under an ASGI server; only the handshake mechanics differ. The
accept-key math lives once in `websocket.compute_accept` and is shared by both paths.

Pre-`101` refusals are ordinary HTTP responses, not close frames, because the protocol switch has not
completed yet:

| Condition | Response |
| --- | --- |
| `Sec-WebSocket-Version` is not `13` | `426 Upgrade Required` with `Sec-WebSocket-Version: 13` |
| Missing `Sec-WebSocket-Key` | `400 Bad Request` |
| No matching websocket route | `404 Not Found` |
| Host / `Origin` allow-list rejects the upgrade | `403 Forbidden` |

!!! warning "Native subprotocol negotiation is unsupported"
    On the native (`Veloce.run`) path the `101` is written synchronously **before** your handler's
    `accept()` runs, so there is nothing left to negotiate. Calling `accept(subprotocol=...)` — or
    passing custom handshake `headers=` — raises `RuntimeError` rather than silently dropping the
    value. Run under an ASGI server (uvicorn/hypercorn) when you need to negotiate a subprotocol.

!!! warning "The native path does not percent-decode the request path"
    `HttpProtocol` ascii-decodes the raw request target without percent-decoding it, so a handler sees
    `/%41` as the literal `/%41`. Under uvicorn (and the ASGI spec) the path arrives already decoded
    to `/A`. Match against decoded values, or run under uvicorn, if your routes depend on
    percent-decoded path segments.

## Graceful drain on shutdown

```python title="app.py"
import asyncio

from veloce import Veloce

app = Veloce()


@app.get("/slow")
async def slow():
    await asyncio.sleep(2)
    return {"done": True}


@app.on_shutdown
async def goodbye():
    app.logger.info("shutdown hooks running after the drain")


if __name__ == "__main__":
    app.run(port=8000)
```

A request already running when `SIGTERM` arrives runs to completion; the shutdown hooks fire only
after the drain finishes.

On `SIGINT`/`SIGTERM` (or Ctrl+C), `run()` stops accepting new connections and runs a **two-phase**
graceful drain so in-flight requests are never cut off mid-pipeline.

Phase one flips a drain flag on every live connection. Each connection finishes the request it is
already dispatching, then closes at the request boundary instead of being cancelled. A connection
admitted during the shutdown window serves at most its first request before quiescing; pipelined
follow-ups are declined so the client retries on a fresh connection.

Phase two is a hard fallback: any dispatch task still running after a bounded drain window is awaited
with a 30-second timeout, then cancelled, so a stuck handler can never hang the process. The shutdown
lifecycle hooks run last.

## Hardening knobs

Every limit below is read from `app.config` at the point it is needed, so you set it once before
`run()`. The native path enforces the same `MAX_CONTENT_LENGTH` body cap as the ASGI path.

```python title="app.py"
from veloce import Veloce

app = Veloce()
app.config["REQUEST_TIMEOUT"] = 30
app.config["KEEP_ALIVE_TIMEOUT"] = 75
app.config["MAX_CONCURRENT_CONNECTIONS"] = 1000
app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024


@app.get("/")
async def index():
    return {"ok": True}


if __name__ == "__main__":
    app.run(port=8000)
```

| Config key | Default | Purpose |
| --- | --- | --- |
| `REQUEST_TIMEOUT` | `30` | Slowloris guard: once a request's bytes start, the whole request line, headers, and body must complete within this budget or the connection is dropped with `408`. |
| `KEEP_ALIVE_TIMEOUT` | `75` | Idle keep-alive timeout: an open connection with no request in flight is closed after this many seconds. |
| `MAX_CONCURRENT_CONNECTIONS` | `1000` | Per-process cap on simultaneously-open connections; a connection over the cap is refused with `503`. |
| `MAX_CONTENT_LENGTH` | `None` | Maximum request body size. An over-limit declared `Content-Length` is refused with `413` before any body is read. |
| `REQUEST_HANDLER_TIMEOUT` | `30` | Per-request handler timeout; a handler exceeding it yields `504 Gateway Timeout`. |

The slowloris and idle timers are mutually exclusive per connection: the idle `KEEP_ALIVE_TIMEOUT`
governs a connection waiting for its next request, and the shorter `REQUEST_TIMEOUT` takes over the
moment a request's first bytes arrive.

The connection cap is enforced under a lock at `connection_made`, so a burst of parallel connections
cannot over-admit past the limit.

!!! note "The body cap is enforced twice"
    `MAX_CONTENT_LENGTH` rejects an honest over-limit `Content-Length` up front with `413`, and the
    streamed running total is a backstop: a client that lies about its length or chunks past the cap
    is still cut off with `413` once the body crosses the limit.

### TCP keepalive

OS-level TCP keepalive is on by default (`TCP_KEEPALIVE`, default `True`), so the kernel reaps a peer
that died without sending a `FIN` — a half-open connection the application idle timer never observes
because no further bytes arrive. The per-socket tuning options are platform-specific and applied only
where the platform exposes them.

| Config key | Default | Purpose |
| --- | --- | --- |
| `TCP_KEEPALIVE` | `True` | Enable `SO_KEEPALIVE` on accepted connection sockets. |
| `TCP_KEEPALIVE_IDLE` | `None` | Idle seconds before the first probe (Linux/macOS only). |
| `TCP_KEEPALIVE_INTERVAL` | `None` | Seconds between probes (Linux only). |
| `TCP_KEEPALIVE_COUNT` | `None` | Failed probes before the connection is dropped (Linux only). |

!!! note
    Windows exposes only `SO_KEEPALIVE` via `setsockopt`, so the idle/interval/count options are
    silently skipped there. A `None` value leaves the OS default in place on every platform.

## Native versus uvicorn

The native path and the ASGI path converge on the same dispatch core, so handlers, dependencies, and
middleware behave identically. The differences are at the transport edge.

| Aspect | Native (`app.run()`) | Under uvicorn |
| --- | --- | --- |
| Process model | Single process, no pre-fork | `--workers N` pre-forks |
| Request path | Not percent-decoded | Decoded per the ASGI spec |
| WebSocket subprotocol | `accept(subprotocol=...)` raises | Negotiated normally |
| TLS | `ssl_context=` for local testing | Terminate at uvicorn or a proxy |
| Hardening knobs | `REQUEST_TIMEOUT`, `KEEP_ALIVE_TIMEOUT`, `MAX_CONCURRENT_CONNECTIONS` | uvicorn's own limits |

For production, prefer the ASGI path: multi-worker scaling, a percent-decoded path matching the ASGI
spec, and full subprotocol negotiation all come for free.

## Next steps

- [WebSockets](../guide/websockets.md) — idle timeouts, heartbeats, and origin checks on both paths.
- [Migrating from FastAPI](migrating-from-fastapi.md) — every divergence in one place.
- Full signatures are in the [API reference](../reference.md).
