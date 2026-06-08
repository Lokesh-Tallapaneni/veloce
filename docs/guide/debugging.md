---
description: Debug a Veloce app — the debug=True HTML traceback page, running in code with app.run, and PROPAGATE_EXCEPTIONS to surface exceptions in tests.
tags: [debugging, debug, traceback, testing]
---

# Debugging

Veloce has two development conveniences for unhandled errors: a rich HTML
traceback page served when [`debug`](../reference.md#veloce.Veloce.debug) is on, and
a `PROPAGATE_EXCEPTIONS` switch that re-raises exceptions out of dispatch so a
test sees the real traceback instead of a `500`. Both ship with Veloce — there
is nothing to install.

## Enabling debug mode

Pass `debug=True` to the constructor, or set `app.debug` (it is bound to
`app.config["DEBUG"]`).

```python title="app.py"
from veloce import Veloce

app = Veloce(debug=True)


@app.get("/boom")
async def boom():
    raise ValueError("something went wrong")


if __name__ == "__main__":
    app.run(port=8000)
```

Visit `http://127.0.0.1:8000/boom` in a browser and Veloce renders the
exception type, message, and every traceback frame with a window of source
around the failing line. Chained exceptions (`raise ... from`), `__notes__`
(PEP 678), and exception groups (PEP 654) are all shown.

!!! note
    `app.run()` is the built-in development server. It is for local use only;
    run under a hardened ASGI server (`uvicorn module:app`) in production. See
    [Deployment](deployment.md).

### HTML for browsers, plain text for tools

The response format follows the request's `Accept` header. A browser sending
`Accept: text/html` gets the styled HTML page; `curl`, CLI tools, and
programmatic clients (no `Accept`, `*/*`, or a `text/plain` preference) get the
plain-text traceback instead.

```bash
curl http://127.0.0.1:8000/boom        # plain-text traceback
```

!!! warning "Never enable debug in production"
    The traceback page exposes source code, file paths, and local framework
    internals. Keep `debug=False` for anything reachable beyond localhost.
    Binding the dev server to a non-local host with `debug=True` logs a warning
    for exactly this reason.

## The debug page is read-only

Veloce's traceback page is a navigable HTML view, not an interactive
in-browser console. It contains no form, no input, no JavaScript that posts
back, and no endpoint that evaluates user-supplied code. Live frame evaluation
is intentionally unimplemented because it turns a debug page into a
remote-code-execution surface.

!!! note
    Every value interpolated into the page — file paths, source lines, the
    exception message — is HTML-escaped, so exception content cannot inject
    markup.

## Surfacing exceptions in tests

By default an unhandled exception becomes a `500` response, which is correct
for production but hides the real error from a test. Set `PROPAGATE_EXCEPTIONS`
to re-raise it out of dispatch so the assertion sees the original traceback.

```python
from veloce import TestClient, Veloce

app = Veloce()
app.config["PROPAGATE_EXCEPTIONS"] = True


@app.get("/boom")
async def boom():
    raise ValueError("something went wrong")


client = TestClient(app)

try:
    client.get("/boom")
    assert False, "expected the exception to propagate"
except ValueError as exc:
    assert str(exc) == "something went wrong"
```

Without the flag, the same call returns a response instead of raising:

```python
from veloce import TestClient, Veloce

app = Veloce()


@app.get("/boom")
async def boom():
    raise ValueError("something went wrong")


client = TestClient(app)

resp = client.get("/boom")
assert resp.status_code == 500
```

### Propagation resolution

`PROPAGATE_EXCEPTIONS` is a tri-state. When unset (`None`), it falls back to
the combination of `DEBUG` and `TESTING`: exceptions propagate only when both
are enabled. An explicit value always wins.

| `PROPAGATE_EXCEPTIONS` | `DEBUG` | `TESTING` | Exceptions propagate |
| --- | --- | --- | --- |
| `True` | (any) | (any) | Yes. |
| `False` | (any) | (any) | No — becomes a `500`. |
| `None` (default) | `True` | `True` | Yes. |
| `None` (default) | otherwise | otherwise | No — becomes a `500`. |

So enabling both `DEBUG` and `TESTING` is the implicit way to get propagation
without naming the key:

```python
from veloce import Veloce

app = Veloce(debug=True)
app.config["TESTING"] = True        # DEBUG + TESTING -> exceptions propagate
```

!!! note
    Registered exception handlers run *before* propagation is considered. A
    handler that returns a response for the raised type wins; propagation only
    applies to exceptions that reach the unhandled `500` path. See
    [Error handling](error-handling.md).

## Next steps

- Convert raised exceptions into responses — see [Error handling](error-handling.md).
- Write tests against the in-memory client — see [Testing](testing.md).
- Run the app for local development — see [Deployment](deployment.md).
- Full signatures are in the [API reference](../reference.md).
