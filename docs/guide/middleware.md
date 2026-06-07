---
description: Add CORS, GZip, CSRF, sessions, rate limiting, security headers, and trusted-host middleware to a Veloce app — function-based or class-based, ordered LIFO.
tags: [middleware, cors, csrf, security]
---

# Middleware

Middleware wraps the request/response cycle — it runs before a handler
sees the request and after it produces a response. Use it for
cross-cutting concerns: CORS, compression, security headers, logging.

## Adding middleware

`app.add_middleware()` accepts middleware in two forms — a configured
instance, or a class together with its keyword options:

```python
from veloce import CORSMiddleware, Veloce

app = Veloce()

# Instance form — build the middleware, then add it.
app.add_middleware(
    CORSMiddleware(
        allow_origins=["*"],
        allow_methods=["GET", "POST", "PUT", "DELETE"],
    )
)

# Class form — pass the class and its options; Veloce constructs it.
app.add_middleware(CORSMiddleware, allow_origins=["*"])
```

Middleware can also be passed when constructing the app, via the
`middleware=[...]` argument to `Veloce(...)`.

### CORS preflight and Private Network Access

`CORSMiddleware` answers a preflight (`OPTIONS` with an `Origin`) with a
`204`. A preflight whose `Origin` is not in the allow-list, or whose
`Access-Control-Request-Method` is not in `allow_methods`, gets a
diagnostic `400` instead of a silently-blocked `204` so the rejection is
visible to developers.

