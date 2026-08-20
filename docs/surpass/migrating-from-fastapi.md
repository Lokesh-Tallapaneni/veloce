---
description: >-
  Migrate a FastAPI app to Veloce — a divergence map covering the install name, OAuth2 token_url,
  whole-body params, and explicit response_model.
tags: [migration, fastapi, divergence, compatibility]
---

# Migrating from FastAPI

Veloce is not built on Starlette, so most FastAPI code runs unchanged but a
handful of names and behaviours diverge.

This page maps every divergence to its Veloce form, then lists what Veloce adds
beyond FastAPI.

Most rows are one-line edits; the behavioural ones (whole-body params,
`response_model` inference) bite silently, so read those first.

```bash
pip install veloceframework
```

!!! warning "The package is `veloceframework`, not `veloce`"
    `pip install veloce` installs an unrelated project. The import name is still
    `veloce` (`from veloce import Veloce`); only the PyPI distribution name differs.

## Divergence map

Each row is a FastAPI idiom and its Veloce equivalent. The behavioural rows
(marked) change runtime results, not just spelling — the rest are renames.

| FastAPI | Veloce | Notes |
| --- | --- | --- |
| `pip install fastapi` | `pip install veloceframework` | Import name stays `veloce`. |
| `OAuth2PasswordBearer(tokenUrl=...)` | `OAuth2PasswordBearer(token_url=...)` | snake_case; advanced schemes keep camelCase (below). |
| Two body-model params split the JSON | Each body param gets the **whole** body | Behavioural — nest in one model. |
| `response_model` inferred from `-> Item` | `response_model=Item` declared explicitly | Behavioural — no return-annotation inference. |
| `response_model=Union[A, B]` filters output | Documents only, does **not** filter | Behavioural — passes the value through unchanged. |
| `?flag=on` / `?flag=t` coerce to `True` | Only `true`, `1`, `yes` (case-insensitive) | Behavioural — other strings are `False`. |
| `FastAPI(docs_url="")` to disable | `Veloce(docs_url=None)` | `""` is a real (empty) path, not "off". |
| `Response(content=..., media_type=...)` | `Response(body=..., content_type=...)` | Constructor kwargs renamed. |
| `WSGIMiddleware` | Not provided | Veloce is ASGI-and-native only. |
| `generate_unique_id_function=...` | `operation_id=...` + auto-disambiguation | Set IDs per route; collisions are suffixed. |
| `TestClient(app, raise_server_exceptions=...)` | `TestClient(app)` + `PROPAGATE_EXCEPTIONS` config | Different propagation control (below). |
| `APIRouter(prefix=..., tags=...)` | Same spelling — `APIRouter` aliases `Router` | `include_router` registers it; named groups with hooks use `Blueprint` (below). |
| `RequestValidationError` outside `HTTPException` | Subclasses `HTTPException` | Behavioural — a broad `HTTPException` handler also catches 422s (below). |
| `def` dependency runs in the threadpool | Runs inline on the event loop | Behavioural — pass `Depends(fn, offload=True)` for blocking sync work. |
| `return "text"` serialises to JSON | `str`/`bytes` return as `text/html` | Behavioural — wrap in `JSONResponse` (or return a dict) for JSON strings. |

## Routers and route groups

`APIRouter` is Veloce's `Router` under the FastAPI name, and the familiar
shape works unchanged:

```python
from veloce import APIRouter, Veloce

router = APIRouter(prefix="/items", tags=["items"])


@router.get("/")
async def list_items():
    return []


app = Veloce()
app.include_router(router)
```

