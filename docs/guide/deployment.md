# Deployment

## The built-in server is for development

`app.run()` starts Veloce's own HTTP server. It is convenient for local
development — one call, no extra dependency — but it is **not** intended
to face production traffic, and `run()` logs a reminder to that effect
on startup.

For production, run the app under a hardened ASGI server. Veloce is a
plain ASGI application (it implements `__call__(scope, receive, send)`),
so any ASGI server works:

```bash
uvicorn your_module:app --host 0.0.0.0 --port 8000 --workers 4
```

Uvicorn (or Hypercorn, Granian, …) brings battle-tested HTTP/1.1 and
WebSocket handling, worker management, graceful reloads, and TLS. The
built-in server covers the development inner loop; production should not
depend on it.

### What the built-in server does and doesn't do

The development server **does** apply a slowloris guard — once a
request's bytes start arriving, the whole request must complete within
`HttpProtocol.REQUEST_TIMEOUT` seconds (default 30) or the connection is
dropped with `408` — and an idle keep-alive timeout.

It **does not** implement fragmented (continuation-frame) WebSocket
messages or HTTP/2. Production WebSocket and HTTP/2 workloads should run
under uvicorn, which implements both. This is a deliberate scope line:
hardening a from-scratch production server is not the project's goal
when mature ASGI servers already exist.

## Running with multiple workers

`uvicorn --workers N` (or several `app.run()` processes) forks **N
independent processes**. They share no Python memory, which has a direct
consequence for any state Veloce keeps in-process:

| State | Shared across workers? | Notes |
|-------|------------------------|-------|
| Signed session cookies (`SessionMiddleware`) | Yes | The session lives in the client's cookie; any worker can verify it with the shared `secret`. Safe across workers. |
| `RateLimitMiddleware` buckets | **No** | Each worker counts only the requests it served, so the effective limit is `N ×` the configured value. |
| `g` / `request.state` | n/a | Per-request, never shared — correct by construction. |
| `app.state` / `app.config` | **No** (after fork) | Mutating `app.state` at runtime affects only the worker that did it. |
| `Veloce.mount`-ed in-memory data, module globals | **No** | Per process. |

Guidance:

- **Sessions** — signed cookies are stateless, so multi-worker is fine.
  If you need server-side, revocable sessions, back them with a shared
  store (Redis, a database). A pluggable backend is on the roadmap.
- **Rate limiting** — `RateLimitMiddleware` is in-memory and therefore
  per-worker. For an accurate global limit across workers, put the
  limiter in a reverse proxy (nginx `limit_req`) or back it with Redis.
- **Any in-memory cache or counter** — assume it is per-worker. Move
  anything that must be globally consistent into an external store.

A single-worker deployment behind a reverse proxy sidesteps all of the
above; scale out with more workers only once shared state is externalised.
