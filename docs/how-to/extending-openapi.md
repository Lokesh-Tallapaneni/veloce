---
description: Inject custom fields into the generated OpenAPI document by overriding app.openapi() or assigning app.openapi_schema — info.x-logo, tag ordering, and global security.
tags: [openapi, schema, customisation, docs]
---

# Extending OpenAPI

Veloce builds the OpenAPI 3.1 document for you from your routes, but the result is a
plain Python dict you are free to reshape. Call [`app.openapi()`](../reference.md#veloce.Veloce)
to build (and cache) it, or assign your own dict to `app.openapi_schema` to replace the
build entirely. The JSON endpoint and the Swagger / ReDoc UIs all read back through
`app.openapi()`, so any change you make there is what clients and the docs pages serve.

The OpenAPI machinery ships with Veloce — there is nothing extra to install.

## How the schema is built and served

`app.openapi()` computes the document on first call and caches it on `app.openapi_schema`:

```python
from veloce import Veloce

app = Veloce(title="Items API", version="1.0.0")

schema = app.openapi()
assert schema["openapi"] == "3.1.0"
assert schema["info"]["title"] == "Items API"
assert app.openapi_schema is schema  # cached: same dict on every call
```

The `/openapi.json` route, the Swagger UI at `/docs`, and the ReDoc UI at `/redoc` all
serve `app.openapi()`, so a mutation reaches every consumer.

Most metadata is better set
through the constructor — `title`, `version`, `summary`, `description`, `contact`,
`license_info`, `terms_of_service`, `servers`, and `openapi_tags` all flow into the
document without any manual editing. Reach for the techniques below only for fields the
constructor does not expose.

## Mutating the cached document in place

The dict returned by `app.openapi()` *is* the cached document. Build it once, then add
your custom fields. Vendor extensions (keys prefixed `x-`) such as ReDoc's `info.x-logo`
have no constructor kwarg, so set them here.

```python title="app.py"
from veloce import Veloce

app = Veloce(title="Items API", version="1.0.0")


@app.get("/items")
async def list_items():
    return [{"id": 1, "name": "Wrench"}]


schema = app.openapi()
schema["info"]["x-logo"] = {
    "url": "https://example.com/logo.png",
    "altText": "Items API",
}


if __name__ == "__main__":
    app.run(port=8000)
```

Run this once at import time (after your routes are registered, before the first request)
so the customised dict is the one cached on `app.openapi_schema`.

!!! note
    Build the schema *after* declaring your routes. `app.openapi()` reads the routes
    registered at the moment it first runs, then caches; routes added afterwards will not
    appear unless you reset `app.openapi_schema = None` to force a rebuild.

## Ordering and annotating tags

Swagger UI and ReDoc render tag groups in the order they appear in the top-level `tags`
list. Pass `openapi_tags` to the constructor to control that order and attach a
description to each group.

```python title="app.py"
from veloce import Veloce

tags_metadata = [
    {"name": "users", "description": "Operations on user accounts."},
    {"name": "items", "description": "Inventory and catalogue operations."},
]

app = Veloce(title="Items API", version="1.0.0", openapi_tags=tags_metadata)


@app.get("/users", tags=["users"])
async def list_users():
    return []


@app.get("/items", tags=["items"])
async def list_items():
    return []


if __name__ == "__main__":
    app.run(port=8000)
```

The constructor stores this on `app.openapi_tags` and emits it verbatim as the document's
`tags`. To reorder tags on an already-built schema, edit `schema["tags"]` directly:

```python
schema = app.openapi()
schema["tags"] = sorted(schema.get("tags", []), key=lambda t: t["name"])
```

## Adding a global security requirement

The generator emits per-operation `security` entries for every reachable
[`Security()`](../reference.md#veloce.Security) scheme. To apply one scheme to the *whole*
API, register the scheme component and add a document-level `security` list.

```python title="app.py"
from veloce import Veloce

app = Veloce(title="Items API", version="1.0.0")


@app.get("/items")
async def list_items():
    return []


schema = app.openapi()
schema.setdefault("components", {}).setdefault("securitySchemes", {})["BearerAuth"] = {
    "type": "http",
    "scheme": "bearer",
}
schema["security"] = [{"BearerAuth": []}]


if __name__ == "__main__":
    app.run(port=8000)
```

!!! warning "A document-level requirement is documentation, not enforcement"
    Adding `security` to the schema only advertises the requirement to clients and the
    docs UI. It does **not** make Veloce reject unauthenticated requests. Enforce auth in
    your handlers or dependencies with [`Security()`](../reference.md#veloce.Security) — the
    OpenAPI document describes the contract, it does not police it.

## Replacing the build entirely

Assign a finished dict to `app.openapi_schema` *before any request lands* and
`app.openapi()` returns it untouched — the auto-build never runs. Use this to serve a
hand-written or externally-generated document.

```python title="app.py"
from veloce import Veloce

app = Veloce()

app.openapi_schema = {
    "openapi": "3.1.0",
    "info": {"title": "Hand-written API", "version": "2.0.0"},
    "paths": {},
}


if __name__ == "__main__":
    app.run(port=8000)
```

A common middle ground is to wrap the auto-build: generate the default document, mutate it,
and assign the result back so the cache holds your customised copy.

```python
schema = app.openapi()              # build the default once
schema["info"]["x-logo"] = {"url": "https://example.com/logo.png"}
app.openapi_schema = schema         # cache the mutated copy
```

!!! note
    Once `app.openapi_schema` is set, Veloce never rebuilds it. If you add routes after
    assigning a custom schema, set `app.openapi_schema = None` to discard the cache and let
    the next `app.openapi()` call regenerate from the current routes.

!!! warning "There is no `openapi()` hook to subclass"
    Unlike some frameworks, Veloce does not call a user-supplied builder function on every
    schema access. Customisation happens by mutating or replacing the cached
    `app.openapi_schema` dict, not by reassigning `app.openapi`. Override the document, not
    the method.

## Where common fields live

| Document field | Source |
| --- | --- |
| `info.title`, `info.version` | `Veloce(title=..., version=...)`. |
| `info.summary`, `info.description` | `Veloce(summary=..., description=...)`. |
| `info.contact`, `info.license` | `Veloce(contact=..., license_info=...)`. |
| `info.termsOfService` | `Veloce(terms_of_service=...)`. |
| `info.x-logo` and other `x-` keys | Mutate `app.openapi()["info"]` directly. |
| `tags` (order and descriptions) | `Veloce(openapi_tags=[...])`. |
| `servers` | `Veloce(servers=[...])`. |
| `externalDocs` | `Veloce(openapi_external_docs=...)`. |
| `security` (global) | Add to `app.openapi()` directly. |

## Testing the customised document

Use the in-memory [`TestClient`](../reference.md#veloce.TestClient) to fetch
`/openapi.json` and assert your fields survived into the served document.

```python
from veloce import TestClient, Veloce

app = Veloce(title="Items API", version="1.0.0")


@app.get("/items")
async def list_items():
    return []


schema = app.openapi()
schema["info"]["x-logo"] = {"url": "https://example.com/logo.png"}

client = TestClient(app)

resp = client.get("/openapi.json")
assert resp.status_code == 200
body = resp.json()
assert body["info"]["x-logo"] == {"url": "https://example.com/logo.png"}
assert body["openapi"] == "3.1.0"
```

## Next steps

- Turn the docs on or off per environment — see [Conditional OpenAPI](conditional-openapi.md).
- Document responses, metadata, and security schemes — see [OpenAPI, metadata and docs](../guide/openapi.md).
- Full signatures are in the [API reference](../reference.md).