Veloce also has [`Blueprint`](../reference/routers.md#veloce.Blueprint) — a *named*
route group carrying its own request hooks and scoped error handlers
(Flask's primitive). Reach for it when a group needs per-group
`before_request` / `errorhandler` behaviour; plain prefix-and-tags grouping
needs only `APIRouter` / `Router`.

## Exception-handler scope includes validation errors

In Veloce, [`RequestValidationError`](../reference/exceptions.md#veloce.RequestValidationError)
(and `ValidationError`) subclass `HTTPException`, so a handler registered for
`HTTPException` also intercepts 422 validation failures. A FastAPI app that
relies on validation errors bypassing its `HTTPException` handler should
register a dedicated `RequestValidationError` handler first — the most
specific class wins. Note also that `from veloce import ValidationError` is
Veloce's exception, not Pydantic's: the two are different classes.

## Sync dependencies run inline

FastAPI offloads every `def` dependency to its threadpool. Veloce runs sync
dependencies inline on the event loop — faster for the pure-computation
case, but a blocking call (a synchronous DB driver, `requests`) will stall
the loop. Mark those explicitly:

```python
from veloce import Depends


def lookup_user(user_id: int):  # blocking I/O inside
    ...


@app.get("/users/{user_id}")
async def show(user=Depends(lookup_user, offload=True)):
    return user
```

!!! tip "Catch the ones you miss"

    Set `app.config["EVENT_LOOP_WATCHDOG"] = True` in development. When a call
    blocks the loop, the watchdog logs the stall and names the route and
    dependency it happened in — e.g. `while serving GET /users/{user_id} ->
    lookup_user` — so the offending `Depends` is obvious. It is off by default
    and meant for development only.

## OAuth2 `token_url`

[`OAuth2PasswordBearer`](../reference/security.md#veloce.OAuth2PasswordBearer) takes
`token_url` in snake_case, matching the rest of the Veloce API.

```python
from veloce import Depends, Veloce
from veloce.security import OAuth2PasswordBearer

app = Veloce()
oauth2 = OAuth2PasswordBearer(token_url="/token")


@app.get("/users/me")
async def me(token: str = Depends(oauth2)):
    return {"token": token}
```

The advanced schemes —
[`OAuth2AuthorizationCodeBearer`](../reference/security.md#veloce.OAuth2AuthorizationCodeBearer)
and [`OpenIdConnect`](../reference/security.md#veloce.OpenIdConnect) — keep the camelCase
spelling because those are the field names the OpenAPI security-scheme object
defines, so a scheme copied from a standard OpenAPI document maps over without
renaming. The camelCase fields are:

| Field | Spelling |
| --- | --- |
| authorization URL | `authorizationUrl` |
| token URL | `tokenUrl` |
| refresh URL | `refreshUrl` |
| OpenID Connect URL | `openIdConnectUrl` |

```python
from veloce import Veloce
from veloce.security import OAuth2AuthorizationCodeBearer

app = Veloce()
oauth2 = OAuth2AuthorizationCodeBearer(
    authorizationUrl="https://auth.example.com/authorize",
    tokenUrl="https://auth.example.com/token",
    scopes={"read": "Read access"},
)
```

## Multiple body params each receive the whole body

In FastAPI, two Pydantic-model parameters split the JSON object into named
fields. In Veloce, **each** body model is validated against the entire request
body. Two model params expect two independent copies of the same JSON document.

```python
from pydantic import BaseModel

from veloce import TestClient, Veloce


class Item(BaseModel):
    name: str


app = Veloce()


@app.post("/items")
async def create(item: Item):
    return {"name": item.name}


client = TestClient(app)

resp = client.post("/items", json={"name": "spoon"})
assert resp.status_code == 200
assert resp.json() == {"name": "spoon"}
```

!!! warning "Nest multiple bodies in one model"
    To accept two structured payloads, wrap them in a single outer model
    (`class Order(BaseModel): item: Item; user: User`) and take one parameter.
    Declaring two model parameters validates the *same* body twice — it does not
    key them by parameter name.

## `response_model` is explicit, never inferred

Veloce does not read the return annotation. A `-> Item` hint documents nothing
and filters nothing; pass `response_model=` on the route to shape and prune the
output.

```python hl_lines="18"
from pydantic import BaseModel

from veloce import TestClient, Veloce


class UserIn(BaseModel):
    name: str
    password: str


class UserOut(BaseModel):
    name: str


app = Veloce()


@app.post("/users", response_model=UserOut)
async def create_user(user: UserIn):
    return user


client = TestClient(app)

resp = client.post("/users", json={"name": "ada", "password": "secret"})
assert resp.status_code == 200
assert resp.json() == {"name": "ada"}
```

The `password` field is dropped because `response_model=UserOut` validates the
return value and emits only that model's fields.

!!! warning "`Union` response models document but do not filter"
    `response_model=A | B` (or `Union[A, B]`) is emitted into the OpenAPI schema,
    but the return value passes through unchanged — Veloce does not pick a member
    of the union to validate against. Use a single model when you need filtering.

## Bool query coercion accepts only `true`, `1`, `yes`

A `bool` query, path, header, or cookie parameter is `True` only when the value
(lower-cased) is `true`, `1`, or `yes`. Every other string is `False`.

```python
from veloce import TestClient, Veloce

app = Veloce()


@app.get("/search")
async def search(verbose: bool = False):
    return {"verbose": verbose}


client = TestClient(app)

assert client.get("/search?verbose=yes").json() == {"verbose": True}
assert client.get("/search?verbose=on").json() == {"verbose": False}
```

## Disabling docs uses `None`, not `""`

The conditional-OpenAPI knobs treat an empty string as a real (empty) path. Pass
`None` to turn a docs surface off.

```python
from veloce import Veloce

app = Veloce(docs_url=None, redoc_url=None, openapi_url=None)
```

Each knob defaults to its usual path; set any to `None` to remove just that one.

| Knob | Default path |
| --- | --- |
| `docs_url` | `/docs` |
| `redoc_url` | `/redoc` |
| `openapi_url` | `/openapi.json` |

## `Response(body=, content_type=)`

The base [`Response`](../reference/responses.md#veloce.Response) constructor uses `body`
and `content_type`, not `content` and `media_type`.

```python
from veloce import Response, Veloce

app = Veloce()


@app.get("/raw")
async def raw():
    return Response(body=b"<x/>", content_type="application/xml")
```

The signature is
`Response(status_code=200, body=b"", content_type="text/plain", headers=None, background=None)`.

## No WSGI bridge or parameter-model shortcuts

Some FastAPI surfaces have no Veloce counterpart. These are intentional gaps,
not pending features.

- No `WSGIMiddleware` — Veloce serves ASGI and its own native transport; mount a
  WSGI app behind a separate server.
- No query/cookie/header **parameter models** (a Pydantic model that fans out
  into individual query/header params). Declare the parameters individually.
- No `bytes = File()` or `list[UploadFile]` shorthand — use
  [`UploadFile`](../reference/requests.md#veloce.UploadFile) per file.
- No `generate_unique_id_function`. Set `operation_id=` per route; colliding
  auto-generated IDs are deterministically suffixed when
  `disambiguate_operation_ids` is on (the default).

## TestClient has no `raise_server_exceptions`

Veloce's [`TestClient`](../reference/testing.md#veloce.TestClient) signature is
`TestClient(app, base_url=..., follow_redirects=False)`. There is no
`raise_server_exceptions`, `backend`, or `root_path` argument. To make an
unhandled handler exception re-raise into the test instead of becoming a `500`,
set the config instead.

```python hl_lines="4"
from veloce import TestClient, Veloce

app = Veloce()
app.config["PROPAGATE_EXCEPTIONS"] = True


@app.get("/boom")
async def boom():
    raise RuntimeError("nope")


client = TestClient(app)
```

With `PROPAGATE_EXCEPTIONS` set, `client.get("/boom")` raises `RuntimeError`.
Propagation is also implicit when both `DEBUG` and `TESTING` are enabled
(`app.config["DEBUG"] = True; app.config["TESTING"] = True`); left at the
defaults, the client receives a `500` response as in production.

## What Veloce adds

These are the capabilities a migrated app gains. None has a FastAPI equivalent
out of the box.

| Capability | Veloce | What it replaces |
| --- | --- | --- |
| Native server | `app.run()` | No uvicorn process needed for development. |
| In-memory tests | `TestClient` / `AsyncTestClient` | No live socket or ASGI transport mock. |
| Request-scoped helpers | `g`, `flash`, `current_app` | Flask-style globals, async-safe via contextvars. |
| App config dict | `app.config` | A mutable Flask-style mapping, not Pydantic settings. |
| App-scoped tasks | `app.spawn(coro)` | Long-lived tasks drained on shutdown. |

### Run without uvicorn

`app.run()` starts a built-in development server — no separate process, no
`uvicorn module:app` command. The app object is still ASGI-callable, so
production can run under uvicorn or the gunicorn worker unchanged.

```python
from veloce import Veloce

app = Veloce()


@app.get("/")
async def index():
    return {"message": "Welcome to Veloce!"}


if __name__ == "__main__":
    app.run(port=8000)
```

!!! note "`run()` is a development server"
    `app.run()` binds to `127.0.0.1` by default and is single-process. For
    production, serve the same `app` under uvicorn or the gunicorn
    `VeloceWorker`. See [Deployment](../guide/deployment.md).

### In-memory TestClient and request helpers

The [`TestClient`](../reference/testing.md#veloce.TestClient) constructs ASGI scopes
directly and runs handlers on its own loop — no socket. The Flask-style helpers
[`g`](../guide/helpers.md), [`flash`](../reference/helpers.md#veloce.flash), and
[`current_app`](../guide/helpers.md) are backed by contextvars,
so they stay correct per-request under concurrency.

```python
from veloce import TestClient, Veloce, g

app = Veloce()


@app.get("/whoami")
async def whoami():
    g.user = "ada"
    return {"user": g.user}


client = TestClient(app)

resp = client.get("/whoami")
assert resp.status_code == 200
assert resp.json() == {"user": "ada"}
```

For async tests, [`AsyncTestClient`](../reference/testing.md#veloce.AsyncTestClient) runs
on the test's own running loop:

```python
from veloce import AsyncTestClient, Veloce

app = Veloce()


@app.get("/")
async def index():
    return {"ok": True}


async def test_index():
    async with AsyncTestClient(app) as client:
        resp = await client.get("/")
        assert resp.json() == {"ok": True}
```

## Next steps

- [Security schemes](../guide/security-schemes.md) — the snake_case/camelCase OAuth2 split in full.
- [Requests and responses](../guide/requests-responses.md) — `Response(body=, content_type=)` and return-value semantics.
- [Testing](../guide/testing.md) — `TestClient`, `AsyncTestClient`, and exception propagation.
- [Deployment](../guide/deployment.md) — running the migrated app under uvicorn, gunicorn, or `app.run()`.
- Full signatures are in the [API reference](../reference/index.md).
