---
description: Enable Cross-Origin Resource Sharing in Veloce with CORSMiddleware — allow_origins, allow_methods, allow_headers, allow_credentials, expose_headers, max_age, preflight handling, and Private Network Access.
tags: [cors, middleware, security, browser]
---

# CORS

A browser blocks JavaScript on one origin from reading a response served by
another unless that response opts in with CORS headers.
[`CORSMiddleware`](../reference.md#veloce.CORSMiddleware) adds those headers and
answers the preflight `OPTIONS` request the browser sends first.

It is
implemented from the [WHATWG Fetch CORS protocol](https://fetch.spec.whatwg.org/#http-cors-protocol)
and ships with Veloce — no install beyond the framework.

## Enabling CORS

Add the middleware with the origins your front-end is served from. Anything not
in the list is denied.

```python title="app.py"
from veloce import CORSMiddleware, Veloce

app = Veloce()

app.add_middleware(
    CORSMiddleware(
        allow_origins=["https://app.example.com"],
        allow_methods=["GET", "POST"],
    )
)


@app.get("/items")
async def items():
    return [{"id": 1}]
```

A response to a request from `https://app.example.com` now carries
`Access-Control-Allow-Origin: https://app.example.com`, so the browser lets the
front-end read it.

!!! note
    The middleware reads the request's `Origin` header. A same-origin request
    (or any request without `Origin`, such as `curl`) carries no `Origin` and is
    unaffected — CORS only governs cross-origin browser fetches.

## Parameters

Every option is a keyword argument to `CORSMiddleware(...)`.

| Parameter | Default | Purpose |
| --- | --- | --- |
| `allow_origins` | `["*"]` | Exact origins allowed to read responses. `["*"]` allows any. |
| `allow_origin_regex` | `None` | A regex matched in full against the request `Origin`, as an alternative to listing every origin. |
| `allow_methods` | `GET, POST, PUT, DELETE, PATCH, OPTIONS` | Methods echoed in `Access-Control-Allow-Methods` and accepted on preflight. |
| `allow_headers` | `["*"]` | Request headers echoed in `Access-Control-Allow-Headers`. `["*"]` allows any. |
| `allow_credentials` | `False` | When `True`, emits `Access-Control-Allow-Credentials: true` so the browser may send cookies and HTTP auth. |
| `allow_private_network` | `False` | Opt in to [Private Network Access](#private-network-access) preflights. |
| `max_age` | `600` | Seconds the browser may cache the preflight result, sent as `Access-Control-Max-Age`. |
| `expose_headers` | `[]` | Response headers the front-end JavaScript is allowed to read, sent as `Access-Control-Expose-Headers`. |
| `name` | `None` | Override the middleware name used by per-route `exclude_middleware`. |

### Matching origins with a regex

When the allowed origins follow a pattern — every subdomain, or per-PR preview
deploys — pass `allow_origin_regex` instead of enumerating them. The pattern
must match the whole origin.

```python
app.add_middleware(
    CORSMiddleware(
        allow_origin_regex=r"https://.*\.example\.com",
    )
)
```

### Exposing response headers

By default the browser only exposes a small set of simple response headers to
JavaScript. List any custom header the front-end needs to read in
`expose_headers`.

```python
app.add_middleware(
    CORSMiddleware(
        allow_origins=["https://app.example.com"],
        expose_headers=["X-Request-ID", "X-Total-Count"],
    )
)
```

## Credentials and the wildcard rule

To let the browser send cookies or `Authorization` on cross-origin requests, set
`allow_credentials=True`. The Fetch standard forbids combining credentials with
a wildcard, so Veloce echoes the exact request `Origin` (never `*`) and adds
`Vary: Origin` so caches key the response per origin.

```python
app.add_middleware(
    CORSMiddleware(
        allow_origins=["https://app.example.com"],
        allow_credentials=True,
    )
)
```

!!! warning "Credentials cannot be combined with a wildcard"
    `allow_credentials=True` with `allow_origins=["*"]`, `allow_headers=["*"]`,
    or a wildcard `allow_origin_regex` (`.*` and equivalents) raises a
    `ValueError` at construction (Fetch CORS spec Sec. 3.2.4). List the concrete
    origins you trust instead.

## How preflight is answered

For any request that is not a simple `GET`/`POST` — a custom header, a `PUT`, a
JSON content type — the browser first sends an `OPTIONS` preflight carrying
`Access-Control-Request-Method` and `Access-Control-Request-Headers`. The
middleware answers it directly, before the request reaches a handler:

- A preflight whose `Origin` is allowed and whose requested method is in
  `allow_methods` gets a `204 No Content` with the negotiated
  `Access-Control-Allow-*` headers and `Access-Control-Max-Age`.
- The requested headers are intersected against `allow_headers` (or echoed
  verbatim when `allow_headers=["*"]`) and returned in
  `Access-Control-Allow-Headers`.
- A preflight from a disallowed origin, or for a method not in `allow_methods`,
  gets a diagnostic `400` instead of a silently-blocked `204`, so the rejection
  is visible to you in the network panel.

!!! warning "Schemes extract, they do not verify — a 400 is for diagnostics"
    The browser blocks a failed preflight either way. The `400` exists so the
    cause is visible during development; do not rely on its body in client code.

## Private Network Access

A public-origin page that fetches a private host (a LAN address, `localhost`)
triggers a [Private Network Access](https://wicg.github.io/private-network-access/)
preflight carrying `Access-Control-Request-Private-Network: true`. The grant is
opt-in: only when `allow_private_network=True` does the response echo
`Access-Control-Allow-Private-Network: true`. It is never emitted otherwise.

```python
app.add_middleware(
    CORSMiddleware(
        allow_origins=["https://app.example.com"],
        allow_private_network=True,
    )
)
```

## Testing CORS

Drive the in-memory [`TestClient`](../reference.md#veloce.TestClient) with an
`Origin` header to inspect the headers, and send an `OPTIONS` with
`Access-Control-Request-Method` to exercise the preflight.

```python
from veloce import CORSMiddleware, TestClient, Veloce

app = Veloce()
app.add_middleware(
    CORSMiddleware(
        allow_origins=["https://app.example.com"],
        allow_methods=["GET", "POST"],
    )
)


@app.get("/items")
async def items():
    return [{"id": 1}]


client = TestClient(app)

# A cross-origin GET is annotated with the allow-origin header.
resp = client.get("/items", headers={"Origin": "https://app.example.com"})
assert resp.status_code == 200
assert resp.headers["Access-Control-Allow-Origin"] == "https://app.example.com"

# The preflight is answered with 204 and the negotiated method set.
preflight = client.options(
    "/items",
    headers={
        "Origin": "https://app.example.com",
        "Access-Control-Request-Method": "POST",
    },
)
assert preflight.status_code == 204
assert "POST" in preflight.headers["Access-Control-Allow-Methods"]

# A disallowed origin gets a diagnostic 400 on preflight.
denied = client.options(
    "/items",
    headers={
        "Origin": "https://evil.example.com",
        "Access-Control-Request-Method": "POST",
    },
)
assert denied.status_code == 400
```

## Next steps

- See the other built-in middleware and ordering rules in [Middleware](middleware.md).
- Reject cross-site WebSocket handshakes with `WebSocketOriginMiddleware` — see [WebSockets](websockets.md).
- Lock down host headers and add hardening headers in [Security schemes](security-schemes.md).
- Full signatures are in the [API reference](../reference.md).
