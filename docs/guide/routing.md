---
description: Define HTTP routes in Veloce with a radix-tree router, typed path converters (int, float, uuid, path), query parameters, sub-routers, and reverse URLs.
tags: [routing, radix, async, asgi]
---

# Routing

Veloce matches requests with a **radix tree**. Routes are registered at
import time; path parameters are extracted and type-coerced during the
tree traversal, not afterwards.

## Route decorators

Every HTTP method has a decorator on the application:

```python title="app.py"
from veloce import Request, Veloce

app = Veloce()


@app.get("/")
async def index(request: Request):
    return {"ok": True}


@app.post("/items")
async def create_item(request: Request):
    return {"created": True}
```

`get`, `post`, `put`, `patch`, `delete`, `head`, `options`, and `trace`
are all available.

## Path parameters

Wrap a segment in braces and annotate the handler parameter — Veloce
coerces the value to the annotated type:

```python
@app.get("/users/{user_id}")
async def get_user(user_id: int):
    # user_id is an int; "/users/abc" never reaches this handler
    return {"id": user_id}
```

### Typed converters

A converter can be declared inline. A segment the converter rejects is a
**route miss** (404), not a validation error:

| Rule                     | Matches                          |
|--------------------------|----------------------------------|
| `{id:int}`               | a decimal integer                |
| `{ratio:float}`          | a decimal float                  |
| `{key:uuid}`             | a canonical RFC 4122 UUID        |
| `{rest:path}`            | the remainder of the URL (greedy)|
| `{c:any(red,green)}`     | one of a fixed set of values     |

```python
@app.get("/files/{rest:path}")
async def serve(rest: str):
    return {"path": rest}   # "/files/a/b/c.txt" -> rest == "a/b/c.txt"
```

Use `register_converter` to add your own.

## Query parameters

A handler parameter that is not a path parameter — and is not the
`Request`, a dependency, or a request-body model — is read from the
query string and coerced to its annotated type. A default value makes
the parameter optional; without one it is a required query parameter,
and a missing value produces a `422`:

```python
@app.get("/search")
async def search(request: Request, q: str = "", page: int = 1, limit: int = 10):
    return {"query": q, "page": page, "limit": limit}
```

`/search?q=veloce&page=2` yields `q="veloce"`, `page=2`, `limit=10`.

## Grouping routes with a Router

A `Router` is a self-contained group of routes with a shared prefix and
metadata. Mount it onto the app with `include_router`:

```python
from veloce import Request, Router, Veloce

app = Veloce()
api = Router(prefix="/api/v2", tags=["v2"])


@api.get("/status")
async def status(request: Request):
    return {"status": "ok"}


app.include_router(api)        # now serving GET /api/v2/status
```

Routers can be nested and carry their own `dependencies=` and
`responses=`. Scoped `before_request` / `after_request` /
`teardown_request` hooks are a `Blueprint` feature — a plain `Router`
has none.

## Reverse URLs

Build a URL for a named route instead of hard-coding paths:

```python
@app.get("/users/{user_id}", name="user-detail")
async def user_detail(user_id: int):
    ...

url = app.url_for("user-detail", user_id=7)   # "/users/7"
```

When a route parameter declares a converter, `url_for` validates the value
against that converter before building the URL, so a reversed URL is guaranteed
to resolve. A value the matcher would reject raises rather than producing a dead
link:

```python
@app.get("/items/{id:int}", name="item")
async def item(id: int):
    ...

app.url_for("item", id=42)      # "/items/42"
app.url_for("item", id="abc")   # raises - "abc" is not a valid int segment
```

Parameters without a typed converter (a bare `{name}` or a raw-regex segment
like `{id:[0-9]+}`) accept any stringifiable value.

## See also

- [Requests & responses](requests-responses.md)
- [Dependency injection](dependency-injection.md)
- [Middleware](middleware.md)
