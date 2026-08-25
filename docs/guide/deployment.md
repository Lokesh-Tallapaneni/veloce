---
description: Deploy Veloce in production with uvicorn or gunicorn workers, security hardening checklist, ReDoS protection, and a comparison with the built-in development server.
tags: [deployment, production, uvicorn]
---

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
`REQUEST_TIMEOUT` seconds (default 30) or the connection is dropped with
`408` — and an idle keep-alive timeout that closes a connection after
`KEEP_ALIVE_TIMEOUT` seconds (default 75) with no request.

Both timeouts are tunable through `app.config`; the values above are the
defaults (the same numbers are exposed as the `HttpProtocol.REQUEST_TIMEOUT`
and `HttpProtocol.KEEP_ALIVE_TIMEOUT` class attributes):

```python
from veloce import Veloce

app = Veloce()
app.config["REQUEST_TIMEOUT"] = 15     # drop a half-sent request after 15s
app.config["KEEP_ALIVE_TIMEOUT"] = 30  # close an idle connection after 30s
app.config["MAX_CONCURRENT_CONNECTIONS"] = 1000  # reject beyond this (default 1000)
```

!!! note "Concurrent-connection cap"
    The built-in server admits at most `MAX_CONCURRENT_CONNECTIONS` simultaneous
    connections (default **1000**) and rejects further ones with `503`
    immediately rather than queueing them — a deliberate memory guard. Raise it
    for workloads holding many long-lived connections (for example a large
    WebSocket fan-out). Uvicorn does not cap concurrency by default; under it
    this setting has no effect.

It serves **HTTP/1.1 and WebSocket**.

- It performs the RFC 6455 upgrade handshake itself, so WebSocket routes
  run under `app.run()` without an ASGI server.
- It does **not** implement HTTP/2. For HTTP/2, either serve the app under
  an HTTP/2-capable ASGI server such as Hypercorn, or — as most deployments
  do — terminate HTTP/2 at a reverse proxy (nginx, Caddy, a cloud load
  balancer) that forwards HTTP/1.1 to the app; uvicorn itself is
  HTTP/1.1-only.
- Native subprotocol negotiation is unsupported — `accept(subprotocol=...)`
  raises on the built-in server, because the `101 Switching Protocols`
  response is written before `accept()` runs — so use an ASGI server if you
  need to negotiate a subprotocol.

!!! note "Why no built-in HTTP/2"
    Not shipping a from-scratch HTTP/2 stack is a deliberate scope line: it
    is a large, security-sensitive binary protocol, and in practice HTTP/2
    is almost always terminated at a proxy that speaks HTTP/1.1 to the app,
    so the app server rarely needs to implement it.

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
  For server-side, revocable sessions use `ServerSessionMiddleware` with
  a `SessionStore` backend. The bundled `InMemorySessionStore` is
  per-process, so a multi-worker deployment needs a shared store (Redis,
  a database) implementing the `SessionStore` interface.
- **Rate limiting** — `RateLimitMiddleware` is in-memory and therefore
  per-worker. For an accurate global limit across workers, put the
  limiter in a reverse proxy (nginx `limit_req`) or back it with Redis.
- **Any in-memory cache or counter** — assume it is per-worker. Move
  anything that must be globally consistent into an external store.

A single-worker deployment behind a reverse proxy sidesteps all of the
above; scale out with more workers only once shared state is externalised.

## Advanced: serving under gunicorn with VeloceWorker

!!! note "POSIX / gunicorn only"
    `VeloceWorker` is an **optional, advanced** alternative for stacks that
    already use gunicorn for process supervision. gunicorn is POSIX-only and
    is not a Veloce dependency. **uvicorn remains the recommended default** —
    reach for this worker only when gunicorn is already part of your
    deployment.

Veloce ships an optional gunicorn worker class,
`veloce.workers.VeloceWorker`, that lets gunicorn manage the process pool
(forking, restarts, signal handling) while each worker drives Veloce's own
`HttpProtocol` directly on an asyncio event loop — no uvicorn and no ASGI
shim in the request path.

Install gunicorn via the optional extra (POSIX only):

```bash
pip install veloceframework[gunicorn]
```

Then point gunicorn at the worker class by its import path:

