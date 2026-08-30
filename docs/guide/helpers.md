---
description: Use Veloce's Flask-style request-scoped helpers — g, current_app, request, session, flash, abort, jsonify, redirect, make_response, and send_file.
tags: [helpers, flask, context, sessions]
---

# Flask-style helpers

Veloce ships a set of Flask-style helpers for the common cases:
request-scoped proxies ([`g`](#g), [`current_app`](#current_app),
[`request`](#request), [`session`](#session)), response shortcuts
([`jsonify`](#jsonify), [`make_response`](#make_response),
[`redirect`](#redirect), [`send_file`](#send_file)), and control-flow
helpers ([`abort`](#abort), [`flash`](#flash)). All import from the
top-level `veloce` package.

## A first example

```python
from veloce import Veloce, abort, g, jsonify

app = Veloce()


@app.get("/users/{user_id}")
async def get_user(user_id: int):
    if user_id < 1:
        abort(404)
    g.user_id = user_id            # stash on the request-scoped namespace
    return jsonify(id=g.user_id, name="alice")
```

`abort(404)` raises the typed `NotFound` exception, `g` carries
per-request state, and `jsonify` builds a JSON response. The rest of this
page covers each helper.

## Request-scoped proxies

Four singletons resolve to per-request state on every access. They are
backed by `contextvars`, so they are correct under async concurrency —
each in-flight request sees its own value with no thread-local hacks.

### g

`g` is a request-scoped namespace for stashing data that several functions
in one request need to share — a database handle, the authenticated user,
a request-scoped cache. It is cleared between requests.

```python
from veloce import Request, Veloce, g

app = Veloce()


@app.get("/profile")
async def profile(request: Request):
    g.request_id = request.headers.get("x-request-id", "none")
    return {"request_id": g.request_id}
```

Reading an attribute that was never set raises `AttributeError`. Use the
dict-style accessors to avoid that:

```python
from veloce import Veloce, g

app = Veloce()


@app.get("/safe")
async def safe():
    value = g.get("missing", "default")     # no AttributeError
    g.setdefault("counter", 0)
    popped = g.pop("counter", None)
    return {"value": value, "popped": popped, "has_value": "value" in g}
```

`g` supports `g.get(name, default=None)`, `g.pop(name, *args)`,
`g.setdefault(name, default=None)`, and `name in g`.

### current_app

`current_app` proxies to the app handling the current request. Use it to
reach `app.config`, `app.state`, or `app.extensions` from code that does
not otherwise have the app:

```python
from veloce import Veloce, current_app

app = Veloce()
app.config["FEATURE_FLAG"] = True


@app.get("/flag")
async def flag():
    return {"enabled": current_app.config.get("FEATURE_FLAG")}
```

Accessing `current_app` outside an active request raises `RuntimeError`.

### request

`request` proxies to the [`Request`](../reference/requests.md#veloce.Request)
being handled. The usual way to reach the request is to annotate a
handler parameter as `Request` (see
[Requests & Responses](requests-responses.md)); the `request` proxy is
for helper code spawned during the request that cannot easily receive it
as an argument.

```python
from veloce import Veloce, request

app = Veloce()


def client_ua() -> str:
    return request.headers.get("user-agent", "unknown")


@app.get("/ua")
async def ua():
    return {"user_agent": client_ua()}
```

### session

`session` proxies to the current request's session. It requires
`SessionMiddleware` (or `ServerSessionMiddleware`) — see the
[Sessions](sessions.md) guide:

```python
from veloce import SessionMiddleware, Veloce, session

app = Veloce()
app.add_middleware(SessionMiddleware, secret_key="change-me-in-production")


@app.get("/visits")
async def visits():
    session["visits"] = session.get("visits", 0) + 1
    return {"visits": session["visits"]}
```

The proxy supports item access (`session["k"]`), `in`, iteration,
`len()`, and the dict methods of the underlying
[`Session`](../reference/sessions.md#veloce.Session).

## abort

[`abort`](../reference/helpers.md#veloce.abort) raises an
[`HTTPException`](../reference/exceptions.md#veloce.HTTPException) by status code. For
known codes it raises the matching typed subclass (`NotFound` for 404,
`Forbidden` for 403) so error handlers registered against that subclass match:

```python
from veloce import Veloce, abort

app = Veloce()


@app.get("/restricted")
async def restricted():
    abort(403, "You may not view this")
```

The signature is `abort(status_code, detail="", headers=None)`. When
`detail` is omitted, the standard reason phrase for the status code is
used. See [Error handling](error-handling.md) for custom handlers.

## jsonify

[`jsonify`](../reference/helpers.md#veloce.jsonify) builds a
[`JSONResponse`](../reference/responses.md#veloce.JSONResponse). Pass keyword arguments
to build an object, or a single positional value (dict, list, or any
JSON-serialisable value):

```python
from veloce import Veloce, jsonify

app = Veloce()


@app.get("/kw")
async def kw():
    return jsonify(name="alice", age=30)      # -> {"name": "alice", "age": 30}


@app.get("/positional")
async def positional():
    return jsonify([1, 2, 3])                 # -> [1, 2, 3]
```

Passing both positional and keyword arguments raises `TypeError`. When
called inside a request, `jsonify` honours two `app.config` flags:
`JSON_SORT_KEYS` (default `False`) sorts dict keys, and
`JSONIFY_PRETTYPRINT_REGULAR` (default `False`) indents the output.

## redirect

[`redirect`](../reference/helpers.md#veloce.redirect) builds a redirect response. The
default status code is `302`:

```python
from veloce import Veloce, redirect

app = Veloce()


@app.get("/old")
async def old():
    return redirect("/new")


@app.get("/permanent")
async def permanent():
    return redirect("/new", code=301)
```

The signature is `redirect(location, code=302, headers=None)`. See [RFC
9110 §15.4](https://www.rfc-editor.org/rfc/rfc9110#section-15.4) for the
redirect status codes — `303` forces a GET, `307` and `308` preserve the
method.

## make_response

[`make_response`](../reference/helpers.md#veloce.make_response) coerces a body, status
code, and headers into a [`Response`](../reference/responses.md#veloce.Response), picking
the response type from the body. A `str` or `bytes` becomes an HTML response, and a
`dict` or `list` a JSON response:

```python
from veloce import Veloce, make_response

app = Veloce()


@app.get("/custom")
async def custom():
    resp = make_response("<h1>Hello</h1>", 200)
    resp.headers["X-Custom"] = "1"
    return resp


@app.get("/created")
async def created():
    return make_response({"id": 1}, 201)
```

The signature is `make_response(body=b"", status_code=200, headers=None,
content_type=None)`. A Pydantic model (anything with `model_dump`) is
serialised to JSON.

A tuple body is unpacked as `(body, status)`, `(body, headers)` or
`(body, status, headers)` - the same shapes a handler may return, read from the
same table as [`Veloce.make_response`](../reference/app.md#veloce.Veloce.make_response)
and the dispatcher. A tuple of any other length is not a response tuple and is
serialised as data.

!!! note "Changed in version 0.18"

    A one- or four-element tuple is answered as data. A four-element tuple
    previously dropped its status and headers in silence.

## send_file

[`send_file`](../reference/helpers.md#veloce.send_file) serves a file from a filesystem
path and returns a [`FileResponse`](../reference/responses.md#veloce.FileResponse) with
conditional-GET headers (`Last-Modified`, `ETag`) already set:

```python
from veloce import Veloce, send_file

app = Veloce()


@app.get("/report")
async def report():
    return send_file("reports/q1.pdf")


@app.get("/download")
async def download():
    return send_file(
        "reports/q1.pdf",
        as_attachment=True,
        download_name="quarter-1.pdf",
        max_age=3600,
    )
```

The signature is `send_file(path_or_file, mimetype=None,
as_attachment=False, download_name=None, last_modified=None, etag=True,
max_age=None)`. Set `as_attachment=True` to add a
`Content-Disposition: attachment` header, `etag=False` to suppress the
auto-generated ETag, and `max_age=` to add a `Cache-Control` header.

!!! warning "Do not pass user input straight to send_file"
    `send_file` does not sanitise the path against directory traversal.
    To serve a file whose name comes from the request, use
    `send_from_directory` (or `send_from_directory_async`), which is
    traversal-safe and returns 403 on any escape attempt. See the
    [OWASP path-traversal
    reference](https://owasp.org/www-community/attacks/Path_Traversal).

!!! warning "send_file blocks inside async handlers"
    `send_file` calls the blocking [`FileResponse`](../reference/responses.md#veloce.FileResponse)
    constructor, which reads file metadata synchronously and emits a
    `VeloceDeprecationWarning` when invoked from an `async def` handler on a running
    event loop. Inside async routes use the async-safe alternatives instead:
    `async_send_file` (the async counterpart of `send_file`, same arguments),
    `await FileResponse.from_path(...)` for a lower-level single file, or
    `await send_from_directory_async(...)` for traversal-safe directory serving.

`async_send_file` mirrors `send_file` argument-for-argument but reads the file
in an executor so it never blocks the event loop:

```python
from veloce import Veloce, async_send_file

app = Veloce()


@app.get("/download")
async def download():
    return await async_send_file(
        "reports/q1.pdf",
        as_attachment=True,
        download_name="quarter-1.pdf",
        max_age=3600,
    )
```

## flash

[`flash`](../reference/helpers.md#veloce.flash) queues a one-time message for the next
request — the classic POST/redirect/GET pattern. Because the message has
to survive a redirect, the queue is carried in the session, so `flash`
requires `SessionMiddleware` (or `ServerSessionMiddleware`):

```python
from veloce import (
    SessionMiddleware,
    Veloce,
    flash,
    get_flashed_messages,
    redirect,
)

app = Veloce()
app.add_middleware(SessionMiddleware, secret_key="change-me-in-production")


@app.post("/items")
async def create_item():
    flash("Item created successfully")
    return redirect("/items")


@app.get("/items")
async def list_items():
    messages = get_flashed_messages()
    return {"flashes": messages}
```

The signature is `flash(message, category="message")`. Read the queue
with `get_flashed_messages(with_categories=False, category_filter=None)`,
which drains it — each message is shown once. Pass
`with_categories=True` to receive `(category, message)` pairs:

```python
from veloce import SessionMiddleware, Veloce, flash, get_flashed_messages

app = Veloce()
app.add_middleware(SessionMiddleware, secret_key="change-me-in-production")


@app.get("/notify")
async def notify():
    flash("Saved", "success")
    flash("Check your input", "error")
    return {"messages": get_flashed_messages(with_categories=True)}
```

Calling `flash` without session middleware installed raises
`RuntimeError`. Calling `get_flashed_messages` outside a request returns
an empty list, so it is safe to call during a template render.

## Next steps

- [Sessions](sessions.md) — the session backends `session` and `flash`
  build on.
- [Requests & Responses](requests-responses.md) — the `Request` object
  and the response classes `jsonify`, `make_response`, and `redirect`
  return.
- [Templates](templates.md) — `get_flashed_messages` is designed to be
  called from a template.
- The [API reference](../reference/index.md) lists every helper with full
  signatures.
