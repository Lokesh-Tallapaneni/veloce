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

## Veloce middleware vs ASGI middleware

`add_middleware` accepts two distinct shapes, and it tells them apart by
what the class subclasses:

- A [`Middleware`](../reference/middleware.md#veloce.Middleware) subclass (or
  instance) is **Veloce-native middleware**. It defines
  `process_request(request)` and/or `process_response(request, response)`
  and runs inside Veloce's own pipeline, with access to the parsed
  [`Request`](../reference/requests.md#veloce.Request) and per-route
  [exclusion](#excluding-middleware-per-route). Every built-in in the
  [table below](#built-in-middleware) is this shape.
- Any other class is treated as a **standard ASGI middleware**: Veloce
  constructs it as `MiddlewareClass(app, **options)` when the ASGI stack
  is assembled, so it wraps the whole application at the scope/receive/send
  level. This is the seam for third-party ASGI middleware — tracing,
  profiling, observability — that expects to wrap an ASGI app.

```python hl_lines="14 18"
from veloce import Middleware, Request, Response, Veloce

app = Veloce()


# Veloce-native: split request/response hooks.
class StampMiddleware(Middleware):
    async def process_response(self, request: Request, response: Response) -> Response:
        response.headers["X-Stamped"] = "1"
        return response


app.add_middleware(StampMiddleware)

# ASGI: a class taking (app, **options); Veloce passes the wrapped app in.
# `SomeTracingMiddleware` here stands in for any third-party ASGI middleware.
app.add_middleware(SomeTracingMiddleware, service_name="api")
```

!!! note
    Veloce-native middleware runs against the parsed `Request`/`Response`,
    so it is the right place for almost everything. Reach for an ASGI
    middleware class only when you are plugging in a third-party component
    that is already written to the ASGI interface.

!!! warning "`BaseHTTPMiddleware` goes through `add_http_middleware`"
    A [`BaseHTTPMiddleware`](#class-based-middleware) subclass is a
    dispatch-shape middleware, not an ASGI app. Passing one to
    `add_middleware` raises `TypeError` — register it with
    [`add_http_middleware`](#class-based-middleware) instead.

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

```python hl_lines="4"
app.add_middleware(
    CORSMiddleware(
        allow_origins=["https://app.example.com"],
        allow_private_network=True,
    )
)
```

For the full parameter table — `allow_origins`, `allow_origin_regex`,
`allow_methods`, `allow_headers`, `allow_credentials`, `expose_headers`,
`max_age` — and the credentials/wildcard rule, see [CORS](cors.md).

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
see [Sessions](sessions.md). Cookie attributes are constructor arguments
(`secure=`, `httponly=`, `samesite=`, `cookie_name=`), not `app.config` keys.

!!! warning "`SESSION_COOKIE_*` keys are retired and stop startup"

    Setting `SESSION_COOKIE_NAME`, `SESSION_COOKIE_SECURE`,
    `SESSION_COOKIE_HTTPONLY` or `SESSION_COOKIE_SAMESITE` now raises
    `AuditFailed` at startup rather than being ignored. Pass the value to the
    middleware instead — see [Sessions](sessions.md).

### Trusted hosts

`TrustedHostMiddleware` validates the `Host` header against an allow-list and
rejects anything else with a `400` — defence against Host-header injection. It
takes a single positional `allowed_hosts` list and supports literal names, the
catch-all `*`, and subdomain wildcards like `*.example.com` (which match
`api.example.com` but never the bare `example.com`):

```python
from veloce import TrustedHostMiddleware, Veloce

app = Veloce()

app.add_middleware(
    TrustedHostMiddleware(allowed_hosts=["example.com", "*.example.com"])
)
```

!!! warning "No `www_redirect`"
    Unlike some other frameworks, `TrustedHostMiddleware` does not redirect a
    bare apex host to its `www.` form — there is no `www_redirect` option. The
    middleware only allows or rejects; to canonicalise a host, add an explicit
    redirect in a handler or a `before_request` hook.

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

Read the nonce inside a handler with
[`csp_nonce(request)`](../reference/middleware.md#veloce.csp_nonce), or write
`{{ csp_nonce }}` straight into a template, and place it on the matching
`<script>`/`<style>` tags as `nonce="..."`:

```html
<script nonce="{{ csp_nonce }}">
  console.log("this one runs");
</script>
```

The nonce is materialised lazily on first read, so a request that never
embeds one pays no extra cost.

!!! warning "A missing nonce fails silently"
    A `<script>` whose `nonce` does not match the policy is refused by the
    browser with no server-side signal - no error in the response, the log,
    or the test suite, since `TestClient` does not enforce CSP. If inline
    scripts stop running, check the rendered `nonce=""` attribute first. A static, nonce-free policy can stay on
`SecurityHeadersMiddleware`; use `CSPMiddleware` when you need a nonce or a
report-only policy.

### Conditional GET

`ConditionalGetMiddleware` evaluates `If-None-Match` / `If-Modified-Since`
against a buffered `GET`/`HEAD` response and downgrades a matching request
to `304 Not Modified` with an empty body
([RFC 9110 §13](https://www.rfc-editor.org/rfc/rfc9110#section-13)). With
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

### When compression uses the thread pool

The same threshold decides both halves of the middleware: a buffered body below
it is compressed inline, and one at or above it is offloaded, exactly as a
streamed chunk is.

Offloading is not free. It costs a handoff per response, and under load every
compressing request queues on the same pool. For a small body that handoff
costs more than the compression, so compressing inline is faster even though it
holds the event loop — the hold is brief, and the pool contention it avoids is
not. Past roughly 48 KiB the balance inverts: zlib releases the GIL for long
enough that the pool genuinely parallelises, and keeping the loop free wins.
The default sits below that crossover so the offload engages before inline
becomes the slower choice.

Lower `min_stream_chunk_offload` to push more work to the pool, or raise it to
keep more inline:

```python
from veloce import GZipMiddleware, Veloce

app = Veloce()
app.add_middleware(GZipMiddleware(min_stream_chunk_offload=8 * 1024))
```

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

To give a specific route its own limit, decorate its handler with `rate_limit`.
The limit lives on the handler, so there is no route string to mistype:

```python
from veloce import RateLimitMiddleware, TokenBucket, rate_limit

app.add_middleware(RateLimitMiddleware(strategy=TokenBucket(rate=1000, per=60)))


@app.post("/login")
@rate_limit(TokenBucket(rate=5, per=60))  # stricter, just for this route
async def login(request):
    ...
```

Put `@rate_limit` **below** the route decorator so the route registers the tagged
handler. A decorated route gets its own per-client counter, independent of the
default budget.

For handlers you cannot decorate, the `overrides` map is the central alternative.
Its key is the route's **full** path template as matched at runtime (the value of
`request.url_rule`), so a route on a blueprint with `url_prefix="/api"` uses
`"/api/login"`, not `"/login"`:

```python
from veloce import RateLimitMiddleware, TokenBucket

app.add_middleware(
    RateLimitMiddleware(
        strategy=TokenBucket(rate=1000, per=60),
        overrides={"/api/login": TokenBucket(rate=5, per=60)},
    )
)
```

An override key that matches no registered route raises on the first request, so
a wrong prefix fails loudly rather than silently doing nothing. An explicit
`overrides` entry wins over a `@rate_limit` tag on the same route.

!!! note "Per-route limits resolve against the entry route"
    Like `exclude_middleware`, a per-route limit is resolved against the route
    matched when the request arrives. A `before_request` hook that rewrites the
    path to a different route does not change which limit applies — rate limiting
    runs before those hooks.

!!! note "Added in version 0.4.0"
    Selectable `strategy`/`backend`, the `rate_limit` per-route decorator, and the
    per-route `overrides` map on `RateLimitMiddleware`. The bare
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

That ordering covers the middleware registered with `add_middleware()`.
Function middleware registered with `@app.middleware("http")` is a separate
layer that always wraps the whole `add_middleware()` chain, so its position in
your source file relative to an `add_middleware()` call does not change the
result:

```python
from veloce import Middleware, Veloce

app = Veloce()


class Stamp(Middleware):
    async def process_response(self, request, response):
        response.headers["X-Stamp"] = "1"
        return response


@app.middleware("http")
async def outer(request, call_next):
    # Runs before Stamp on the way in, and after it on the way out —
    # regardless of whether this decorator appears above or below
    # `app.add_middleware(Stamp)`.
    return await call_next(request)


app.add_middleware(Stamp)
```

!!! note
    Because the two registration styles form separate layers, a function
    middleware always sees the response *after* every `add_middleware()`
    middleware has processed it. Register both halves of a cooperating pair in
    the same style if their relative order matters.

## Short-circuiting the request

`process_request` returns `None` to let the request carry on. Returning a
[`Response`](../reference/responses.md#veloce.Response) instead **short-circuits** the
pipeline: the handler never runs, and neither does the `process_request` of any
middleware added after this one. The returned response still travels back out
through the response phase.

```python
from veloce import JSONResponse, Middleware, Request, Response, Veloce

app = Veloce()


class APIKeyMiddleware(Middleware):
    async def process_request(self, request: Request) -> Response | None:
        if request.headers.get("X-API-Key") != "expected-key":
            return JSONResponse({"detail": "Unauthorized"}, status_code=401)
        return None  # carry on to the handler


app.add_middleware(APIKeyMiddleware)
```

This is how every built-in that rejects a request before it reaches your code
works — `CSRFMiddleware`, `RateLimitMiddleware`, `TrustedHostMiddleware`, and
`CORSMiddleware`'s preflight reply all short-circuit from `process_request`.

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

- [CORS](cors.md) — the full `CORSMiddleware` parameter reference.
- [Sessions](sessions.md) — `SessionMiddleware` and `ServerSessionMiddleware`.
- [Configuration](configuration.md) — `SECRET_KEY` and the app-level defaults.
- [Deployment](deployment.md)
- [Routing](routing.md)
- [Dependency injection](dependency-injection.md)
- The [API reference](../reference/index.md) lists every middleware class.
