# Veloce

<p align="center">
  <em>Fast, ergonomic async Python web framework — ASGI-native, batteries included.</em>
</p>

<p align="center">
  <a href="https://pypi.org/project/veloceframework/"><img alt="PyPI" src="https://img.shields.io/pypi/v/veloceframework.svg"></a>
  <a href="https://pypi.org/project/veloceframework/"><img alt="Python versions" src="https://img.shields.io/pypi/pyversions/veloceframework.svg"></a>
  <a href="https://github.com/Lokesh-Tallapaneni/veloce/blob/main/LICENSE"><img alt="License" src="https://img.shields.io/pypi/l/veloceframework.svg"></a>
  <a href="https://github.com/Lokesh-Tallapaneni/veloce/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/Lokesh-Tallapaneni/veloce/actions/workflows/ci.yml/badge.svg"></a>
  <a href="https://github.com/Lokesh-Tallapaneni/veloce/actions/workflows/docs.yml"><img alt="Docs" src="https://github.com/Lokesh-Tallapaneni/veloce/actions/workflows/docs.yml/badge.svg"></a>
</p>

---

**Documentation:** [veloceframework.com](https://veloceframework.com)

**Source code:** <https://github.com/Lokesh-Tallapaneni/veloce>

---

Veloce is a from-scratch async Python web framework. The router,
request/response pipeline, dependency injection system, OpenAPI
generator, WebSocket layer, and test client are all in-tree — not
wrappers around an existing stack.

## Position

Veloce is a from-scratch async Python web framework — not a wrapper around Starlette, FastAPI, or Flask. It provides FastAPI-style typed dependency injection, Pydantic v2 request/response validation, OpenAPI 3.1 schema generation, WebSocket support, and Flask-compatible helpers (g, flash, blueprints, session) in a single tree. It is built on a radix-tree router, an in-memory test client, and built-in CORS/CSRF/session/rate-limit/security-headers middleware. An external security review of the request path is planned ahead of 1.0; until then, treat it as pre-production software.

## Key design points

* **Async-first handlers.** `async def` handlers run directly on the
  event loop; sync `def` handlers are supported transparently and run in
  a thread-pool executor so they do not block it.
* **Precompiled dispatch.** Handler signatures are inspected once at
  registration into a `HandlerPlan`; the per-request hot path performs
  no reflection.
* **Radix-tree routing** with typed path converters (`int`, `float`,
  `uuid`, `path`, plus custom).
* **Typed dependency injection** via `Depends`, `Security`,
  `SecurityScopes`, including `yield`-style dependencies with teardown.
* **OpenAPI 3.1** generated from Pydantic models, served through
  Swagger UI and ReDoc out of the box.
* **In-memory test client** drives the real ASGI surface — no sockets,
  no separate server process.

## Requirements

Python 3.10+.

Veloce depends on a small native-extension stack: `orjson`,
`httptools`, `pydantic` v2, `python-multipart`, `multidict`, and
`uvloop` on non-Windows platforms.

## Installation

```bash
pip install veloceframework
```

The installed package exposes the `veloce` import:

```python
from veloce import Veloce
```

## Example

Create `main.py`:

```python
from veloce import Veloce

app = Veloce()


@app.get("/")
async def index() -> dict[str, str]:
    return {"hello": "world"}


@app.get("/items/{item_id:int}")
async def read_item(item_id: int) -> dict[str, int]:
    return {"item_id": item_id}
```

Run it with the built-in server (no extra dependencies):

```bash
veloce run main:app
```

`uvicorn` is an optional extra — install it (`pip install veloceframework[uvicorn]`)
to serve under it or any other ASGI server, or use `app.run()` / the gunicorn
`VeloceWorker`:

```bash
uvicorn main:app
```

Open <http://127.0.0.1:8000/items/42> to see `{"item_id": 42}`, or
<http://127.0.0.1:8000/docs> for the interactive OpenAPI UI.

## Feature surface

| Area              | Highlights                                                                                  |
|-------------------|---------------------------------------------------------------------------------------------|
| Routing           | radix tree, path converters, blueprints with nesting, subdomain routing, host constraints   |
| Requests          | streaming bodies, multipart, JSON, MultiDict query/headers/cookies, rich URL/header accessors |
| Responses         | `JSONResponse`, `HTMLResponse`, `StreamingResponse`, `FileResponse`, ETag/Last-Modified     |
| Dependency inj.   | `Depends`, `Security`, `SecurityScopes`, `Annotated[T, Depends()]`, `yield` + teardown      |
| Validation        | Pydantic v2 query / path / header / cookie / body, structured 422 errors                    |
| OpenAPI           | OpenAPI 3.1, Swagger UI, ReDoc, security schemes, webhooks, callbacks, `operation_id`       |
| WebSockets        | full ASGI surface, dependency injection, subprotocol negotiation, typed iter helpers        |
| Middleware        | CORS, GZip, TrustedHost, HTTPSRedirect, ProxyFix, Session, CSRF, `BaseHTTPMiddleware`       |
| Templating        | Jinja2 with `url_for` / `g` / `current_app` globals, async render, context processors       |
| Sessions          | signed cookies, server-side backend, `permanent_lifetime`, secret rotation                  |
| Testing           | in-memory `TestClient`, multipart, cookies, follow-redirects, `session_transaction`         |
| Tooling           | `veloce run`, `veloce routes`, `veloce shell` (argparse-based CLI)                          |

The full Tier 0/1/2 feature matrix and per-feature design notes live in
[`docs/`](docs/).

## Benchmarks

Comparative benches against equivalent third-party async and sync
frameworks live in [`bench/`](bench/). On the JSON-hello and path-param
hot paths Veloce sustains a 3-7x throughput multiplier on Python 3.12
under the configurations measured. Numbers are workload-specific and
reproducible; run the suite locally before quoting them.

## Project status

Veloce follows [semantic versioning](https://semver.org/): the public
API surface — the names exported from `veloce/__init__.py` — is what each
release commits to. The current version is shown by the PyPI badge above.

## License

MIT. See [`LICENSE`](LICENSE).
