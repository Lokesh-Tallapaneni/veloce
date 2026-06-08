---
description: Containerise a Veloce app with a minimal Dockerfile, running it under the built-in server (app.run) or uvicorn, with exec-form CMD and ProxyFix behind a load balancer.
tags: [docker, deployment, uvicorn, proxyfix]
---

# Docker

Veloce is a plain ASGI application, so it containerises like any other Python
web app. A Veloce image needs only the interpreter, the `veloceframework`
package, and your code — there is no mandatory ASGI server in the request path,
since [`app.run()`](../reference.md#veloce.Veloce.run) ships a built-in HTTP/1.1
and WebSocket server. This page builds a minimal image for both serving paths.

## A minimal Dockerfile

Copy the app in, install `veloceframework`, and run it. This image uses the
built-in server via `app.run()` — no extra server dependency.

```dockerfile title="Dockerfile"
FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

CMD ["python", "main.py"]
```

```text title="requirements.txt"
veloceframework
```

The `main.py` it runs is an ordinary full-program file. Bind to all interfaces
with `bind_all=True` so the container's published port is reachable — the
default `127.0.0.1` bind only answers from inside the container.

```python title="main.py"
from veloce import Veloce

app = Veloce()


@app.get("/")
async def index(request):
    return {"message": "Hello from Veloce"}


if __name__ == "__main__":
    app.run(host=None, port=8000, bind_all=True)
```

Build and run it:

```bash
docker build -t my-veloce-app .
docker run -p 8000:8000 my-veloce-app
```

!!! warning "The built-in server is for development"
    `app.run()` logs a startup reminder that its from-scratch HTTP server is
    intended for local development, not production traffic. For production,
    serve under uvicorn (below) or behind a reverse proxy. See
    [Run a server manually](manually.md) for the trade-offs.

!!! warning "`bind_all=True` exposes the server"
    `app.run()` defaults to `127.0.0.1`. Inside a container you must bind to
    `0.0.0.0` (`bind_all=True`) for the published port to work, but never
    combine that with `debug=True` on an internet-reachable host — debug
    tracebacks leak source and internals. Passing both `host=...` and
    `bind_all=True` raises `ValueError`.

## Running under uvicorn

For production, run the app under a hardened ASGI server. Install the
`uvicorn` extra and let uvicorn import the `app` object.

```dockerfile title="Dockerfile"
FROM python:3.12-slim

WORKDIR /app

RUN pip install --no-cache-dir "veloceframework[uvicorn]"

COPY . .

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
```

The `app` object is the same module-level `Veloce()` instance; uvicorn drives
it through Veloce's ASGI `__call__`, so no `if __name__ == "__main__"` block is
needed for this path.

```python title="main.py"
from veloce import Veloce

app = Veloce()


@app.get("/")
async def index(request):
    return {"message": "Hello from Veloce"}
```

!!! tip "One worker per container"
    Prefer a single uvicorn worker per container and scale by running more
    containers — the orchestrator (Kubernetes, ECS, Compose) handles process
    supervision and restarts. Veloce keeps rate-limit buckets and in-memory
    caches per process, so multiple workers split that state. See
    [Server workers](workers.md) for the per-worker state table.

## Exec form vs shell form

Always write `CMD` (and `ENTRYPOINT`) in **exec form** — a JSON array — not
shell form.

```dockerfile
# Exec form: PID 1 is python/uvicorn directly. Signals reach the server.
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
```

```dockerfile
# Shell form: PID 1 is /bin/sh, which does not forward SIGTERM. Avoid.
CMD uvicorn main:app --host 0.0.0.0 --port 8000
```

Exec form makes the server process PID 1, so `docker stop`'s `SIGTERM` reaches
it directly and the ASGI lifespan `shutdown` event (and Veloce's shutdown
hooks) runs before the container exits. Shell form wraps the command in
`/bin/sh -c`, which does not forward signals, so the container is killed
ungracefully after the stop timeout.

## Behind a load balancer

A container almost always sits behind a reverse proxy or cloud load balancer
that terminates TLS and forwards over HTTP/1.1. Add
[`ProxyFix`](../guide/middleware.md) so the app trusts the proxy's
`X-Forwarded-*` headers — without it, `request.client.host` is the proxy's IP
and `request.url` reports `http`, breaking client-IP logging and any
`https`-scheme redirect.

```python title="main.py"
from veloce import ProxyFix, Veloce

app = Veloce()

# One trusted proxy hop in front: trust the last X-Forwarded-For / -Proto entry.
app.add_middleware(ProxyFix, x_for=1, x_proto=1)


@app.get("/whoami")
async def whoami(request):
    return {"client": request.client.host, "scheme": request.url.scheme}


if __name__ == "__main__":
    app.run(host=None, port=8000, bind_all=True)
```

Each `x_*` count is the number of trusted hops, read right-to-left. Set a field
to `0` to ignore that header entirely.

| Argument | Default | Purpose |
| --- | --- | --- |
| `x_for` | `1` | Trusted `X-Forwarded-For` hops; resolves `request.client.host`. |
| `x_proto` | `1` | Trusted `X-Forwarded-Proto` hops; resolves `request.url.scheme`. |
| `x_host` | `0` | Trusted `X-Forwarded-Host` hops; resolves the request host. |
| `x_port` | `0` | Trusted `X-Forwarded-Port` hops; fills the public port when the host carries none. |
| `x_prefix` | `0` | Trusted `X-Forwarded-Prefix` hops; resolves the mount prefix. |

!!! warning "Match the hop count to your topology"
    `ProxyFix` trusts these headers unconditionally — set each count to the
    exact number of proxies you control. If a client can reach the app
    directly (not only through the load balancer), a spoofed `X-Forwarded-For`
    is accepted as the real client IP. Trust forwarded headers only when every
    request is guaranteed to pass through your proxy layer.

Two trusted load-balancer hops forwarding client IP and scheme would be
`app.add_middleware(ProxyFix, x_for=2, x_proto=2)`.

## Testing the container behaviour

The `ProxyFix` behaviour is verifiable in-process with the
[`TestClient`](../guide/testing.md) by sending the headers a load balancer
would add.

```python
from veloce import ProxyFix, TestClient, Veloce

app = Veloce()
app.add_middleware(ProxyFix, x_for=1, x_proto=1)


@app.get("/whoami")
async def whoami(request):
    return {"client": request.client.host, "scheme": request.url.scheme}


client = TestClient(app)

resp = client.get(
    "/whoami",
    headers={"X-Forwarded-For": "203.0.113.7", "X-Forwarded-Proto": "https"},
)
assert resp.status_code == 200
assert resp.json() == {"client": "203.0.113.7", "scheme": "https"}
```

## Next steps

- [Run a server manually](manually.md) — uvicorn, `veloce run`, and `app.run()` compared.
- [Server workers](workers.md) — multi-process serving and the per-worker state table.
- [HTTPS concepts](https.md) — TLS termination at the proxy or load balancer.
- [Behind a proxy](../guide/behind-a-proxy.md) — `ProxyFix`, `root_path`, and mount prefixes in depth.
- Full signatures are in the [API reference](../reference.md).
```