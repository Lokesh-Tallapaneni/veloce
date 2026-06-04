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
| `RateLimitMiddleware`       | In-process token-bucket rate limiter                          |
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
