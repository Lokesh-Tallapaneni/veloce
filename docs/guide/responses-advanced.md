---
description: Advanced response control in Veloce — response_class / default_response_class, additional OpenAPI responses with responses=, typed response cookies and headers, and changing the status code from an injected Response.
tags: [responses, cookies, headers, status]
---

# Advanced responses

By default a handler that returns a dict or model is serialised to a
[`JSONResponse`](../reference/responses.md#veloce.JSONResponse), with status `200` and the
headers Veloce computes for you.

This page covers the levers for taking that control back:

- picking a different [`Response`](../reference/responses.md#veloce.Response) class
- documenting extra status codes in the OpenAPI schema
- setting cookies and headers with typed helpers
- changing the status code from inside the handler

For the basics of returning a response, see [Requests and responses](requests-responses.md).

## Choosing a response class

Pass `response_class=` to a route to control how the handler's return value is
encoded. The class is called with the return value, so the string this handler
returns is sent as HTML rather than being JSON-encoded:

```python title="app.py"
from veloce import HTMLResponse, Veloce

app = Veloce()


@app.get("/page", response_class=HTMLResponse)
async def page():
    return "<h1>Hello</h1>"
```

The default is `JSONResponse`. Returning a `Response` instance from the handler
always wins over `response_class` — the instance is sent as-is.

A text response class encodes `str` or `bytes`. Returning a `dict` or a `list`
under one raises `TypeError` naming both, since there is no sensible rendering of
a mapping as HTML — declare `response_class=JSONResponse` on that route, or
return a string.

### A default class for the whole app

Set `default_response_class=` on the app (or a [`Blueprint`](blueprints.md)) to
change the fallback for every route that does not declare its own. A route-level
`response_class=` still overrides it:

```python title="app.py"
from veloce import ORJSONResponse, Veloce

app = Veloce(default_response_class=ORJSONResponse)


@app.get("/items")
async def items():
    return [{"id": 1}, {"id": 2}]
```

!!! note
    `ORJSONResponse` is a semantic alias for `JSONResponse` (both encode with
    orjson). Declaring it communicates the encoder choice; it does not change the
    output bytes.

### A custom JSON content type

Subclass `JSONResponse` and override `default_media_type` to ship a JSON suffix
type such as `application/problem+json` without re-implementing the encoder:

```python
from veloce import JSONResponse


class ProblemJSON(JSONResponse):
    default_media_type = "application/problem+json"
```

Use it as `@app.get("/x", response_class=ProblemJSON)`. The class is also honoured
by `ProblemJSON.from_bytes(...)` when you already hold encoded JSON.

## Documenting additional responses

A route advertises one success response in its OpenAPI document. Pass `responses=`
to document the other status codes the operation can return — a `{status: spec}`
mapping where each `spec` may carry a `model` (a Pydantic model for the response
body schema), a `description`, and any free-form OpenAPI keys (`headers`, `links`):

```python title="app.py"
from pydantic import BaseModel

from veloce import Veloce, status

app = Veloce()


class Item(BaseModel):
    id: int
    name: str


class Error(BaseModel):
    detail: str


@app.get(
    "/items/{item_id}",
    responses={
        status.HTTP_404_NOT_FOUND: {"model": Error, "description": "Item not found"},
    },
)
async def get_item(item_id: int) -> Item:
    return Item(id=item_id, name="Widget")
```

The `404` now appears in the generated schema alongside the `200`. The `model`
schema is emitted under `application/json` only — see the warning below.

!!! warning "`responses=` model schemas are JSON-only"
    A `model` entry generates its body schema under `application/json`. To
    document a non-JSON media type, supply the OpenAPI `content` object yourself
    as a free-form key instead of `model`:

    ```python
    responses={
        200: {
            "description": "A CSV export",
            "content": {"text/csv": {"schema": {"type": "string"}}},
        },
    }
    ```

!!! note
    Set `responses=` on the app or a [`Blueprint`](blueprints.md) to overlay the
    same entries onto every route; a route's own `responses=` merges on top, and
    per-route status codes win.

`responses=` only shapes the schema. It does not make the handler return those
statuses — that is the job of `status_code=`, `HTTPException`, or returning a
`Response` directly.

## Status codes

Set the default success status with `status_code=`. The named constants in
[`status`](../reference/index.md) read better than bare integers:

```python title="app.py"
from veloce import Veloce, status

app = Veloce()


@app.post("/items", status_code=status.HTTP_201_CREATED)
async def create_item():
    return {"id": 1}
```

The handler returns a plain dict and the response goes out as `201 Created`.

### Returning a different status per request

When the status depends on the result, return a response object with the status
you want. It overrides the route's `status_code=`:

```python title="app.py"
from veloce import JSONResponse, Veloce, status

app = Veloce()

_items: dict[int, str] = {}


@app.put("/items/{item_id}")
async def upsert(item_id: int, name: str):
    created = item_id not in _items
    _items[item_id] = name
    code = status.HTTP_201_CREATED if created else status.HTTP_200_OK
    return JSONResponse({"id": item_id, "name": name}, status_code=code)
```

## Dynamic status from an injected `Response`

Returning a `JSONResponse` bypasses the route's `response_model` filtering and
`response_class`. When you want the normal serialisation but still need to set the
status (or a header or cookie) conditionally, declare a `response: Response` parameter. Veloce injects a
fresh response object whose status and headers are merged onto the final one:

```python title="app.py"
from veloce import Response, Veloce, status

app = Veloce()

_items: dict[int, str] = {}


@app.put("/items/{item_id}")
async def upsert(item_id: int, name: str, response: Response):
    if item_id not in _items:
        response.status_code = status.HTTP_201_CREATED
    _items[item_id] = name
    return {"id": item_id, "name": name}
```

The returned dict is still serialised through the route's normal path; only the
status you set is applied.

!!! note
    The injected `Response` is created once per request and shared with any
    dependency that also declares the parameter. Until you assign
    `response.status_code`, it carries the sentinel `0`, meaning "not set" — the
    dispatcher leaves the real status untouched.

## Headers

Set a one-off header through the injected response's `headers` dict, or use the
typed setters and properties on [`Response`](../reference/responses.md#veloce.Response) for
the standard fields:

```python title="app.py"
from veloce import Response, Veloce

app = Veloce()


@app.get("/report")
async def report(response: Response):
    response.headers["X-Report-Id"] = "42"
    response.set_cache_control(max_age=3600, public=True)
    return {"ok": True}
```

The typed helpers build correct values for you instead of hand-formatting header
strings:

| Helper | Sets | Notes |
| --- | --- | --- |
| `set_cache_control(...)` | `Cache-Control` | Combines directives in RFC 9111 order. |
| `add_vary(*names)` | `Vary` | Merges and de-duplicates case-insensitively. |
| `set_etag(value, weak=False)` | `ETag` | Quotes the value; `add_etag()` derives one from the body. |
| `set_content_disposition(...)` | `Content-Disposition` | Builds the RFC 6266 attachment/inline value. |
| `last_modified = ...` | `Last-Modified` | Accepts `datetime`, Unix timestamp, or string. |
| `retry_after = ...` | `Retry-After` | Accepts `int`, `timedelta`, or `datetime`. |

### Conditional responses and `Vary`

[`add_vary`](../reference/responses.md#veloce.Response.add_vary) tells caches which request
headers the response depends on, so a `Vary: Cookie` response is not served to a
different user. [`make_conditional`](../reference/responses.md#veloce.Response.make_conditional)
downgrades a response to `304 Not Modified` when the request's `If-None-Match` or
`If-Modified-Since` preconditions already match (RFC 9110 §13):

```python title="app.py"
from veloce import Response, Veloce

app = Veloce()


@app.get("/profile")
async def profile(request):
    response = Response(body=b"<p>hi</p>", content_type="text/html")
    response.add_vary("Cookie")
    response.add_etag()
    return response.make_conditional(request)
```

### Write-side preconditions

`make_conditional` handles the *read* side — it turns a `GET` into a `304` when
the client already has the current representation.
[`check_preconditions`](../reference/responses.md#veloce.Response.check_preconditions)
handles the *write* side: it raises `412 Precondition Failed` when the client's
"only write if it has not changed" condition no longer holds, which is the guard
against a lost update.

```python title="app.py"
from veloce import JSONResponse, Veloce

app = Veloce()

DOCUMENTS = {"readme": {"body": "hello", "modified": 1_700_000_000.0}}


@app.put("/documents/{doc_id}")
async def update(doc_id: str, request):
    document = DOCUMENTS[doc_id]
    response = JSONResponse({"id": doc_id})
    response.add_etag()
    response.last_modified = document["modified"]
    # Raises 412 if the client's precondition no longer holds.
    return response.check_preconditions(request)
```

Both forms of the condition are honoured, in the precedence RFC 9110 §13.2.2
defines: `If-Match` is checked first, and `If-Unmodified-Since` only when
`If-Match` is absent. `If-Match: *` passes whenever a representation exists. A
concrete `If-Match` tag is compared *strongly* (§8.8.3.1), so the weak ETags
`add_etag` produces never satisfy one — send `If-Unmodified-Since` instead when
your ETags are weak.

Call it inside a handler, where the raised `HTTPException` becomes a response.

!!! note "Changed in version 0.13"
    `check_preconditions` enforces `If-Unmodified-Since`. It previously checked
    only `If-Match`, so a client sending a date rather than an ETag received no
    lost-update protection.

## Cookies

Set cookies with [`set_cookie`](../reference/responses.md#veloce.Response.set_cookie) on the
injected response. The validated parameters build a correct RFC 6265 `Set-Cookie`
header, and multiple calls append rather than overwrite:

```python title="app.py"
from veloce import Response, Veloce

app = Veloce()


@app.post("/login")
async def login(response: Response):
    response.set_cookie("session", "abc123", httponly=True, samesite="Lax")
    return {"ok": True}
```

`samesite` defaults to `"Lax"`. Pass `samesite="None"` (with `secure=True`) for a
cross-site cookie, or `samesite=None` to omit the attribute entirely.

!!! warning "Match the attributes when deleting"
    A browser only replaces an existing cookie when `Path`, `Domain`, and the
    `Secure` / `SameSite` / `Partitioned` attributes match. Pass the same flags to
    [`delete_cookie`](../reference/responses.md#veloce.Response.delete_cookie) that you used
    to set it, or the cookie is stored twice instead of removed.

### Partitioned cookies

Pass `partitioned=True` for a CHIPS cookie (Cookies Having Independent Partitioned
State), keyed to the top-level site so embedded third-party contexts each get an
isolated jar. `Partitioned` requires `Secure`, so it is only emitted when
`secure=True`:

```python
response.set_cookie(
    "tracker", "xyz", secure=True, samesite="None", partitioned=True
)
```

### `__Host-` and `__Secure-` prefixes

Pass `prefix="host"` or `prefix="secure"` to add the RFC 6265bis §4.1.3 name
prefix and enforce its invariants. `"secure"` requires `secure=True`; `"host"`
also requires `path="/"` and no `domain`. A violation raises `ValueError`:

```python
response.set_cookie("session", "abc123", secure=True, prefix="host")
```

The cookie travels on the wire as `__Host-session`.

## Testing responses

Use the in-memory [`TestClient`](../reference/testing.md#veloce.TestClient) to assert the
status, headers, and cookies without a server:

```python
from veloce import Response, TestClient, Veloce, status

app = Veloce()

_items: dict[int, str] = {}


@app.put("/items/{item_id}")
async def upsert(item_id: int, name: str, response: Response):
    if item_id not in _items:
        response.status_code = status.HTTP_201_CREATED
        response.set_cookie("seen", "1")
    _items[item_id] = name
    return {"id": item_id, "name": name}


client = TestClient(app)

resp = client.put("/items/1?name=Widget")
assert resp.status_code == 201
assert resp.cookies["seen"] == "1"

resp = client.put("/items/1?name=Gadget")
assert resp.status_code == 200
```

## Next steps

- Return values, status, and the response family basics — see [Requests and responses](requests-responses.md).
- Shape and filter the response body with models — see [Request models](request-models.md).
- Control how values are serialised to JSON — see [JSON and encoding](encoding.md).
- Document operations and schemas — see [OpenAPI](openapi.md).
- Full signatures are in the [API reference](../reference/index.md).