Set `allow_private_network=True` to participate in
[Private Network Access](https://wicg.github.io/private-network-access/):
when a preflight carries `Access-Control-Request-Private-Network: true`,
the response echoes `Access-Control-Allow-Private-Network: true`. The grant
is opt-in and never emitted unless configured.

```python
app.add_middleware(
    CORSMiddleware(
        allow_origins=["https://app.example.com"],
        allow_private_network=True,
    )
)
```

## Built-in middleware

Veloce ships the following middleware, all importable from the top-level
`veloce` package:

| Middleware                  | Purpose                                                       |
|-----------------------------|---------------------------------------------------------------|
| `CORSMiddleware`            | Cross-Origin Resource Sharing                                 |
| `GZipMiddleware`            | Response compression                                          |
| `CSRFMiddleware`            | Double-submit-cookie CSRF protection                          |
| `SessionMiddleware`         | Signed, timestamped session cookies                           |
| `ServerSessionMiddleware`   | Server-side sessions; the cookie carries only an opaque id    |
| `TrustedHostMiddleware`     | Host-header allow-list                                        |
| `HTTPSRedirectMiddleware`   | Redirect plain HTTP to HTTPS                                  |
| `SecurityHeadersMiddleware` | Attach common hardening response headers to every response    |
| `CSPMiddleware`             | Content-Security-Policy with a per-request nonce and report-only support |
| `ConditionalGetMiddleware`  | Emit `304 Not Modified` for satisfied GET/HEAD preconditions  |
| `RateLimitMiddleware`       | Per-client rate limiter with a selectable algorithm and backend |
| `WebSocketOriginMiddleware` | Reject cross-site WebSocket handshakes (CSWSH)                |
| `LoggingMiddleware`         | Structured request/response access logging                    |
| `RequestIDMiddleware`       | Assign a unique request ID and echo it in the response        |
| `ProxyFix`                  | Honour `X-Forwarded-*` from trusted proxies                   |

The base classes `Middleware` and [`BaseHTTPMiddleware`](#class-based-middleware)
are also exported, along with the `rotate_csrf_token` helper used with
`CSRFMiddleware`.

`SessionMiddleware` and `ServerSessionMiddleware` have a dedicated guide —
see [Sessions](sessions.md). For configuring cookie attributes through
`app.config`, see [Configuration](configuration.md#built-in-defaults).

### Content-Security-Policy with a nonce

`CSPMiddleware` emits a `Content-Security-Policy` (and/or
`Content-Security-Policy-Report-Only`) header, optionally with a fresh
per-request nonce. Pass `policy` as a string template containing the
literal `{nonce}` placeholder, or as a directive mapping where the
`'nonce'` source is substituted with the generated nonce:

```python
from veloce import CSPMiddleware

app.add_middleware(
    CSPMiddleware(
        policy={"default-src": "'self'", "script-src": ["'self'", "'nonce'"]},
        report_only_policy="default-src 'self'",
    )
)
```

Read the nonce inside a handler or template with
[`csp_nonce(request)`](../reference.md#veloce.csp_nonce) and place it on the
matching `<script>`/`<style>` tags as `nonce="..."`. The nonce is
materialised lazily on first read, so a request that never embeds one pays
no extra cost. A static, nonce-free policy can stay on
`SecurityHeadersMiddleware`; use `CSPMiddleware` when you need a nonce or a
report-only policy.

### Conditional GET

`ConditionalGetMiddleware` evaluates `If-None-Match` / `If-Modified-Since`
against a buffered `GET`/`HEAD` response and downgrades a matching request
to `304 Not Modified` with an empty body (RFC 9110 Sec. 13). With
`auto_etag` (the default) it also synthesises a weak `ETag` for a buffered,
non-empty `200` that lacks one. Register it **after** `GZipMiddleware` so a
synthesised ETag reflects the compressed bytes:

```python
from veloce import ConditionalGetMiddleware, GZipMiddleware

app.add_middleware(GZipMiddleware())
app.add_middleware(ConditionalGetMiddleware())
```

`StreamingResponse` bodies are not buffered for ETag synthesis.

### Streaming compression

`GZipMiddleware` also compresses streaming responses chunk-by-chunk
through a single deflate stream, so a long-running streamed body no longer
has to be buffered to be compressed. Chunks at or above
`min_stream_chunk_offload` bytes (32 KiB by default) are offloaded to the
thread pool; latency-sensitive types (`text/event-stream` by default, via
`latency_sensitive_types`) are passed through uncompressed so server-sent
events are never merged or delayed.

### Rate limiting

`RateLimitMiddleware` limits requests per client. Used with no arguments it runs
a process-local sliding-log limiter — `max_requests` per `window_seconds`:

```python
from veloce import RateLimitMiddleware

app.add_middleware(RateLimitMiddleware(max_requests=100, window_seconds=60))
```

Pass a `strategy` to choose the algorithm, and a `backend` to choose where the
per-client state lives:

```python
from veloce import RateLimitMiddleware, TokenBucket

app.add_middleware(RateLimitMiddleware(strategy=TokenBucket(rate=100, per=60, burst=20)))
```

| Strategy        | Behavior                                                                 |
| --------------- | ------------------------------------------------------------------------ |
| `FixedWindow`   | `limit` per fixed `window`; cheapest, but allows a burst at the boundary  |
| `SlidingWindow` | `limit` per rolling `window`; smooths the boundary burst with two counters |
| `TokenBucket`   | refills `rate` per `per` seconds, allowing a burst up to `burst` (default `rate`); `burst=1` is a strict leaky bucket |

The default `InMemoryRateLimitBackend` counts per process. For one limit shared
across every worker and host, use `RedisRateLimitBackend` (see below).

!!! note "Added in version 0.4.0"
    Selectable `strategy`/`backend` on `RateLimitMiddleware`. The bare
    `max_requests`/`window_seconds` form is unchanged.

## Function middleware

For one-off logic, register a function with `@app.middleware("http")`.
It receives the request and a `call_next` callable that runs the rest of
the stack:

```python
@app.middleware("http")
async def add_timing_header(request, call_next):
    response = await call_next(request)
    response.headers["X-Powered-By"] = "veloce"
    return response
```

## Class-based middleware

For reusable middleware, subclass `BaseHTTPMiddleware` and implement
`dispatch`:

```python
from veloce import BaseHTTPMiddleware


class RequestIDMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        response = await call_next(request)
        response.headers["X-Request-ID"] = new_id()
        return response


app.add_http_middleware(RequestIDMiddleware())
```

## Ordering

Middleware runs in the order it is added on the way in, and in reverse
on the way out — the first one added is the outermost layer.

## Excluding middleware per route

A route can opt out of named middleware with `exclude_middleware`. Each entry
is matched against a middleware's name, which defaults to its class name; pass
`name=` to the middleware when two instances of the same class must be
addressed independently. The opt-out applies to both the request and response
phases, so a skipped middleware never runs for that route at all.

The exclusion set is keyed on the route matched at dispatch entry. The same set
of middleware that runs `process_request` runs `process_response`, so setup and
teardown stay balanced. A `before_request` hook that rewrites the request path
to a different route does not change which middleware run for that request - the
entry route's `exclude_middleware` is authoritative.

!!! warning "RateLimitMiddleware counts per process by default"
    The default `InMemoryRateLimitBackend` keeps its state in one process, so
    under `uvicorn --workers N` the effective limit is roughly `N x` the
    configured one. For a shared cross-worker limit pass a
    [`RedisRateLimitBackend`](databases.md#redis-sessions-and-rate-limiting) from
    `veloce.contrib.redis` (`pip install veloceframework[redis]`), which keeps
    the state in Redis:

    ```python
    from redis.asyncio import Redis

    from veloce import RateLimitMiddleware, TokenBucket
    from veloce.contrib.redis import RedisRateLimitBackend

    client = Redis.from_url("redis://localhost:6379/0")
    app.add_middleware(
        RateLimitMiddleware(
            strategy=TokenBucket(rate=100, per=60),
            backend=RedisRateLimitBackend(client),
        )
    )
    ```

```python
app.add_middleware(CSRFMiddleware(secret="..."))
app.add_middleware(RateLimitMiddleware(max_requests=100, window_seconds=60))


# Inbound webhooks can't carry a CSRF token, and the health probe should
# never be rate limited.
@app.post("/webhooks/stripe", exclude_middleware=["CSRFMiddleware"])
async def stripe_webhook(request):
    ...


@app.get("/health", exclude_middleware=["RateLimitMiddleware"])
async def health():
    return {"status": "ok"}
```

This works on `@app.route`/`@app.get`/`@app.post`/… and the imperative
`add_api_route`, and on `Blueprint` and `Router` routes. Routes that declare no
exclusions run every registered middleware and pay no extra per-request cost.

## See also

- [Sessions](sessions.md) — `SessionMiddleware` and `ServerSessionMiddleware`.
- [Configuration](configuration.md) — the `SESSION_COOKIE_*` keys.
- [Deployment](deployment.md)
- [Routing](routing.md)
- [Dependency injection](dependency-injection.md)
- The [API reference](../reference.md) lists every middleware class.
