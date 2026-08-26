---
description: Raise and handle errors in Veloce — HTTPException, abort(), register_error_handler, custom error pages, and validation errors.
tags: [errors, exceptions, abort, http-status]
---

# Error Handling

Veloce turns errors into HTTP responses through a small set of pieces: the
[`HTTPException`](../reference/exceptions.md#veloce.HTTPException) class, the
[`abort()`](../reference/helpers.md#veloce.abort) shortcut for raising one, and
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

`abort()` is a shorthand; you can raise [`HTTPException`](../reference/exceptions.md#veloce.HTTPException)
yourself when you want full control. The constructor is
`HTTPException(status_code=None, detail="", headers=None)`.

```python
from veloce import HTTPException, Veloce

app = Veloce()


@app.get("/teapot")
async def teapot():
    raise HTTPException(418, "I refuse to brew coffee")
```

Veloce also ships a named subclass for each standard status code. Each carries
a fixed `code` and `description`, so you can raise one without repeating the
number:

```python
from veloce import NotFound, Veloce

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

Every name in the table below is importable from the top level:

| Status code | Subclass                       |
| ----------- | ------------------------------ |
| 400         | `BadRequest`                   |
| 401         | `Unauthorized`                 |
| 402         | `PaymentRequired`              |
| 403         | `Forbidden`                    |
| 404         | `NotFound`                     |
| 405         | `MethodNotAllowed`             |
| 406         | `NotAcceptable`                |
| 407         | `ProxyAuthenticationRequired`  |
| 408         | `RequestTimeout`               |
| 409         | `Conflict`                     |
| 410         | `Gone`                         |
| 411         | `LengthRequired`               |
| 412         | `PreconditionFailed`           |
| 413         | `RequestEntityTooLarge`        |
| 414         | `RequestURITooLong`            |
| 415         | `UnsupportedMediaType`         |
| 416         | `RangeNotSatisfiable`          |
| 417         | `ExpectationFailed`            |
| 418         | `ImATeapot`                    |
| 422         | `UnprocessableEntity`          |
| 429         | `TooManyRequests`              |
| 500         | `InternalServerError`          |
| 501         | `ServerNotImplemented`         |
| 502         | `BadGateway`                   |
| 503         | `ServiceUnavailable`           |
| 504         | `GatewayTimeout`               |

!!! note "Why `ServerNotImplemented`"
    `NotImplemented` is a Python builtin, so the 501 class cannot use the
    obvious name. `ServerNotImplemented` is the exported spelling; it reads
    alongside `InternalServerError` in the 5xx block.

!!! note "Added in version 0.12"
    These names became top-level imports in 0.12. `from veloce.exceptions
    import NotFound` still works and returns the same class.

## Catching anything Veloce raised

Every exception the framework defines — HTTP errors, validation failures,
WebSocket closes, routing and setup errors, JWT and signature failures —
inherits [`VeloceError`](../reference/exceptions.md#veloce.VeloceError). One
`except` clause therefore answers "did this come from Veloce?":

```python
from veloce import BuildError, NotFound, VeloceError

for error in (NotFound("gone"), BuildError("profile", {})):
    try:
        raise error
    except VeloceError as exc:
        print(type(exc).__name__, "came from Veloce")
```

The root was mixed in beside the bases those classes already had, so nothing
that matched before stops matching. `DuplicateRouteError` is still a
`ValueError` and `FilesKeyError` is still a `KeyError` — but they are now
*also* `VeloceError`, which is what lets you tell a route-registration bug
apart from an ordinary bad value:

```python
from veloce import DuplicateRouteError, VeloceError

exc = DuplicateRouteError("/items", "GET", "list_items", "list_all")
assert isinstance(exc, ValueError)     # unchanged
assert isinstance(exc, VeloceError)    # new
```

`VeloceError` also makes a catch-all handler expressible:

```python
from veloce import JSONResponse, Request, Veloce, VeloceError

app = Veloce()


@app.exception_handler(VeloceError)
async def on_framework_error(request: Request, exc: VeloceError):
    return JSONResponse(
        {"error": type(exc).__name__, "detail": str(exc)},
        status_code=getattr(exc, "status_code", 500),
    )
```

`VeloceError` is listed first among the bases of the classes that also carry a
stdlib type, so a handler registered against it wins the method-resolution
walk over a broader handler registered against `ValueError` or `KeyError`.

!!! note "Added in version 0.12"
    `VeloceError` is new in 0.12. Existing `except` clauses are unaffected —
    the root was added to the base list, never substituted for one.

## The default error response

Without any custom handler, an `HTTPException` renders as JSON. The body is
`{"detail": <detail or description>}`, the status code is `exc.status_code`,
and any `exc.headers` are applied. This is what
[`http_exception_handler`](../reference/exceptions.md#veloce.http_exception_handler)
produces, and it is the framework default for every error raised through
`abort()` or `HTTPException`.

## Registering custom error handlers

Register a handler to replace the default response for a given exception
type or status code. The decorator form is `@app.exception_handler(...)`;
the imperative form is `app.register_error_handler(...)`. A handler
receives the request and the exception, and returns any value Veloce can
coerce to a response (a dict, a tuple, or a response object).

```python
from veloce import JSONResponse, NotFound, Request, Veloce

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
from veloce import Forbidden, JSONResponse, Request, Veloce


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
just a handler that returns an [`HTMLResponse`](../reference/responses.md#veloce.HTMLResponse).
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
from veloce import HTMLResponse, JSONResponse, NotFound, Request, Veloce

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
raises [`RequestValidationError`](../reference/exceptions.md#veloce.RequestValidationError),
a `422` carrying a structured `errors` list. You do **not** need to register a
handler to get a useful response — the default body is a structured error list,
one entry per failed field with `loc` (where it failed), `msg`, and `type`:

```json
{
  "detail": [
    {"loc": ["query", "limit"], "msg": "Input should be a valid integer", "type": "int_parsing"}
  ],
  "status_code": 422
}
```

`loc` is the path to the failing value. Its parts are strings for field and key
names and **integers for array indices**, so an error inside the second element
of a list reports `["body", "lines", 1, "qty"]`. That union is what the
generated `ValidationError` schema declares, and it keeps an index `1` distinct
from a dict key `"1"`.

The exported [`request_validation_exception_handler`](../reference/exceptions.md#veloce.request_validation_exception_handler)
renders the same per-field `detail` list as `{"detail": [...]}` (without the
top-level `status_code` field the default dispatch adds). Register it explicitly,
or reshape the response with your own handler:

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

The generated OpenAPI document advertises this response automatically.

Any operation whose request is validated — one carrying a path, query, header, or
cookie parameter, a JSON body, or a form field — gains a `422` entry that
references a shared `HTTPValidationError` component schema (the
`{"detail": [{"loc", "msg", "type"}, ...]}` shape shown above).

Operations with no validatable parameter never advertise a `422`, and an explicit
`422` declared through `responses=` or `openapi_extra` is kept as-is.

## Unhandled exceptions are logged

An exception no handler catches becomes a generic `500` — and is recorded on the
app's logger at `ERROR` level, with the traceback and the request that failed:

```
Exception on /orders/42 [POST]
Traceback (most recent call last):
  ...
RuntimeError: connection pool exhausted
```

This needs no logging setup: Python's handler of last resort puts an unconfigured
`ERROR` record on stderr. It is the app's own logger — `logging.getLogger(app.import_name)`
— so it is configured, routed or silenced like any other:

```python
import logging

logging.getLogger("myapp").setLevel(logging.CRITICAL)   # silence it
logging.getLogger("myapp").addHandler(my_handler)       # or route it
```

A **handled** exception is not logged: registering `@app.exception_handler(...)`
for it means the application dealt with it. Neither is an `HTTPException` such as
`abort(404)`, which is an outcome rather than a failure. A propagated exception
(below) is not logged either — it carries its own traceback to the caller.

To ship these somewhere else, subscribe to the `got_request_exception` signal;
it fires for the same exceptions and is how an error tracker hooks in.

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
- The [API reference](../reference/index.md) lists `HTTPException`, `abort`, and
  the registration methods with full signatures.
