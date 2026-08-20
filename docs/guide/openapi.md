---
description: >-
  Configure the OpenAPI document, metadata, and interactive docs — app metadata,
  /openapi.json, app.openapi() extension, conditional docs, Swagger UI parameters,
  per-route operation_id/openapi_extra/include_in_schema, callbacks, and webhooks.
tags: [openapi, docs, swagger, metadata]
---

# OpenAPI, metadata and docs

Veloce generates an [OpenAPI 3.1](https://spec.openapis.org/oas/v3.1.0) document
from your registered routes and serves it alongside two interactive explorers.
[`Veloce`](../reference/application.md#veloce.Veloce) exposes the document at `/openapi.json`,
Swagger UI at `/docs`, and ReDoc at `/redoc` by default. The schema is built lazily
on first access and cached on [`app.openapi_schema`](../reference/application.md#veloce.Veloce).

```python title="app.py"
from veloce import Request, Veloce

app = Veloce(title="Catalog API", version="1.0.0")


@app.get("/ping")
async def ping(request: Request):
    return {"pong": True}


if __name__ == "__main__":
    app.run(port=8000)
```

Run the app and visit `http://localhost:8000/docs` for Swagger UI or
`http://localhost:8000/redoc` for ReDoc. Both read from `/openapi.json`.

## App metadata

Metadata passed to the `Veloce` constructor populates the OpenAPI `info` object
and the document root. Only `title` and `version` are emitted unconditionally;
the rest appear when set.

```python title="app.py"
from veloce import Request, Veloce

app = Veloce(
    title="Catalog API",
    version="2.3.1",
    summary="Products, carts and orders.",
    description="A longer description, **Markdown** is rendered by the docs UIs.",
    terms_of_service="https://example.com/terms",
    contact={"name": "API Support", "url": "https://example.com/support"},
    license_info={"name": "MIT", "url": "https://opensource.org/license/mit"},
    servers=[{"url": "https://api.example.com", "description": "Production"}],
    openapi_tags=[{"name": "items", "description": "Catalog operations."}],
    openapi_external_docs={"url": "https://example.com/docs", "description": "Manual"},
)


@app.get("/items", tags=["items"])
async def list_items(request: Request):
    return []
```

| Constructor argument | Emitted as | Purpose |
| --- | --- | --- |
| `title` | `info.title` | API name shown in the docs UIs. Defaults to `"Veloce"`. |
| `version` | `info.version` | API version string. Defaults to `"0.1.0"`. |
| `summary` | `info.summary` | One-line summary, distinct from `description`. |
| `description` | `info.description` | Longer Markdown description. |
| `terms_of_service` | `info.termsOfService` | URL to the terms of service. |
| `contact` | `info.contact` | OpenAPI contact object. |
| `license_info` | `info.license` | OpenAPI license object. |
| `servers` | `servers` | Base URLs the API is served from. |
| `openapi_tags` | `tags` | Tag metadata and ordering for the document. |
| `openapi_external_docs` | `externalDocs` | Document-level external documentation object. |

`openapi_version` on the app is the spec version string emitted as the document's
top-level `openapi` key; it defaults to `"3.1.0"` and rarely needs changing.

## Docs URLs

The three serving paths are constructor arguments. Set any of them to change the
path; set one to `None` to disable that endpoint.

```python title="app.py"
from veloce import Request, Veloce

app = Veloce(
    openapi_url="/api/openapi.json",
    docs_url="/api/docs",
    redoc_url="/api/redoc",
)


@app.get("/ping")
async def ping(request: Request):
    return {"pong": True}
```

| Argument | Default | Purpose |
| --- | --- | --- |
| `openapi_url` | `/openapi.json` | Path serving the generated OpenAPI JSON document. |
| `docs_url` | `/docs` | Path serving Swagger UI. |
| `redoc_url` | `/redoc` | Path serving ReDoc. |

!!! note
    The `/docs` and `/redoc` routes fetch the document from `openapi_url`. If you
    move `openapi_url`, Veloce wires the UIs to the new path automatically — you
    do not adjust them separately.

## Conditional OpenAPI

Disabling a docs surface is per-URL, and the granularity matters.

`docs_url=None` / `redoc_url=None`
: removes an interactive UI while the JSON schema endpoint stays up.

`openapi_url=None`
: disables the whole OpenAPI subsystem — no JSON document and, because the UIs
  depend on it, no `/docs` or `/redoc`.

```python title="app.py"
import os

from veloce import Request, Veloce

# Serve the machine-readable schema everywhere, but only expose the
# interactive explorers outside production.
production = os.environ.get("ENV") == "production"

app = Veloce(
    docs_url=None if production else "/docs",
    redoc_url=None if production else "/redoc",
)


@app.get("/ping")
async def ping(request: Request):
    return {"pong": True}
```

!!! warning "Disable with `None`, not `\"\"`"
    The off switch is the value `None`. An empty string is a real (empty) path,
    not a disable signal, and registering a route at `""` is invalid. To turn a
    docs surface off, pass `None`.

```python
from veloce import Request, TestClient, Veloce

app = Veloce(docs_url=None, openapi_url="/openapi.json")


@app.get("/ping")
async def ping(request: Request):
    return {"pong": True}


client = TestClient(app)

assert client.get("/docs").status_code == 404
assert client.get("/openapi.json").status_code == 200
```

## Extending the schema

[`app.openapi()`](../reference/application.md#veloce.Veloce.openapi) returns the generated
document, building it on first call and caching it on `app.openapi_schema`.

The returned dict is the live cached object, so mutating it in place is the supported
way to inject keys the generator does not produce — an `info.x-logo`, a custom tag
ordering, vendor extensions.

```python title="app.py" hl_lines="14"
from veloce import Request, Veloce

app = Veloce(title="Catalog API")


@app.get("/items")
async def list_items(request: Request):
    return []


# Build once, then patch the cached document in place. The /docs and
# /openapi.json endpoints serve this same mutated object.
schema = app.openapi()
schema["info"]["x-logo"] = {"url": "https://example.com/logo.png"}
```

To replace the document wholesale and bypass the auto-build, assign your own dict
to `app.openapi_schema` before the first request lands. `app.openapi()` returns it
untouched.

```python
from veloce import Request, TestClient, Veloce

app = Veloce()

app.openapi_schema = {
    "openapi": "3.1.0",
    "info": {"title": "Hand-written", "version": "9.9"},
    "paths": {},
}


@app.get("/items")
async def list_items(request: Request):
    return []


client = TestClient(app)

assert client.get("/openapi.json").json()["info"]["title"] == "Hand-written"
```

!!! note
    The generator runs once and caches. If you register routes after the first
    call to `app.openapi()` (or after the first request), reset
    `app.openapi_schema = None` to force a rebuild on next access.

## Swagger UI parameters

`swagger_ui_parameters` is a dict of options passed straight into the
`SwaggerUIBundle(...)` constructor in the `/docs` page. Use it to tune the
explorer's behaviour.

```python title="app.py"
from veloce import Request, Veloce

app = Veloce(
    swagger_ui_parameters={
        "docExpansion": "none",
        "deepLinking": True,
        "displayRequestDuration": True,
    },
)


@app.get("/ping")
async def ping(request: Request):
    return {"pong": True}
```

`swagger_ui_init_oauth` is a separate dict passed to Swagger UI's `initOAuth(...)`,
used to pre-fill the OAuth2 client configuration on the Authorize dialog.

!!! note
    The docs assets (Swagger UI and ReDoc bundles) load from a CDN, pinned to a
    fixed version with a Subresource Integrity hash. Veloce does not currently
    self-host the assets, so `/docs` and `/redoc` need network access to the CDN.

## Separate input and output schemas

When a model's request shape and response shape diverge — a `computed_field`, a
write-only or read-only field — Veloce emits a distinct `-Output` component for the
response so each side documents what is actually sent and received. This is on by
default; pass `separate_input_output_schemas=False` to reuse one component for both.

```python title="app.py"
from pydantic import BaseModel

from veloce import Veloce


class ItemIn(BaseModel):
    name: str
    price: float


class ItemOut(BaseModel):
    name: str
    price: float
    id: int


app = Veloce()


@app.post("/items", response_model=ItemOut)
async def create_item(item: ItemIn):
    return {"name": item.name, "price": item.price, "id": 1}
```

The request body references `ItemIn`; the `200` response references `ItemOut`.

!!! note
    Models whose validation and serialization JSON Schemas are byte-identical collapse
    onto a single component — the `-Output` variant only appears when the shapes
    genuinely differ.

!!! tip
    Declaring distinct `…In` / `…Out` models keeps your request and response
    contracts explicit. See [Requests and responses](requests-responses.md).

### The return annotation is the response model

A handler that names a model in its return annotation needs no `response_model=`
— the annotation becomes the contract, and is used to filter the response and to
document it:

```python
from pydantic import BaseModel

from veloce import Veloce


class Item(BaseModel):
    id: int
    name: str


app = Veloce()


@app.get("/items/{item_id}")
async def read_item(item_id: int) -> Item:
    return Item(id=item_id, name="Portal Gun")
```

Because the annotation *filters*, returning a richer object under a narrower
annotation is the way to keep internal fields off the wire while a type checker
still accepts the return:

```python
class ItemInternal(Item):
    cost_price: float


@app.get("/safe/{item_id}")
async def read_safe(item_id: int) -> Item:
    # `cost_price` is dropped from the response — the contract is `Item`.
    return ItemInternal(id=item_id, name="Portal Gun", cost_price=1.5)
```

`list[Model]` works the same way — the response is documented as an array and its
elements are filtered:

```python
@app.get("/items")
async def list_items() -> list[Item]:
    return [ItemInternal(id=1, name="Portal Gun", cost_price=1.5)]  # filtered to Item
```

A union documents its alternatives as `oneOf`. `Model | None` additionally
documents the null case, and returning `None` is allowed:

```python
@app.get("/search")
async def search() -> Item | None:
    return None            # documented as oneOf [Item, null]
```

A union is **documented but not filtered** — which member a value should be
re-shaped through is ambiguous — so use a single model when you need the
response narrowed.

An explicit `response_model=` always wins over the annotation. An annotation that
names no model — a [`Response`](../reference/responses.md#veloce.Response) subclass, `Any`,
a bare `dict` — declares no contract and needs no opt-out. To keep a model
annotation for your editor while declaring no contract, pass `response_model=None`.

!!! note "Changed in version 0.12"
    The return annotation previously had no effect on the response. A handler
    annotated with a model now filters its response to that model's fields. Run
    `veloce check` to see which routes are affected before upgrading.

!!! tip "Find undocumented routes"
    `veloce check` reports every route that publishes no response schema, and
    every route whose `response_model=` contradicts its return annotation — so a
    broken contract is visible before deploying rather than at request time.
    `Veloce(debug=True)` logs the same findings at startup, so an undocumented
    route surfaces on the first boot rather than when someone reads the docs
    page.

## Per-route configuration

Every route decorator accepts OpenAPI-specific keywords that shape its operation
object.

```python title="app.py" hl_lines="17 18 24"
from pydantic import BaseModel

from veloce import Request, Veloce


class Item(BaseModel):
    name: str


app = Veloce()


@app.post(
    "/items",
    tags=["items"],
    summary="Create an item",
    operation_id="create_item",
    openapi_extra={"x-internal": True},
)
async def create_item(item: Item):
    return {"name": item.name}


@app.get("/healthz", include_in_schema=False)
async def healthz(request: Request):
    return {"status": "ok"}
```

| Keyword | Effect |
| --- | --- |
| `operation_id` | Sets the operation's `operationId`. Defaults to `<name>_<method>`. |
| `openapi_extra` | A dict deep-merged onto the generated operation (nested dicts merge key-by-key; scalars and lists overwrite). |
| `include_in_schema` | When `False`, the route stays reachable but is omitted from the document. |
| `tags` | OpenAPI tags for the operation, combined with router-level tags. |
| `summary` / `description` | Operation summary and description; `description` defaults to the handler docstring. |
| `deprecated` | Marks the operation `deprecated`. |
| `response_description` | Description of the successful response. |

```python
from veloce import Request, TestClient, Veloce

app = Veloce()


@app.get("/items", operation_id="list_items")
async def list_items(request: Request):
    return []


@app.get("/healthz", include_in_schema=False)
async def healthz(request: Request):
    return {"status": "ok"}


client = TestClient(app)
schema = client.get("/openapi.json").json()

assert schema["paths"]["/items"]["get"]["operationId"] == "list_items"
assert "/healthz" not in schema["paths"]
assert client.get("/healthz").status_code == 200
```

!!! note "operationId disambiguation"
    Each `operationId` must be unique in the document. Two handlers sharing a
    function name on different paths would collide, so Veloce deterministically
    suffixes the auto-generated id with a path-derived segment and logs a warning.
    Pin `operation_id=` to silence it. Disable the suffixing entirely with
    `Veloce(disambiguate_operation_ids=False)`.

## Callbacks

A `callbacks` map on a route is emitted verbatim into the operation's `callbacks`
field. It documents requests your API makes back to the caller in response to the
original operation.

```python title="app.py"
from veloce import Request, Veloce

app = Veloce()

invoice_callback = {
    "invoicePaid": {
        "{$request.body#/callbackUrl}": {
            "post": {
                "requestBody": {
                    "content": {
                        "application/json": {
                            "schema": {"type": "object"}
                        }
                    }
                },
                "responses": {"200": {"description": "Acknowledged."}},
            }
        }
    }
}


@app.post("/invoices", callbacks=invoice_callback)
async def create_invoice(request: Request):
    return {"status": "pending"}
```

!!! warning "Callbacks are documentation-only and pass through verbatim"
    Unlike FastAPI, Veloce does not build callback operations from a callback
    router — there is no callback-router introspection. You supply the callback
    object yourself as a raw OpenAPI Callback dict, and Veloce copies it into the
    operation unchanged. Veloce neither validates the structure nor sends the
    callback requests at runtime; wiring the outbound request is your code's job.

## Webhooks

`app.webhooks` is a router whose routes are pure documentation. Register handlers
on it to emit an OpenAPI 3.1 `webhooks` map describing events an external service
should expect from your API. These routes are never served — they only document
the payload shape.

```python title="app.py"
from pydantic import BaseModel

from veloce import Veloce


class Subscription(BaseModel):
    username: str
    monthly_fee: float


app = Veloce()


@app.webhooks.post("/new-subscription")
async def new_subscription(body: Subscription):
    """Sent when a new subscription is created."""
```

The first Pydantic-typed parameter of a webhook handler becomes the documented
request body. The event key is the registered path with surrounding slashes
stripped (`new-subscription` above).

```python
from pydantic import BaseModel

from veloce import TestClient, Veloce


class Subscription(BaseModel):
    username: str


app = Veloce()


@app.webhooks.post("/new-subscription")
async def new_subscription(body: Subscription):
    pass


client = TestClient(app)
schema = client.get("/openapi.json").json()

assert "new-subscription" in schema["webhooks"]
assert "post" in schema["webhooks"]["new-subscription"]
```

!!! note
    Webhook operationIds flow through the same disambiguation pass as regular
    routes, so a webhook sharing a handler name with a path operation still emits
    a unique `operationId`.

## Validating the document

Veloce can run a lightweight structural pass over the assembled document —
checking for malformed operations and dangling `$ref`s — and raise a `ValueError`
naming the offending path. It is opt-in via `validate_openapi`, which defaults to
the app's `debug` flag, so production builds pay nothing.

```python
from veloce import Veloce

app = Veloce(validate_openapi=True)
```

!!! note
    Veloce builds the document as plain dicts; there are no Pydantic OpenAPI model
    classes to import. Read and mutate the schema as nested dictionaries.

## Next steps

- [Security schemes](security-schemes.md) — how `Security()` schemes populate `components.securitySchemes`.
- [Requests and responses](requests-responses.md) — `response_model` and the request/response contract the schema documents.
- [Parameters](parameters.md) — how path, query, header and cookie parameters become OpenAPI parameter objects.
- [Routing](routing.md) — the full path-operation decorator surface.
- Full signatures are in the [API reference](../reference/index.md).