```bash
gunicorn your_module:app -k veloce.workers.VeloceWorker --workers 4
```

gunicorn binds the listening socket in the master and hands it to each
forked worker, so all workers share one kernel accept queue — the standard
pre-fork model. The worker runs the app's startup hooks when it boots and
its shutdown hooks when gunicorn stops it, draining in-flight requests
within the configured `--timeout` before cancelling stragglers.

The per-worker state caveats in the table above apply unchanged: each
gunicorn worker is a separate process with its own memory, so in-memory
rate-limit buckets, caches, and `app.state` mutations are per-worker.

This path serves **HTTP/1.1 and WebSocket** through the same built-in
protocol as `app.run()` — for HTTP/2, put it behind a reverse proxy or
use an HTTP/2-capable ASGI server, as above. As on the built-in server,
native subprotocol negotiation is unsupported.

!!! note "New — runtime-verified, not yet battle-tested at scale"
    `VeloceWorker` has been exercised end-to-end on Linux (Ubuntu 24.04,
    Python 3.12, gunicorn 26.0.0): worker boot (`init_process` → `run`),
    request serving, TLS termination via `--certfile`/`--keyfile` (HTTPS
    served; plain HTTP to the TLS port refused — no cleartext downgrade),
    `--max-requests` worker recycling, graceful `SIGTERM` shutdown with no
    orphaned processes, and worker self-exit when the master is killed
    (arbiter-death detection). It is still new and has not been run under
    sustained production load or at multi-worker scale — load-test it for
    your workload before relying on it, and note uvicorn remains the
    recommended default for most deployments.

## Serving over HTTP/2

Veloce's built-in server and uvicorn both speak HTTP/1.1 only. Because the
app is ASGI-native, HTTP/2 needs no application changes — it is a serving
concern, reached two ways.

**Behind a reverse proxy (the common path).** Terminate HTTP/2 and TLS at
nginx / Caddy / a cloud load balancer, and proxy HTTP/1.1 to the app. The
HTTP/2 wins (multiplexing, header compression) apply on the client-to-edge
hop; the edge-to-app hop stays HTTP/1.1. An nginx server block:

```nginx
server {
    listen 443 ssl;
    http2 on;                 # nginx < 1.25.1: `listen 443 ssl http2;`
    server_name example.com;
    ssl_certificate     /etc/ssl/example.crt;
    ssl_certificate_key /etc/ssl/example.key;

    location / {
        proxy_pass http://127.0.0.1:8000;     # the app, on HTTP/1.1
        proxy_set_header Host              $host;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # WebSocket upgrade pass-through (HTTP/1.1 only).
        proxy_http_version 1.1;
        proxy_set_header Upgrade    $http_upgrade;
        proxy_set_header Connection "upgrade";
    }
}
```

Add [`ProxyFix`](middleware.md) so the app trusts the proxy's
`X-Forwarded-*` headers (correct client IP and `https` scheme):

```python
from veloce import ProxyFix

# One proxy hop in front: trust the last X-Forwarded-For / -Proto entry.
app.add_middleware(ProxyFix, x_for=1, x_proto=1)
```

**Direct HTTP/2 with Hypercorn.** Hypercorn is an ASGI server that speaks
HTTP/1.1, HTTP/2, and HTTP/3; it serves the same Veloce app with no code
change. Browsers require HTTP/2 over TLS (negotiated via ALPN), so pass a
certificate:

```bash
pip install hypercorn
hypercorn main:app --certfile cert.pem --keyfile key.pem --bind 0.0.0.0:8443
```

Over TLS, Hypercorn negotiates HTTP/2 automatically; an HTTP/1.1 client on
the same port still works. WebSocket routes are served on both paths.

## Extending the CLI with plugins

The `veloce` command ships with `new`, `generate` (`g`), `run`, `mcp`, `routes`, `check`, `shell`, and
`custom`. A distribution can add its own subcommands by advertising a
callable under the `veloce.commands` entry-point group — useful for
deployment helpers, database migrations, or any operational task you want
alongside the built-in commands.

In your package's `pyproject.toml`:

```toml
[project.entry-points."veloce.commands"]
deploy = "mypkg.cli:register"
```

The target is handed the argparse subparsers action and adds exactly one
subcommand, setting a `func` default that receives the parsed arguments and
returns an exit code:

