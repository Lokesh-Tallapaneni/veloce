---
description: >-
  Generate typed API clients and SDKs from a Veloce app's OpenAPI document — the
  /openapi.json endpoint, driving openapi-generator and openapi-ts, and shaping
  method names with operation_id and automatic collision disambiguation.
tags: [openapi, clients, sdk, codegen]
---

# Generating clients and SDKs

Veloce serves a standards-compliant OpenAPI 3.1 document at `/openapi.json`, so any
OpenAPI-aware code generator can produce a typed client for your API. This page covers
fetching the document, running two common generators, and controlling the generated
method names with `operation_id` on the [`Router`](../reference/routers.md#veloce.Router) decorators and Veloce's
automatic collision handling.

## The OpenAPI document

Every [`Veloce`](../reference/application.md#veloce.Veloce) app exposes its schema at `/openapi.json`
by default. Generators read this single file — they never need access to your source.

```python title="app.py"
from pydantic import BaseModel

from veloce import Veloce

app = Veloce(title="Items API", version="1.0.0")


class Item(BaseModel):
    name: str
    price: float


@app.get("/items/{item_id}")
async def read_item(item_id: int) -> Item:
    return Item(name="Widget", price=9.99)


if __name__ == "__main__":
    app.run(port=8000)
```

The schema is built lazily and cached on first request. Fetch it from a running server,
or render it in-process with the [`TestClient`](../reference/testing.md#veloce.TestClient) so the
generator step needs no live port.

```python
from pydantic import BaseModel

from veloce import TestClient, Veloce

app = Veloce(title="Items API", version="1.0.0")


class Item(BaseModel):
    name: str
    price: float


@app.get("/items/{item_id}")
async def read_item(item_id: int) -> Item:
    return Item(name="Widget", price=9.99)


client = TestClient(app)

schema = client.get("/openapi.json").json()
assert schema["openapi"].startswith("3.1")
assert schema["info"]["title"] == "Items API"
assert "/items/{item_id}" in schema["paths"]
```

!!! note
    The path is set by the `openapi_url` constructor argument (default `"/openapi.json"`).
    Pass `openapi_url=None` to disable the schema endpoint entirely, in which case you must
    dump the document to a file with the [`TestClient`](../reference/testing.md#veloce.TestClient)
    before running a generator.

## Dumping the schema to a file

Most generators take a local file or a URL. To produce a committable `openapi.json`,
render it once and write it out as part of a build step.

```python title="dump_schema.py"
import json
from pathlib import Path

from veloce import TestClient, Veloce

app = Veloce(title="Items API", version="1.0.0")


@app.get("/ping")
async def ping() -> dict[str, str]:
    return {"status": "ok"}


schema = TestClient(app).get("/openapi.json").json()
Path("openapi.json").write_text(json.dumps(schema, indent=2))
```

Run it with `python dump_schema.py`; the resulting `openapi.json` is the input for every
generator below.

## Generating a client with openapi-generator

[openapi-generator](https://openapi-generator.tech/) emits clients for dozens of
languages. Point it at the running server or the dumped file.

```bash
# from a running app
openapi-generator generate \
  -i http://127.0.0.1:8000/openapi.json \
  -g python \
  -o ./client

# from a committed file
openapi-generator generate -i openapi.json -g typescript-fetch -o ./client
```

The generated method names come straight from each operation's `operationId` (see below),
so naming your operations well is what makes the resulting SDK read cleanly.

## Generating a TypeScript client with openapi-ts

[openapi-ts](https://heyapi.dev/) produces a TypeScript client and types directly from the
document.

```bash
npx @hey-api/openapi-ts -i http://127.0.0.1:8000/openapi.json -o src/client
```

Each generated function is keyed off the operation's `operationId`; the request and
response types are generated from the Pydantic models that Veloce emitted into the
document's `components.schemas`.

## How `operation_id` shapes method names

Every operation in the document carries an `operationId`. Generators turn that string into
the client method or function name, so it is the single most important field for SDK
ergonomics. By default Veloce derives it as `<handler-name>_<method>`.

```python
from veloce import TestClient, Veloce

app = Veloce()


@app.get("/items")
async def list_items() -> list[str]:
    return ["a", "b"]


client = TestClient(app)
schema = client.get("/openapi.json").json()

assert schema["paths"]["/items"]["get"]["operationId"] == "list_items_get"
```

Pass `operation_id=` on the route to pin an explicit, stable name. A pinned id is emitted
verbatim and is never rewritten, which keeps the generated method name stable across
refactors even if you rename the handler function.

```python
from veloce import TestClient, Veloce

app = Veloce()


@app.get("/items", operation_id="listItems")
async def list_items() -> list[str]:
    return ["a", "b"]


client = TestClient(app)
schema = client.get("/openapi.json").json()

assert schema["paths"]["/items"]["get"]["operationId"] == "listItems"
```

!!! tip "Pin `operation_id` for a stable SDK surface"
    Auto-generated ids change when you rename a handler. If downstream consumers depend on
    the generated method names, set `operation_id=` explicitly on every public route so the
    client API does not shift underneath them.

## Automatic disambiguation of colliding ids

The OpenAPI specification (OpenAPI 3.1 Sec. 4.8.10) requires every `operationId` to be
unique across the document; duplicates break code generation. Two handlers that share a
function name on different paths would otherwise emit the same auto-generated id. Veloce
detects these collisions and deterministically suffixes the duplicates with a
path-derived tail, so the document a generator sees is always valid.

```python
from veloce import TestClient, Veloce

app = Veloce()


@app.get("/items")
async def search() -> list[str]:
    return []


@app.get("/users")
async def search() -> list[str]:  # noqa: F811  -- same function name, different route
    return []


client = TestClient(app)
schema = client.get("/openapi.json").json()

ids = {
    schema["paths"]["/items"]["get"]["operationId"],
    schema["paths"]["/users"]["get"]["operationId"],
}
# Both started as `search_get`; the collision is resolved to two distinct ids.
assert len(ids) == 2
assert "search_get" in ids
assert "search_get__users" in ids
```

| Operation | Resulting id |
| --- | --- |
| The first operation to claim a bare id | keeps it |
| Each later duplicate | becomes `<id>__<path-segments>` (and `<id>__<path-segments>_<n>` if even that collides) |

A single aggregated `WARNING` is logged listing every collision and its resolution, so you can find
and pin the affected routes.

!!! warning "Auto-suffixed ids are stable but not pretty"
    Disambiguation guarantees a **valid** document, not a readable SDK: `search_get__users`
    is an awkward client method name. Treat the warning as a prompt to set `operation_id=`
    on the colliding routes. Explicitly pinned ids are reserved before disambiguation runs,
    so an auto id colliding with a pinned id suffixes the **auto** one, never the pinned one.

!!! warning "Two identical explicit ids are a hard error in your schema"
    Disambiguation only rewrites auto-generated ids. If you pin the **same** `operation_id=`
    on two routes, Veloce cannot silently rename a value you chose; it leaves both in place,
    logs a separate warning, and the duplicate violates OpenAPI 3.1 Sec. 4.8.10. Rename one
    of the pair yourself.

To turn the behaviour off, construct the app with `disambiguate_operation_ids=False`
(default `True`). With it off, colliding auto-generated ids are emitted as-is and the
resulting document may be rejected by strict generators — only do this if you guarantee
uniqueness another way.

```python
from veloce import Veloce

app = Veloce(disambiguate_operation_ids=False)
```

!!! note "No `generate_unique_id_function`"
    Veloce has no FastAPI-style `generate_unique_id_function` hook. Shape ids with
    `operation_id=` per route and rely on automatic disambiguation for the rest. See
    [Migrating from FastAPI](../surpass/migrating-from-fastapi.md) for the full divergence list.

## Grouping operations with tags

Most generators split the client into modules or classes by the operation's `tags`. Tag
your routes to produce a cleanly grouped SDK rather than one flat namespace.

```python
from veloce import TestClient, Veloce

app = Veloce()


@app.get("/items", tags=["items"])
async def list_items() -> list[str]:
    return []


@app.get("/users", tags=["users"])
async def list_users() -> list[str]:
    return []


client = TestClient(app)
schema = client.get("/openapi.json").json()

assert schema["paths"]["/items"]["get"]["tags"] == ["items"]
assert schema["paths"]["/users"]["get"]["tags"] == ["users"]
```

A route with `tags=["items"]` is grouped under an `Items` client class (or `items`
module) by most generators, mirroring how the operation appears in the interactive docs.

## Next steps

- [OpenAPI, metadata and docs](../guide/openapi.md) — shape the document the generators read.
- [Migrating from FastAPI](../surpass/migrating-from-fastapi.md) — `operation_id=` in place of `generate_unique_id_function`.
- [Testing](../guide/testing.md) — render `/openapi.json` in-process with the `TestClient`.
- Full signatures are in the [API reference](../reference/index.md).
