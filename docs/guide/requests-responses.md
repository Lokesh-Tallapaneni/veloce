---
description: >-
  Read request fields, headers, cookies, query, the raw body, the stream, and form data, then build responses with status codes, a Response constructed directly, sync streaming, NDJSON, and response cookies and headers.
tags: [request, response, streaming, status]
---

# Requests and responses

[`Request`](../reference/requests.md#veloce.Request) exposes the incoming message; the
[`Response`](../reference/responses.md#veloce.Response) family builds what goes back. A
handler can return a plain value and let Veloce coerce it, or construct a
response itself for full control over status, body, and headers.

## Reading the request

Annotate a parameter as `Request` and Veloce passes the live request in. All
parsing is lazy — properties you never read cost nothing.

```python title="app.py"
from veloce import Request, Veloce

app = Veloce()


@app.get("/inspect")
async def inspect(request: Request):
    return {
        "method": request.method,
        "path": request.path,
        "query": dict(request.query_params),
        "user_agent": request.headers.get("user-agent", ""),
        "host": request.host,
        "client": request.client.host if request.client else None,
    }
```

- `request.query_params` and `request.headers` are multi-value mappings:
  `headers["x"]` (or `.get("x")`) returns the first value, `headers.getlist("x")`
  returns every value.
- `request.client` is an `Address(host, port)` or `None` for synthetic requests.
- `request.state` is a per-request scratch namespace — use `request.state.foo`
  for your own data, never a bare attribute on `request`.

### Client IP

Five properties expose the connecting client's address:

| Property | Returns | Notes |
| --- | --- | --- |
| `request.client` | `Address(host, port)` or `None` | ASGI scope `client`; honours ProxyFix. |
| `request.client_host` | `str` or `None` | The IP portion of `client`. |
| `request.client_port` | `int` or `None` | The port portion of `client`. |
| `request.remote_addr` | `str` or `None` | Flask-style alias for `client_host`. |
| `request.access_route` | `list[str]` | `X-Forwarded-For` chain + connecting peer. |

```python title="app.py"
from veloce import Request, Veloce

app = Veloce()


@app.get("/ip")
async def ip(request: Request):
    return {
        "client_host": request.client_host,
        "client_port": request.client_port,
        "remote_addr": request.remote_addr,
        "access_route": request.access_route,
    }
```

`client`, `client_host`, and `remote_addr` return `None` when the transport
reports no peer. `TestClient` supplies a synthetic one, so under it they read
`Address(host="testclient", port=50000)`, `"testclient"` and `"testclient"`, and
`access_route` is `["testclient"]` — assert against those rather than `None`.

!!! warning "Forwarded headers are spoofable"
    `X-Forwarded-For` and `access_route` reflect whatever the caller sent. Only
    trust them when you have added [`ProxyFix`](../reference/middleware.md#veloce.ProxyFix)
    and configured the correct hop depth — see
    [Behind a proxy](behind-a-proxy.md).

### Cookies and query parameters

`request.cookies` parses the `Cookie` header lazily; `request.query_params`
parses the URL query string.

```python title="app.py"
from veloce import Request, Veloce

app = Veloce()


@app.get("/search")
async def search(request: Request):
    term = request.query_params.get("q", "")
    tags = request.query_params.getlist("tag")
    session_id = request.cookies.get("session_id")
    return {"q": term, "tags": tags, "session_id": session_id}
```

```python
from veloce import Request, TestClient, Veloce

app = Veloce()


@app.get("/search")
async def search(request: Request):
    return {
        "q": request.query_params.get("q", ""),
        "tags": request.query_params.getlist("tag"),
        "session_id": request.cookies.get("session_id"),
    }


client = TestClient(app)
client.cookies["session_id"] = "xyz"

resp = client.get("/search?q=veloce&tag=a&tag=b")
assert resp.status_code == 200
assert resp.json() == {"q": "veloce", "tags": ["a", "b"], "session_id": "xyz"}
```

## Reading the raw body

The body accessors are awaitable so the same code works whether the body is
already buffered or still streaming in.

| Accessor | Returns |
| --- | --- |
| `await request.body()` | the full body as `bytes`. |
| `await request.json()` | the body parsed as JSON (raises `400` on malformed JSON, returns `None` for an empty body, and `None` when `Content-Type` declares the body is not JSON). |
| `await request.text()` | the body decoded as UTF-8. |
| `await request.get_data(as_text=True)` | the body decoded via the `Content-Type` charset. |

!!! warning "A declared non-JSON body is not parsed"
    `await request.json()` returns `None` when the request declares a
    `Content-Type` that is not JSON. `text/plain`, `multipart/form-data` and
    `application/x-www-form-urlencoded` are the types a cross-origin form or
    `fetch` may send with **no CORS preflight**, so parsing a body under one of
    them lets an attacker drive the endpoint through a cookie-authenticated
    victim's browser. An absent header asserts nothing and is still parsed; a
    `+json` suffix such as `application/vnd.api+json` is JSON. A JSON body model
    applies the same rule and answers `422`.

!!! note "Changed in version 0.18"
    `await request.json()` previously parsed the body whatever the
    `Content-Type` said. Send `application/json`, or read the raw bytes with
    `await request.body()` and parse them yourself.

```python title="app.py"
from veloce import Request, Veloce

app = Veloce()


@app.post("/echo")
async def echo(request: Request):
    raw = await request.body()
    return {"length": len(raw), "content_type": request.content_type}


@app.post("/echo-json")
async def echo_json(request: Request):
    payload = await request.json()
    return {"received": payload}
```

!!! note
    `request.json()` and `request.body()` are `async` to match the
    `await request.json()` idiom. Veloce buffers the body before dispatch, so on
    the in-memory and ASGI paths the await resolves immediately. The synchronous
    `request.get_json()` / `request.data` accessors exist for Flask muscle
    memory but raise `RuntimeError` if the body has not been buffered yet.

### Streaming the request body

`request.stream()` async-iterates the body in chunks, so a large upload is
processed without ever buffering it whole. Mark the route `stream=True` to opt
into true incremental delivery on the ASGI path: each chunk is yielded as the
server delivers it, in constant memory.

```python title="app.py"
from veloce import Request, Veloce

app = Veloce()


@app.post("/upload", stream=True)
async def upload(request: Request):
    total = 0
    async for chunk in request.stream():  # one chunk at a time, never buffered
        total += len(chunk)
    return {"bytes": total}
```

By default a route's body is buffered before the handler runs, so the
synchronous accessors (`request.get_json()`, `request.data`) find it ready. A
`stream=True` route trades those away: the body is delivered incrementally and
must be consumed through `request.stream()` (or `await request.body()` to drain
it). `MAX_CONTENT_LENGTH` is still enforced — an over-large streamed body is
refused mid-read. This is the same on every transport: the built-in
`Veloce.run()` server and the gunicorn `VeloceWorker` honour `stream=True`
exactly as an ASGI server does, so a handler behaves the same however it is
deployed.

!!! note "Changed in version 0.16"
    The built-in server previously streamed *every* route's body off the socket
    regardless of `stream=True`, so `request.get_json()` and `request.data`
    raised `RuntimeError` there while working under uvicorn. Routes without the
    flag are now buffered before dispatch on that path too. A handler that
    relied on incremental delivery without declaring `stream=True` must declare
    it.

!!! note "Added in version 0.9"
    The `stream=True` route option enables incremental request-body reading on
    the ASGI path.

### Form data

`await request.form()` parses both `application/x-www-form-urlencoded` and
`multipart/form-data` bodies into a `FormData` mapping.

```python title="app.py"
from veloce import Request, Veloce

app = Veloce()


@app.post("/login")
async def login(request: Request):
    form = await request.form()
    return {"user": form.get("username")}
```

!!! tip
    For typed, validated bodies prefer a Pydantic model parameter or the
    [`Form`](../reference/parameters.md#veloce.Form) marker — see
    [Parameters](parameters.md) and [File uploads](file-uploads.md). Reach for
    `request.form()` only when you want the raw mapping.

## Returning responses

A handler can return several shapes; Veloce coerces each to a `Response`.

| Return value | Becomes |
| --- | --- |
| `dict` / `list` | `JSONResponse` with status `200`. |
| `str` | `text/html` response. |
| `bytes` | `text/html` response. |
| Pydantic model | `JSONResponse` of `model_dump()`. |
| `Response` instance | Used as-is. |
| `(body, status)` tuple | `body` coerced, then status applied. |
| `(body, status, headers)` tuple | `body` coerced, status and headers applied. |
| any other tuple | JSON-encoded as an array, like a `list`. |

```python title="app.py"
from veloce import Request, Veloce

app = Veloce()


@app.get("/dict")
async def as_dict(request: Request):
    return {"json": True}


@app.get("/created")
async def created(request: Request):
    return {"created": True}, 201


@app.get("/with-headers")
async def with_headers(request: Request):
    return {"ok": True}, 201, {"X-Trace": "abc"}
```

In the `(body, status)` tuple the second element may be an `int` status, in
`(body, status, headers)` the third is a headers dict.

!!! note
    A bare `str` body still becomes `text/html`, so `("<b>hi</b>", 201)` is an
    HTML `201`.

Only those two lengths are response tuples. A tuple of any other length is a
value, and is encoded as a JSON array — `return ("x",)` is `["x"]`, not `"x"`.
That holds whether or not the route declares a `response_class`, so a mistyped
`return body, 201, headers, extra` shows up in the body rather than silently
serving a `200` with the headers dropped.

!!! note "Changed in version 0.13"
    A route with a `response_class` reads a non-`(body, status[, headers])`
    tuple the same way a route without one does. It previously took the tuple's
    first element and discarded the rest.

### Status codes

Import [`status`](../reference/index.md) for named status constants
instead of bare integers — they read better and are RFC-named.

```python title="app.py"
from veloce import Request, Veloce, status

app = Veloce()


@app.post("/items", status_code=status.HTTP_201_CREATED)
async def create_item(request: Request):
    return {"id": 1}


@app.get("/teapot")
async def teapot(request: Request):
    return {"detail": "no coffee here"}, status.HTTP_418_IM_A_TEAPOT
```

`@app.get(..., status_code=...)` sets the default status for the route; a tuple
or an explicit `Response` overrides it per return.

### Returning a Response directly

Construct a response object when you need explicit control over the body bytes,
content type, status, and headers.

The base `Response` takes `body=` (bytes) and `content_type=`.

```python title="app.py" hl_lines="6 7 8"
from veloce import HTMLResponse, JSONResponse, PlainTextResponse, Request, Response, Veloce

app = Veloce()


@app.get("/raw")
async def raw(request: Request):
    return Response(body=b"\x00\x01\x02", content_type="application/octet-stream")


@app.get("/html")
async def html(request: Request):
    return HTMLResponse("<h1>Hello from Veloce</h1>")


@app.get("/text")
async def text(request: Request):
    return PlainTextResponse("plain", status_code=200)


@app.get("/json")
async def json(request: Request):
    return JSONResponse({"ok": True}, status_code=200)
```

- `Response(body=..., content_type=...)` takes raw `bytes` — it does not encode
  a string for you. Use `HTMLResponse` / `PlainTextResponse` for `str` content.
- `JSONResponse(data, status_code=...)` and `HTMLResponse(content, status_code=...)`
  take their payload as the first positional argument.
- Other classes: [`RedirectResponse`](../reference/responses.md#veloce.RedirectResponse),
  [`FileResponse`](../reference/responses.md#veloce.FileResponse),
  [`ORJSONResponse`](../reference/responses.md#veloce.ORJSONResponse),
  [`UJSONResponse`](../reference/responses.md#veloce.UJSONResponse).

!!! warning "`Response` body is bytes, not text"
    `Response(body=...)` expects `bytes`. Passing a `str` raises when the
    response is encoded — pass `body=b"hello"`, or use
    `HTMLResponse`/`PlainTextResponse`, which encode UTF-8 for you.
    `content_type=` controls only the header, never the encoding.

### Response cookies and headers

Mutate headers on the `Response` object, and set cookies with `set_cookie()` so
multiple `Set-Cookie` values append correctly.

```python title="app.py"
from veloce import JSONResponse, Request, Veloce

app = Veloce()


@app.get("/set")
async def set_things(request: Request):
    resp = JSONResponse({"ok": True})
    resp.headers["X-App"] = "veloce"
    resp.set_cookie("session_id", "abc123", httponly=True, samesite="Lax", max_age=3600)
    return resp
```

```python
from veloce import JSONResponse, Request, TestClient, Veloce

app = Veloce()


@app.get("/set")
async def set_things(request: Request):
    resp = JSONResponse({"ok": True})
    resp.headers["X-App"] = "veloce"
    resp.set_cookie("session_id", "abc123", httponly=True)
    return resp


client = TestClient(app)

resp = client.get("/set")
assert resp.status_code == 200
assert resp.headers["X-App"] == "veloce"
assert resp.cookies["session_id"] == "abc123"
```

- `set_cookie()` defaults to `samesite="Lax"` and `path="/"`. Pass
  `secure=True` and `samesite="None"` together for cross-site cookies.
- `delete_cookie(key, ...)` expires a cookie — pass the same `path`/`domain`/
  `secure`/`samesite` flags it was set with, or the browser keeps both.

!!! warning "Set cookies through `set_cookie()`"
    Do not assign `response.headers["Set-Cookie"] = ...` directly — that
    overwrites any cookie another middleware already set. `set_cookie()` appends
    correctly so every cookie survives.

### Shaping output with `response_model`

Pass `response_model=` on the route to validate and filter the handler's return
value through a model. Veloce dumps the result through it, so undeclared fields
drop and the OpenAPI response schema documents the public shape.

```python title="app.py"
from pydantic import BaseModel

from veloce import Request, Veloce

app = Veloce()


class UserOut(BaseModel):
    id: int
    name: str


@app.get("/users/{user_id}", response_model=UserOut)
async def get_user(request: Request, user_id: int):
    # The extra "password" field is dropped by response_model.
    return {"id": user_id, "name": "Ada", "password": "secret"}
```

```python
from pydantic import BaseModel

from veloce import Request, TestClient, Veloce

app = Veloce()


class UserOut(BaseModel):
    id: int
    name: str


@app.get("/users/{user_id}", response_model=UserOut)
async def get_user(request: Request, user_id: int):
    return {"id": user_id, "name": "Ada", "password": "secret"}


client = TestClient(app)

resp = client.get("/users/1")
assert resp.status_code == 200
assert resp.json() == {"id": 1, "name": "Ada"}
```

`response_model` also accepts `list[Model]` — each element is dumped.

It also accepts these filter flags:

| Filter flag |
| --- |
| `response_model_exclude_unset` |
| `response_model_exclude_none` |
| `response_model_exclude_defaults` |
| `response_model_by_alias` |
| `response_model_include` |
| `response_model_exclude` |

Each flag maps to the `model_dump` option of the same name. `include` and
`exclude` accept any collection of field names and are normalised to a set;
where a field is named by both, `exclude` wins.

`response_model_include` and `response_model_exclude` also shape the **OpenAPI
document**: the route's response schema names exactly the fields it sends, under
a derived component (`Item_name_price`). Routes filtering one model the same way
share that component; an unfiltered route keeps the model's own. Without this the
document would advertise fields the route omits — and mark them `required` when
they have no default — so a generated client would expect data it never receives.

!!! note "Changed in version 0.13"
    A filtered response documents its filtered shape. It previously documented
    the whole model whatever the filter said.

```python
@app.get("/users/{user_id}", response_model=UserOut, response_model_exclude_none=True)
async def read_user(user_id: int) -> UserOut:
    return UserOut(id=user_id, name="Ada")     # a null `nickname` is omitted
```

The flags are fixed when the route is registered, so the options are resolved
once there rather than rebuilt on every response. That is invisible from the
outside — the same fields are emitted either way — but it means the flags are
constructor arguments: assigning to `route.response_model_exclude_none` after
registration does not take effect. Pass the flag to the route decorator.

!!! note "`response_model` is inferred from the return annotation"
    A `-> UserOut` return annotation supplies the response model, as in FastAPI:
    the value is reshaped and filtered through it, and the schema is documented.
    Pass `response_model=` when you want a different model from the one you
    annotate; it wins over the annotation.

    Inference recognises a model, `list[Model]`, and a union of models. Any other
    annotation declares no contract and the value is serialised as-is.

!!! warning "A `Union` response model documents but does not filter"
    Only a Pydantic model or a sequence of one is reshaped at runtime. A
    `response_model=A | B` (or `Union[A, B]`) falls through unfiltered — which
    member to reshape through is ambiguous, so the return value is serialised
    as-is. It **is** documented, as a `oneOf` of the alternatives. Declare a
    single concrete output model per route when you need filtering.

## Streaming responses

[`StreamingResponse`](../reference/responses.md#veloce.StreamingResponse) sends chunks as
they are produced, without buffering the whole body. It accepts an async
iterator **or** a plain sync generator. `str` chunks are encoded to UTF-8
automatically.

```python title="app.py"
from veloce import Request, StreamingResponse, Veloce

app = Veloce()


@app.get("/stream")
async def stream(request: Request):
    def chunks():
        for i in range(1000):
            yield f"line {i}\n"

    return StreamingResponse(chunks(), content_type="text/plain")
```

The default `content_type` is `application/octet-stream`; set it to match your
payload.

### Streaming JSON Lines (NDJSON)

For newline-delimited JSON, encode each record on its own line and stream them
with the `application/x-ndjson` content type so clients can parse the response
record-by-record.

```python title="app.py"
import orjson

from veloce import Request, StreamingResponse, Veloce

app = Veloce()


@app.get("/events.ndjson")
async def events(request: Request):
    async def rows():
        for i in range(3):
            yield orjson.dumps({"seq": i}) + b"\n"

    return StreamingResponse(rows(), content_type="application/x-ndjson")
```

```python
import orjson

from veloce import Request, StreamingResponse, TestClient, Veloce

app = Veloce()


@app.get("/events.ndjson")
async def events(request: Request):
    async def rows():
        for i in range(3):
            yield orjson.dumps({"seq": i}) + b"\n"

    return StreamingResponse(rows(), content_type="application/x-ndjson")


client = TestClient(app)

resp = client.get("/events.ndjson")
assert resp.status_code == 200
lines = [orjson.loads(line) for line in resp.body.splitlines()]
assert lines == [{"seq": 0}, {"seq": 1}, {"seq": 2}]
```

!!! note
    A streaming response has no `Content-Length`; it is sent with chunked
    transfer encoding. For Server-Sent Events use
    [`EventSourceResponse`](../reference/websockets.md#veloce.EventSourceResponse) instead —
    see [Server-sent events](sse.md).

## A complete program

This runs on the no-uvicorn path with `app.run()`.

```python title="app.py"
from veloce import Request, Veloce, status

app = Veloce()


@app.post("/items", status_code=status.HTTP_201_CREATED)
async def create_item(request: Request):
    payload = await request.json()
    return {"stored": payload}, status.HTTP_201_CREATED


if __name__ == "__main__":
    app.run(port=8000)
```

## Next steps

- [Parameters](parameters.md) — validate query, path, header, and form inputs.
- [File uploads](file-uploads.md) — `UploadFile`, multipart forms, and size limits.
- [Server-sent events](sse.md) — push streams with `EventSourceResponse`.
- [Caching](caching.md) — ETags, conditional requests, and `Cache-Control`.
- Full signatures are in the [API reference](../reference/index.md).
