---
description: Run Veloce behind a reverse proxy with root_path and script_root for a stripped path prefix and ProxyFix to trust X-Forwarded-* headers by hop depth.
tags: [proxy, deployment, headers, security]
---

# Behind a proxy

When Veloce runs behind a reverse proxy (nginx, Caddy, an ALB, Cloudflare), the TCP peer is the proxy, not the original client, and the proxy may strip a path prefix before forwarding. [`ProxyFix`](../reference/middleware.md#veloce.ProxyFix) recovers the original client IP, scheme, host, and prefix from the proxy's forwarding headers, and `request.root_path` / `request.script_root` expose the mounted prefix. This page covers both.

## The two problems a proxy introduces

A reverse proxy changes what the application sees in two distinct ways. Each has its own fix:

| Symptom | What the proxy did | Fix |
| --- | --- | --- |
| `request.client_host` is the proxy IP, scheme is `http` behind TLS | Terminated the connection and re-issued the request | [`ProxyFix`](../reference/middleware.md#veloce.ProxyFix) reads `X-Forwarded-*` |
| The app is served under `/api` but routes are registered at `/` | Mounted the app under a prefix and stripped it | `root_path` / `script_root` |

## Trusting forwarded headers with ProxyFix

A proxy injects headers describing the original request. Add [`ProxyFix`](../reference/middleware.md#veloce.ProxyFix) and tell it how many hops to trust for each header.

```python title="app.py"
from veloce import ProxyFix, Request, Veloce

app = Veloce()
app.add_middleware(ProxyFix(x_for=1, x_proto=1, x_host=1))


@app.get("/whoami")
async def whoami(request: Request):
    return {
        "client": request.client_host,
        "scheme": request.url.scheme,
        "host": request.host,
    }
```

After the middleware runs:

- `request.client_host` returns the IP from `X-Forwarded-For` instead of the TCP peer.
- `request.url.scheme` reflects `X-Forwarded-Proto` (so redirects and `request.url` say `https` even though TLS was terminated upstream).
- `request.host` reflects `X-Forwarded-Host`.

!!! warning "Trust depth is a security boundary"
    The numbers count trusted proxies from the **right** (closest to the app). A client can spoof these headers; only the proxies you control are trustworthy. With two proxies in front of you, set `x_for=2` so the value attributed to the client is the one your *outermost* trusted proxy wrote. Setting trust too high lets a client forge its own IP or scheme.

### Hop counts per header

Each field independently selects the Nth value from the right of its header. `0` disables that header entirely; negative values raise `ValueError` at construction.

| Field | Header | Default | Purpose |
| --- | --- | --- | --- |
| `x_for` | `X-Forwarded-For` | `1` | Original client IP. |
| `x_proto` | `X-Forwarded-Proto` | `1` | Original scheme (`http`/`https`). |
| `x_host` | `X-Forwarded-Host` | `0` | Original Host header. |
| `x_port` | `X-Forwarded-Port` | `0` | Public port when the forwarded Host carries none. |
| `x_prefix` | `X-Forwarded-Prefix` | `0` | Stripped path prefix (feeds `script_root`). |

`X-Forwarded-Host`, `X-Forwarded-Port`, and `X-Forwarded-Prefix` default to `0` (off) — enable only the ones your proxy actually sets.

### The standard Forwarded header

[`ProxyFix`](../reference/middleware.md#veloce.ProxyFix) can also read the RFC 7239 `Forwarded` header, which supersedes the `X-Forwarded-*` set: when it is trusted, it is the sole authority for `for`, `proto` and `host`. That is off by default, because nginx, ALB and most CDNs emit `X-Forwarded-*` and leave `Forwarded` untouched — so a client could send one and decide its own address, scheme and host, silencing the headers your proxy does control.

Enable it only where every trusted proxy sets or sanitizes `Forwarded` itself:

```python
app.add_middleware(ProxyFix(x_for=2, x_proto=1, trust_forwarded=True))
```

!!! note "Changed in version 0.20.0"

    `trust_forwarded` defaults to `False`. A deployment behind a proxy that
    emits only `Forwarded` must now opt in.

!!! note "ProxyFix selects by trust depth, it does not authenticate"
    The middleware picks the value your trusted hop wrote; it does not verify the upstream chain cryptographically. Restrict who can reach the app directly (firewall, private network) so the only source of these headers is a proxy you operate. See [RFC 7239](https://www.rfc-editor.org/rfc/rfc7239) for the `Forwarded` grammar.

## A stripped prefix: root_path and script_root

When the app is mounted under a prefix — by an ASGI server (`uvicorn --root-path /api`), by `app.mount("/sub", inner_app)`, or by a proxy sending `X-Forwarded-Prefix` — the routes still register at `/`, but the public URL carries the prefix. Two request properties expose it:

```python title="app.py"
from veloce import Request, Veloce

app = Veloce()


@app.get("/info")
async def info(request: Request):
    return {
        "root_path": request.root_path,
        "script_root": request.script_root,
        "path": request.path,
    }
```

- `request.root_path` returns the ASGI `scope["root_path"]` — set by the server or by a mount. Empty string at root.
- `request.script_root` is the same value, **except** that a `ProxyFix`-trusted `X-Forwarded-Prefix` wins over the ASGI scope, because that prefix is the trusted outer-edge value when the ASGI server itself sits behind a proxy that strips it.

You can also set the prefix on the application directly:

```python
app = Veloce(root_path="/api")
```

### Combining a stripped prefix with ProxyFix

Enable `x_prefix` so the proxy's `X-Forwarded-Prefix` feeds `script_root`:

```python title="app.py"
from veloce import ProxyFix, Request, Veloce

app = Veloce()
app.add_middleware(ProxyFix(x_for=1, x_proto=1, x_host=1, x_prefix=1))


@app.get("/items")
async def items(request: Request):
    return {"mounted_under": request.script_root}
```

## url_for behind a proxy

[`url_for`](../reference/application.md#veloce.Veloce.url_for) reverse-resolves a route name to a path. It builds from the registered route template and does **not** prepend `root_path` or `script_root`, so a relative result is root-relative to the application, not to the public mount point.

```python title="app.py"
from veloce import Request, Veloce

app = Veloce()


@app.get("/items/{item_id:int}", name="item")
async def item(request: Request, item_id: int):
    return {"url": request.url_for("item", item_id=item_id)}
```

For absolute URLs, pass `_external=True`. Veloce uses `app.config["SERVER_NAME"]` and `app.config["PREFERRED_URL_SCHEME"]` (or override with `_scheme=` / `_host=`):

```python
app.config["SERVER_NAME"] = "example.com"
app.config["PREFERRED_URL_SCHEME"] = "https"
# url_for("item", item_id=7, _external=True) -> "https://example.com/items/7"
```

!!! warning "url_for does not add the proxy prefix"
    Behind a stripped prefix, `url_for("item", item_id=7)` returns `/items/7`, not `/api/items/7`. To produce public links that include the mount prefix, prepend `request.script_root` yourself, or set `_host=` to the public host for absolute URLs. Veloce does not splice the prefix into reversed URLs automatically.

## Testing it

Drive the forwarding headers through [`TestClient`](../reference/testing.md#veloce.TestClient) and assert the recovered values.

```python
from veloce import ProxyFix, Request, TestClient, Veloce

app = Veloce()
app.add_middleware(ProxyFix(x_for=1, x_proto=1, x_host=1))


@app.get("/whoami")
async def whoami(request: Request):
    return {
        "client": request.client_host,
        "scheme": request.url.scheme,
        "host": request.host,
    }


client = TestClient(app)

resp = client.get(
    "/whoami",
    headers={
        "X-Forwarded-For": "203.0.113.7",
        "X-Forwarded-Proto": "https",
        "X-Forwarded-Host": "example.com",
    },
)
assert resp.status_code == 200
assert resp.json() == {
    "client": "203.0.113.7",
    "scheme": "https",
    "host": "example.com",
}
```

## Next steps

- [Middleware](middleware.md) — where `ProxyFix` sits in the request pipeline and how to order it.
- [Configuration](configuration.md) — set `SERVER_NAME` and `PREFERRED_URL_SCHEME` for absolute `url_for`.
- [Deployment](deployment.md) — run the app behind a real proxy in production.
- Full signatures are in the [API reference](../reference/index.md).
