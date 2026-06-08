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
| `{d:date}`               | an ISO 8601 date → `datetime.date` |
| `{t:time}`               | an ISO 8601 time → `datetime.time` |
| `{ts:datetime}`          | an ISO 8601 datetime → `datetime.datetime` |
| `{dur:timedelta}`        | an ISO 8601 duration → `datetime.timedelta` |
| `{amount:decimal}`       | a decimal literal → `decimal.Decimal` |

```python
@app.get("/files/{rest:path}")
async def serve(rest: str):
    return {"path": rest}   # "/files/a/b/c.txt" -> rest == "a/b/c.txt"
```

The temporal and decimal converters coerce the matched segment to the
corresponding Python type, so the handler receives a `datetime.date`,
`datetime.datetime`, `datetime.time`, `datetime.timedelta`, or
`decimal.Decimal` rather than a string. A `Z` suffix on a datetime/time is
accepted (normalised to `+00:00`). `timedelta` requires a full ISO 8601
duration with at least one component (`P1DT2H`); a bare number is a route
miss.

```python
@app.get("/reports/{day:date}")
async def report(day):
    return {"weekday": day.weekday()}   # "/reports/2026-06-03" -> date(2026, 6, 3)
```

Use `register_converter` to add your own.

### Static segments win over parameters

When a static path and a parameterised path overlap at the same position, the
static one always matches first. The radix tree tries an exact-segment lookup
before scanning parameter children, so registration order is irrelevant:

```python
@app.get("/users/me")
async def me(request: Request):
    return {"user": "current"}


@app.get("/users/{user_id}")
async def get_user(user_id: int):
    return {"id": user_id}
```

`/users/me` resolves to `me()` even though `/users/{user_id}` could also match,
and even if `get_user` were registered first.

!!! note "No ordering footgun"
    Unlike a regex router where the first registered pattern wins, Veloce's
    match precedence is structural: an exact segment is tried before any
    parameter child, and the legacy `*` wildcard is tried last. You do not have
    to register specific routes before general ones.

### Constrained converters

The `int`, `float`, and `str` converters accept arguments, so a bound takes
part in matching rather than only producing a handler-layer error. A value
outside the bound is a **route miss** (404), not a `422`:

| Rule                              | Matches                                  |
|-----------------------------------|------------------------------------------|
| `{n:int(min=1,max=100)}`          | an integer in `[1, 100]`                 |
| `{n:int(min=1)}`                  | an integer `>= 1`                        |
| `{n:int(signed=False)}`           | an unsigned integer (no leading `-`)     |
| `{x:float(min=0.0,max=1.0)}`      | a float in `[0.0, 1.0]`                  |
| `{code:str(length=2)}`            | a string of exactly 2 characters         |
| `{slug:str(minlength=3,maxlength=64)}` | a string of 3–64 characters         |

```python
@app.get("/page/{n:int(min=1,max=100)}")
async def page(n: int):
    return {"page": n}     # "/page/0" and "/page/101" are 404, "/page/50" is 200
```

The constraints are enforced whether the route resolves on the radix fast path
or the regex fallback, and `url_for` rejects a value the converter would never
match. A converter with no arguments behaves exactly as before.

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

### Duplicate routes

Registering a second handler for the same path and HTTP method raises
`DuplicateRouteError` at registration time, so an accidental route shadowing
fails loudly at startup instead of dispatching to the wrong handler. Change the
policy with `on_duplicate` on `Veloce(...)` or `Router(...)`:

```python
app = Veloce(on_duplicate="error")    # default: raise DuplicateRouteError
app = Veloce(on_duplicate="warn")     # log a warning and replace
app = Veloce(on_duplicate="override") # replace silently
```

Including the same router twice carries the same handler and is an idempotent
re-mount, never reported as a conflict.

## Path operation configuration

Every route decorator accepts a set of OpenAPI knobs that shape how the
operation appears in the generated schema and the docs UI. They are
metadata-only — they do not change how a request is matched or dispatched.

```python title="app.py"
from veloce import Veloce

app = Veloce()


@app.post(
    "/items",
    tags=["items"],
    summary="Create an item",
    response_description="The created item",
    status_code=201,
)
async def create_item(name: str):
    """Add a new item to the catalogue.

    The full request body is validated before this handler runs.
    """
    return {"name": name}
```

The handler's docstring becomes the operation `description`, so an
operation's prose lives next to the code it documents.

| Option | Default | Purpose |
| --- | --- | --- |
| `tags` | `[]` | OpenAPI tags for the operation, combined with the router-level tags. |
| `summary` | route name | Short one-line summary shown in the docs UI. |
| `description` | handler docstring | Long-form description; falls back to `handler.__doc__`. |
| `deprecated` | `False` | Marks the operation deprecated in the schema. |
| `response_description` | `"Successful Response"` | Description of the `200`/`status_code` response. |
| `status_code` | `200` | Default success status code for the response. |
| `operation_id` | `"{name}_{method}"` | Explicit OpenAPI `operationId`; pin it for stable client codegen. |
| `include_in_schema` | `True` | Register the route but omit it from the OpenAPI document when `False`. |
| `openapi_extra` | `None` | Arbitrary dict deep-merged onto this route's operation object. |

A docstring is the idiomatic way to document an operation:

```python
@app.get("/health")
async def health():
    """Report whether the service is up.

    Used by the load balancer's health check.
    """
    return {"status": "ok"}
```

!!! warning "Docstring is used whole — no summary split"
    Veloce uses the entire docstring as the operation `description`. It does
    **not** treat the first line as `summary` and the rest as `description`.
    Set `summary=` explicitly when you want a short title; otherwise the
    summary defaults to the route name, not the docstring's first line.

### Deprecating and hiding routes

`deprecated=True` keeps the route live but flags it in the schema; clients
and the docs UI render it struck through. `include_in_schema=False` keeps
the route fully functional but drops it from the OpenAPI document entirely:

```python
@app.get("/legacy", deprecated=True)
async def legacy():
    return {"detail": "use /v2 instead"}


@app.get("/internal/metrics", include_in_schema=False)
async def metrics():
    return {"requests": 1234}
```

### Stable operation IDs

`operation_id` defaults to `"{name}_{method}"` (e.g. `get_user_get`).
Duplicate auto-generated IDs are disambiguated with a numeric suffix and a
warning. Pin `operation_id=` to get a stable identifier for generated
clients:

```python
@app.get("/users/{user_id}", operation_id="readUser")
async def get_user(user_id: int):
    return {"id": user_id}
```

### Extending the operation object

`openapi_extra` is deep-merged onto the operation object, so you can attach
fields Veloce does not surface directly. Dict keys merge recursively; list
values overwrite:

```python
@app.get(
    "/report",
    openapi_extra={"x-internal": True, "responses": {"503": {"description": "Down"}}},
)
async def report():
    return {"ok": True}
```

!!! note
    These options live on every method decorator (`get`, `post`, ...) and on
    `Router` routes. Route-level `tags` are appended to the router's `tags`;
    a route-level `operation_id` or `summary` overrides any inherited value.

Build the document and inspect the configured operation directly with the
in-memory client:

```python
from veloce import TestClient, Veloce

app = Veloce()


@app.post("/items", tags=["items"], summary="Create an item", status_code=201)
async def create_item(name: str):
    """Add a new item to the catalogue."""
    return {"name": name}


client = TestClient(app)

schema = client.get("/openapi.json").json()
op = schema["paths"]["/items"]["post"]
assert op["summary"] == "Create an item"
assert op["tags"] == ["items"]
assert op["description"] == "Add a new item to the catalogue."
```

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