```python
# mypkg/cli.py
def register(subparsers):
    parser = subparsers.add_parser("deploy", help="Deploy the app.")
    parser.add_argument("target")
    parser.set_defaults(func=_deploy)


def _deploy(args):
    print(f"Deploying to {args.target}")
    return 0
```

Once the package is installed, `veloce deploy prod` runs your command.

!!! note "Plugins cannot break the core"
    A plugin that fails to import, does not resolve to a callable, raises
    while registering, or whose name collides with a built-in (or another
    plugin) is reported with a warning and skipped. The built-in commands
    always remain usable.

## Security considerations

### Hardening checklist

- Pass `secure=True` to your session middleware, and register
  `SecurityHeadersMiddleware(hsts_max_age=31536000)`, for any
  internet-facing deployment.
- Run `veloce check your_module:app` before deploying — it flags
  `DEBUG`, a missing `SECRET_KEY`, insecure session cookies, and missing
  security headers.
- Set `MAX_CONTENT_LENGTH` so oversized uploads are refused.
- Keep `debug=False` for anything reachable beyond `localhost` — debug
  tracebacks leak source and internals.

### What the audit can and cannot see

Middleware reports on itself. The audit walks the registered middleware and
asks each one, so a middleware written outside Veloce is audited on the same
terms as a built-in. Severity decides what a finding *does*:

| severity | at startup | `veloce check` |
| --- | --- | --- |
| `error` | refuses to serve (`AuditFailed`) | exits 1 |
| `warning` | logged in debug | exits 1 |
| `info` | logged in debug | reported, exits 0 |

```python
from veloce import Finding, Middleware

class TenantAuthMiddleware(Middleware):
    def audit(self, ctx):
        if not ctx.app.config.get("TENANT_SIGNING_KEY"):
            yield Finding(
                "TENANT_SIGNING_KEY is not set - tenant headers are unverified.",
                severity="error",
                fix="set TENANT_SIGNING_KEY",
                id="tenant-signing-key-missing",
            )
```

Anything adding hardening headers declares it, and satisfies the headers check
without being a `SecurityHeadersMiddleware`:

```python
class MyHeadersMiddleware(Middleware):
    sets_hardening_headers = True
```

A session backend gets its cookie check by subclassing
[`SessionMiddlewareBase`](../reference/middleware.md), which also carries the
`session.permanent` lifetime rule.

!!! note "Checks that read the route table"

    `veloce check` imports your app without starting it, so routes registered
    during startup do not exist yet. A check that reads `ctx.app`'s routes must
    set `audit_needs_routes = True`; it is then skipped until startup rather
    than reporting a live route as missing.

### Silencing an accepted finding

Turn off one finding by id, so the audit stays on for everything else:

```python
app.config["SILENCED_AUDIT_IDS"] = ("hardening-headers-missing",)
```

!!! warning "A clean audit is not a proof"

    The audit reports on this app's middleware. Hardening the app does not
    own — TLS terminated at a reverse proxy, headers added by a CDN — is
    invisible to it, as is a middleware that reports nothing. Verify those
    separately.

### Regex constraints and ReDoS

A `pattern=` (or `regex=`) constraint on `Query`, `Path`, `Header`,
`Cookie`, or `Form` is compiled once and then matched **synchronously on
the event loop** against the request value. A pattern with catastrophic
backtracking — typically nested quantifiers like `(a+)+`, `(.*)*`, or
overlapping alternations — can take super-linear time on a crafted
input, stalling the loop and every other request on that worker
(a regular-expression denial of service, ReDoS).

Guidance for developer-supplied patterns:

- Prefer simple, linear-time patterns; avoid nested quantifiers and
  quantified groups that can match the same text more than one way.
- Anchor patterns (`^…$`) so the engine does not retry at every offset.
- Pair a pattern with a `max_length` constraint so the input the regex
  runs against is bounded.
- Test any non-trivial pattern against a deliberately adversarial input
  before shipping it.

Veloce compiles each pattern once at route registration, so the
compile cost is paid up front — but the *match* cost is still the
developer's responsibility to keep linear.

## See also

- [Middleware](middleware.md)
- [Testing](testing.md)
- [Comparison](../comparison.md)

