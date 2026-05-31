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
```

It serves **HTTP/1.1 only** — it performs no WebSocket upgrade
handshake and does not implement HTTP/2. Run WebSocket routes and
HTTP/2 workloads under uvicorn, which implements both; Veloce's
WebSocket support is reached through the ASGI server, not the built-in
development server. This is a deliberate scope line: hardening a
from-scratch production server is not the project's goal when mature
ASGI servers already exist.

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

This path serves **HTTP/1.1 only**, exactly like the built-in development
server — WebSocket and HTTP/2 workloads still belong under uvicorn.

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

## Extending the CLI with plugins

The `veloce` command ships with `run`, `routes`, `check`, `shell`, and
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

- Call `app.use_secure_defaults()` (secure session cookies +
  `SecurityHeadersMiddleware`) for any internet-facing deployment.
- Run `veloce check your_module:app` before deploying — it flags
  `DEBUG`, a missing `SECRET_KEY`, insecure session cookies, and missing
  security headers.
- Set `MAX_CONTENT_LENGTH` so oversized uploads are refused.
- Keep `debug=False` for anything reachable beyond `localhost` — debug
  tracebacks leak source and internals.

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

