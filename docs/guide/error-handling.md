---
description: Raise and handle errors in Veloce — HTTPException, abort(), register_error_handler, custom error pages, and validation errors.
tags: [errors, exceptions, abort, http-status]
---

# Error Handling

Veloce turns errors into HTTP responses through a small set of pieces: the
[`HTTPException`](../reference.md#veloce.HTTPException) class, the
[`abort()`](../reference.md#veloce.abort) shortcut for raising one, and
error handlers that convert a raised exception into the response the client
sees.

```python
from veloce import Veloce, abort

app = Veloce()


@app.get("/users/{user_id}")
async def get_user(user_id: int):
    user = {"1": "Ada"}.get(str(user_id))
    if user is None:
        abort(404)
    return {"user": user}
```

Requesting `/users/2` returns a `404` with a JSON body
`{"detail": "Not Found"}`. The rest of this page explains how that
happens and how to customise it.

## Raising errors with abort()

`abort(status_code, detail="", headers=None)` raises an `HTTPException`
with the given status. When you omit `detail`, Veloce fills in the standard
reason phrase for the code (`"Not Found"` for 404, `"Forbidden"` for 403).

```python
from veloce import Veloce, abort

app = Veloce()


@app.get("/secret")
async def secret(token: str = ""):
    if token != "open-sesame":
        abort(403, "You shall not pass")
    return {"ok": True}
```

For known status codes `abort` raises a specifically-typed subclass — `403`
raises `Forbidden`, `404` raises `NotFound`, and so on. This matters for
handler registration: a handler registered against `Forbidden` will match.
Unknown codes fall back to the base `HTTPException`.

Pass `headers` to attach response headers to the error — for example a
`Retry-After` on a rate-limited response:

```python
from veloce import Veloce, abort

app = Veloce()


@app.get("/limited")
async def limited():
    abort(429, "Slow down", headers={"Retry-After": "30"})
```

## Raising HTTPException directly

`abort()` is a shorthand; you can raise [`HTTPException`](../reference.md#veloce.HTTPException)
yourself when you want full control. The constructor is
`HTTPException(status_code=None, detail="", headers=None)`.

```python
from veloce import HTTPException, Veloce

app = Veloce()


@app.get("/teapot")
async def teapot():
    raise HTTPException(418, "I refuse to brew coffee")
```

Veloce also ships named subclasses for each standard status code under
`veloce.exceptions`. Each carries a fixed `code` and `description`, so you
can raise one without repeating the number. These names are not part of the
top-level `veloce` namespace — import them from the submodule:

```python
from veloce import Veloce
from veloce.exceptions import NotFound

app = Veloce()


@app.get("/items/{name}")
async def get_item(name: str):
    if name not in {"pen", "cup"}:
        raise NotFound(f"no item named {name!r}")
    return {"item": name}
```

The first positional argument to a subclass is the detail message, so
`NotFound("no item")` reads naturally while still defaulting the status
code to the subclass's `code`.

!!! note
    Subclasses available under `veloce.exceptions` include `BadRequest`
    (400), `Unauthorized` (401), `Forbidden` (403), `NotFound` (404),
    `MethodNotAllowed` (405), `Conflict` (409), `Gone` (410),
    `RequestEntityTooLarge` (413), `UnsupportedMediaType` (415),
    `UnprocessableEntity` (422), `TooManyRequests` (429),
    `InternalServerError` (500), `BadGateway` (502), and
    `ServiceUnavailable` (503), among others. The
    [API reference](../reference.md) documents `HTTPException` itself; the
    named subclasses live in `veloce.exceptions`.

## The default error response

Without any custom handler, an `HTTPException` renders as JSON. The body is
`{"detail": <detail or description>}`, the status code is `exc.status_code`,
and any `exc.headers` are applied. This is what
[`http_exception_handler`](../reference.md#veloce.http_exception_handler)
produces, and it is the framework default for every error raised through
`abort()` or `HTTPException`.

## Registering custom error handlers

Register a handler to replace the default response for a given exception
type or status code. The decorator form is `@app.exception_handler(...)`;
the imperative form is `app.register_error_handler(...)`. A handler
receives the request and the exception, and returns any value Veloce can
coerce to a response (a dict, a tuple, or a response object).

```python
from veloce import JSONResponse, Request, Veloce
from veloce.exceptions import NotFound

app = Veloce()


@app.exception_handler(NotFound)
async def handle_not_found(request: Request, exc: NotFound):
    return JSONResponse(
        {"error": "not_found", "path": request.path},
        status_code=404,
    )


@app.get("/missing")
async def missing():
    raise NotFound()
```

A handler registered against a base class catches every subclass, because
Veloce walks the exception's method-resolution order to find a match. A
handler on `HTTPException` therefore catches every `NotFound`,
`Forbidden`, and so on:

```python
from veloce import HTTPException, JSONResponse, Request, Veloce

app = Veloce()


@app.exception_handler(HTTPException)
async def handle_http_error(request: Request, exc: HTTPException):
    return JSONResponse(
        {"status": exc.status_code, "detail": exc.detail},
        status_code=exc.status_code,
    )
```

### Registering by status code

Pass an integer instead of a class to handle a specific status code. A
status-code handler takes precedence over a class handler for the same
code.

```python
from veloce import HTMLResponse, Request, Veloce

app = Veloce()


@app.exception_handler(404)
async def not_found_page(request: Request, exc):
    return HTMLResponse("<h1>Page not found</h1>", status_code=404)
```

### The imperative form

`register_error_handler` and `add_exception_handler` register the same
handlers without a decorator — useful when wiring handlers in a factory
function. Both accept either an exception class or an integer status code.

```python
from veloce import JSONResponse, Request, Veloce
from veloce.exceptions import Forbidden


async def on_forbidden(request: Request, exc: Forbidden):
    return JSONResponse({"error": "forbidden"}, status_code=403)


def create_app() -> Veloce:
    app = Veloce()
    app.register_error_handler(Forbidden, on_forbidden)
    return app
```

!!! note
    `app.exception_handler` is also available under the alias
    `app.errorhandler` (one word). The two are identical.

## Custom error pages

An error handler can return any response shape, so HTML error pages are
just a handler that returns an [`HTMLResponse`](../reference.md#veloce.HTMLResponse).
Combine a status-code handler with a template for a polished 404 page:

```python
from veloce import HTMLResponse, Request, Veloce

app = Veloce()

_PAGE = """
<!doctype html>
<title>Not found</title>
<h1>404 — {path} does not exist</h1>
<p><a href="/">Return home</a></p>
"""


@app.exception_handler(404)
async def not_found(request: Request, exc):
    return HTMLResponse(_PAGE.format(path=request.path), status_code=404)
```

For a content-negotiated handler, inspect the request's `Accept` header and
return HTML or JSON accordingly:

```python
from veloce import HTMLResponse, JSONResponse, Request, Veloce
from veloce.exceptions import NotFound

app = Veloce()


@app.exception_handler(NotFound)
async def not_found(request: Request, exc: NotFound):
    accept = request.headers.get("accept", "")
    if "text/html" in accept:
        return HTMLResponse("<h1>Not found</h1>", status_code=404)
    return JSONResponse({"detail": exc.detail}, status_code=404)
```

## Validation errors

When a typed handler parameter fails to parse — a bad path converter, a
missing required query value, an invalid body — the dependency resolver
raises [`RequestValidationError`](../reference.md#veloce.RequestValidationError),
a `422` carrying a structured `errors` list. The default response renders
`{"detail": [...per-field errors...]}` via
[`request_validation_exception_handler`](../reference.md#veloce.request_validation_exception_handler).

Register your own handler to reshape it:

```python
from veloce import JSONResponse, Request, RequestValidationError, Veloce

app = Veloce()


@app.exception_handler(RequestValidationError)
async def on_invalid(request: Request, exc: RequestValidationError):
    return JSONResponse({"errors": exc.errors}, status_code=422)
```

`RequestValidationError` subclasses `ValidationError`, which in turn
subclasses `UnprocessableEntity` (a `422` `HTTPException`). An
`except ValidationError` handler, or one registered against
`HTTPException`, catches it too via the MRO walk.

## Propagating exceptions during tests

By default Veloce catches unhandled exceptions and returns a `500`. While
testing it is usually better to let the original exception surface with its
traceback. Set `PROPAGATE_EXCEPTIONS` in the config, or enable both `DEBUG`
and `TESTING` (which implies propagation):

```python
from veloce import Veloce

app = Veloce()
app.config["PROPAGATE_EXCEPTIONS"] = True
```

With propagation on, an exception raised in a handler re-raises out of
dispatch instead of being converted to a `500`. See
[Configuration](configuration.md#built-in-defaults) for how
`PROPAGATE_EXCEPTIONS`, `DEBUG`, and `TESTING` interact.

!!! tip
    For the semantics of HTTP status codes, the
    [MDN HTTP status reference](https://developer.mozilla.org/en-US/docs/Web/HTTP/Status)
    and [RFC 9110](https://www.rfc-editor.org/rfc/rfc9110.html) are the
    authoritative sources. Veloce's named exceptions map one-to-one onto
    those codes.

## Next steps

- [Configuration](configuration.md) — tune `PROPAGATE_EXCEPTIONS` and other
  keys that affect how failures are reported.
- [Testing](testing.md) — assert on error responses with the in-memory
  test client.
- [Requests & Responses](requests-responses.md) — the response shapes an
  error handler can return.
- The [API reference](../reference.md) lists `HTTPException`, `abort`, and
  the registration methods with full signatures.
