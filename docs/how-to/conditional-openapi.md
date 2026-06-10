---
description: Disable the OpenAPI document and the interactive docs in production by passing openapi_url=None, docs_url=None, and redoc_url=None to Veloce, gated on an environment or config flag.
tags: [openapi, docs, production, security]
---

# Conditional OpenAPI

Veloce serves the generated OpenAPI document and two interactive explorers by
default. [`Veloce`](../reference.md#veloce.Veloce) accepts `openapi_url`,
`docs_url`, and `redoc_url` to relocate or disable each one.

Pass `None` to a URL to turn that endpoint off entirely — useful for hiding your
API surface in production while keeping the explorers during development.

## The three URLs

Each URL controls one endpoint, all enabled by default:

| Argument | Default | Endpoint |
| --- | --- | --- |
| `openapi_url` | `/openapi.json` | The generated OpenAPI 3.1 JSON document. |
| `docs_url` | `/docs` | The Swagger UI explorer. |
| `redoc_url` | `/redoc` | The ReDoc explorer. |

| Set to `None` | Effect |
| --- | --- |
| `docs_url=None` or `redoc_url=None` | Disables that explorer but leaves `/openapi.json` in place, so external tooling can still consume the schema. |
| `openapi_url=None` | Disables the JSON document *and* both explorers, because the UIs have nothing to read. |

```python title="app.py"
from veloce import Request, Veloce

# No JSON schema, no Swagger UI, no ReDoc.
app = Veloce(openapi_url=None, docs_url=None, redoc_url=None)


@app.get("/ping")
async def ping(request: Request):
    return {"pong": True}


if __name__ == "__main__":
    app.run(port=8000)
```

!!! warning "Disable with `None`, not an empty string"
    The off switch is the literal value `None`. An empty string (`docs_url=""`)
    does **not** disable the endpoint — it registers the route at the empty
    path, which is not what you want. Only `None` skips registration.

## Gating on an environment variable

Read an environment variable at startup and choose the URLs from it. The docs
stay on in development and vanish in production.

```python title="app.py" hl_lines="10 11 12"
import os

from veloce import Request, Veloce

PRODUCTION = os.environ.get("APP_ENV") == "production"

app = Veloce(
    title="Catalog API",
    version="1.0.0",
    openapi_url=None if PRODUCTION else "/openapi.json",
    docs_url=None if PRODUCTION else "/docs",
    redoc_url=None if PRODUCTION else "/redoc",
)


@app.get("/items")
async def items(request: Request):
    return {"items": []}


if __name__ == "__main__":
    app.run(port=8000)
```

Run with `APP_ENV=production` and the three endpoints return `404`; run without
it and they serve as usual.

The flag is read once at construction, so toggling it requires a restart — which
is the right granularity for a deployment switch.

## Gating on app config

If you already centralise settings in [`Config`](../reference.md#veloce.Config),
load the flag before constructing the app and feed it into the constructor.

```python title="app.py"
from veloce import Config, Request, Veloce

config = Config()
config.from_prefixed_env("APP")  # reads APP_ENABLE_DOCS, etc.

enable_docs = config.get("ENABLE_DOCS", False)

app = Veloce(
    openapi_url="/openapi.json" if enable_docs else None,
    docs_url="/docs" if enable_docs else None,
    redoc_url="/redoc" if enable_docs else None,
)
app.config.update(config)


@app.get("/health")
async def health(request: Request):
    return {"ok": True}


if __name__ == "__main__":
    app.run(port=8000)
```

!!! note
    `app.config` is the live application config dict, but `Veloce` reads
    `openapi_url` / `docs_url` / `redoc_url` from the constructor arguments, not
    from `app.config`. Resolve the flag *before* the `Veloce(...)` call and pass
    the URLs in — mutating `app.config` afterwards has no effect on which docs
    routes were registered.

## Keep the schema, hide only the explorers

A common production posture is to keep `/openapi.json` for SDK generation and
monitoring while removing the human-facing explorers. Disable just the two UIs:

```python title="app.py"
from veloce import Request, Veloce

# Schema stays; Swagger UI and ReDoc are gone.
app = Veloce(docs_url=None, redoc_url=None)


@app.get("/ping")
async def ping(request: Request):
    return {"pong": True}


if __name__ == "__main__":
    app.run(port=8000)
```

!!! warning "Disabling the docs is not authentication"
    A `404` on `/docs` hides the explorer, but it does not protect the routes
    themselves. Anyone who knows your paths can still call them. Treat
    conditional docs as surface reduction, not access control — secure the
    routes with [security schemes](../guide/security-schemes.md).

## Testing the toggle

Construct the app with the docs disabled and assert the endpoints are gone with
the in-memory [`TestClient`](../reference.md#veloce.TestClient).

```python
from veloce import Request, TestClient, Veloce

app = Veloce(openapi_url=None, docs_url=None, redoc_url=None)


@app.get("/ping")
async def ping(request: Request):
    return {"pong": True}


client = TestClient(app)

assert client.get("/ping").status_code == 200
assert client.get("/openapi.json").status_code == 404
assert client.get("/docs").status_code == 404
assert client.get("/redoc").status_code == 404
```

Flip the construction to the defaults and the same three paths return `200`.

## Next steps

- Configure the document itself — see [OpenAPI, metadata and docs](../guide/openapi.md).
- Centralise the gating flag — see [Configuration](../guide/configuration.md).
- Protect the routes the docs would have described — see [Security schemes](../guide/security-schemes.md).
- Full signatures are in the [API reference](../reference.md).
