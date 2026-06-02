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

## See also

- [Sessions](sessions.md) — `SessionMiddleware` and `ServerSessionMiddleware`.
- [Configuration](configuration.md) — the `SESSION_COOKIE_*` keys.
- [Deployment](deployment.md)
- [Routing](routing.md)
- [Dependency injection](dependency-injection.md)
- The [API reference](../reference.md) lists every middleware class.
