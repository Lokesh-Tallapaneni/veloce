# Middleware

Middleware wraps the request/response cycle — it runs before a handler
sees the request and after it produces a response. Use it for
cross-cutting concerns: CORS, compression, security headers, logging.

## Adding middleware

`app.add_middleware()` accepts a configured middleware instance:

```python
from veloce import CORSMiddleware, Veloce

app = Veloce()

app.add_middleware(
    CORSMiddleware(
        allow_origins=["*"],
        allow_methods=["GET", "POST", "PUT", "DELETE"],
        allow_credentials=True,
    )
)
```

Middleware can also be passed when constructing the app, via the
`middleware=[...]` argument to `Veloce(...)`.

## Built-in middleware

| Middleware                | Purpose                                        |
|---------------------------|------------------------------------------------|
| `CORSMiddleware`          | Cross-Origin Resource Sharing                  |
| `GZipMiddleware`          | Response compression                           |
| `CSRFMiddleware`          | Double-submit-cookie CSRF protection           |
| `SessionMiddleware`       | Signed, timestamped session cookies            |
| `TrustedHostMiddleware`   | Host-header allow-list                         |
| `HTTPSRedirectMiddleware` | Redirect plain HTTP to HTTPS                   |
| `ProxyFixMiddleware`      | Honour `X-Forwarded-*` from trusted proxies    |

All are importable from the top-level `veloce` package.

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


app.add_middleware(RequestIDMiddleware())
```

## Ordering

Middleware runs in the order it is added on the way in, and in reverse
on the way out — the first one added is the outermost layer.
