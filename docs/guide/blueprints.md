---
description: Group routes into reusable Blueprints (APIRouter) in Veloce, register them onto an app under a URL prefix, scope hooks and error handlers, and nest blueprints.
tags: [blueprints, apirouter, routing, structure]
---

# Blueprints

A [`Blueprint`](../reference.md#veloce.Blueprint) is a deferred-registration
collection of routes, hooks, and error handlers that you attach to an
application later with [`register_blueprint`](../reference.md#veloce.Veloce.register_blueprint).
It lets you split a large application into self-contained modules and mount
each one under its own URL prefix.

```python title="app.py"
from veloce import Blueprint, Request, Veloce

app = Veloce()
admin = Blueprint("admin", url_prefix="/admin")


@admin.get("/dashboard")
async def dashboard(request: Request):
    return {"page": "dashboard"}


app.register_blueprint(admin)   # now serving GET /admin/dashboard
```

The blueprint owns nothing at runtime. When you call `register_blueprint`,
its routes are spliced into the application's radix tree under the combined
prefix, so dispatch is exactly as fast as if you had declared the routes on
the app directly.

## Constructing a blueprint

The first argument is the blueprint **name**, which is required and must be
unique per application. The name becomes the prefix of every route's
endpoint (`"<name>.<handler>"`), which is what [`url_for`](routing.md#reverse-urls)
and the hook-gating machinery use to identify blueprint routes.

```python
from veloce import Blueprint

blog = Blueprint(
    "blog",
    url_prefix="/blog",
)
```

`Blueprint` extends [`Router`](../reference.md#veloce.Router), so it accepts
the same keyword arguments: `url_prefix`, `default_response_class`,
`dependencies`, and `responses`. The route decorators (`get`, `post`, `put`,
`patch`, `delete`, `head`, `options`, the generic `route`, and `websocket`)
are all inherited.

!!! note
    `Router` names the prefix argument `prefix`; `Blueprint` names it
    `url_prefix` to match the Flask convention. Both strip a trailing slash.

## APIRouter

If you come from FastAPI, `APIRouter` is available as an alias for
`Blueprint` — it is the exact same class, exported under both names.

```python
from veloce import APIRouter, Request, Veloce

app = Veloce()
router = APIRouter("items", url_prefix="/items")


@router.get("/")
async def list_items(request: Request):
    return {"items": []}


app.register_blueprint(router)
```

## URL prefixes

The prefix can be set at construction or overridden at registration. Passing
`url_prefix=` to `register_blueprint` wins over the blueprint's own prefix,
which lets you mount the same blueprint twice — for example, to expose a v1
and a v2 of the same API surface.

```python
from veloce import Blueprint, Request, Veloce

app = Veloce()
api = Blueprint("api")


@api.get("/ping")
async def ping(request: Request):
    return {"pong": True}


app.register_blueprint(api, url_prefix="/v1")   # GET /v1/ping
app.register_blueprint(api, url_prefix="/v2")   # GET /v2/ping
```

Because the blueprint instance is never mutated by registration, the same
object can be registered onto more than one application.

## Scoped hooks

A blueprint can register `before_request`, `after_request`, and
`teardown_request` hooks that fire **only** for requests routed to one of its
own routes. This is the main difference from a plain `Router`, which has no
hooks.

```python
from veloce import Blueprint, Request, Veloce, g

app = Veloce()
admin = Blueprint("admin", url_prefix="/admin")


@admin.before_request
async def load_admin(request: Request):
    g.role = "admin"


@admin.get("/me")
async def whoami(request: Request):
    return {"role": g.role}


app.register_blueprint(admin)
```

The hook above runs for `/admin/me` but not for any app-level route. Use
`app.before_request` for application-wide hooks instead.

## Scoped error handlers

[`errorhandler`](../reference.md#veloce.Blueprint.errorhandler) registers an
error handler that catches exceptions raised by the blueprint's own
handlers. Integer keys register against a status code; class keys register
against an exception type (matched by MRO). App-level handlers act as the
fallback when no blueprint handler matches.

```python
from veloce import Blueprint, JSONResponse, Request, Veloce

app = Veloce()
shop = Blueprint("shop", url_prefix="/shop")


class OutOfStock(Exception):
    pass


@shop.errorhandler(OutOfStock)
async def handle_out_of_stock(request: Request, exc: OutOfStock):
    return JSONResponse({"error": "out of stock"}, status_code=409)


@shop.get("/buy/{sku}")
async def buy(sku: str):
    raise OutOfStock(sku)


app.register_blueprint(shop)
```

`exception_handler` is an alias for `errorhandler`, so either name works.
See [Error handling](error-handling.md) for the full handler model.

## Nested blueprints

A blueprint can mount another blueprint with its own
[`register_blueprint`](../reference.md#veloce.Blueprint.register_blueprint).
The child's routes register under the parent prefix plus the child prefix,
and the child's hooks and error handlers are merged into the parent. The
child's routes only reach the app once the parent is itself registered.

```python
from veloce import Blueprint, Request, Veloce

app = Veloce()
api = Blueprint("api", url_prefix="/api")
users = Blueprint("users", url_prefix="/users")


@users.get("/{user_id}")
async def get_user(user_id: int):
    return {"id": user_id}


api.register_blueprint(users)    # nest users under api
app.register_blueprint(api)      # GET /api/users/42
```

The endpoint of the nested route becomes `"api.users.get_user"`. A blueprint
cannot be registered as a child of itself — doing so raises `ValueError`.

!!! tip
    Override the child's prefix at nesting time by passing `url_prefix=` to
    the parent's `register_blueprint`, just as you can when registering onto
    an app.

## Inspecting registered blueprints

After registration, [`app.blueprints`](../reference.md#veloce.Veloce.blueprints)
returns a fresh dictionary mapping each blueprint name to its instance, and
[`app.iter_blueprints()`](../reference.md#veloce.Veloce.iter_blueprints)
yields the blueprint objects in registration order.

```python
from veloce import Blueprint, Veloce

app = Veloce()
app.register_blueprint(Blueprint("admin", url_prefix="/admin"))

assert "admin" in app.blueprints
for bp in app.iter_blueprints():
    print(bp.name)
```

The returned dictionary is a copy, so mutating it does not affect the
application. Re-registering a blueprint under an existing name overwrites the
previous entry in this map.

## Sharing dependencies across a blueprint

Because `Blueprint` extends `Router`, the constructor's `dependencies=` list
applies to every route on the blueprint. Per-route `dependencies=` are
appended to (not replaced by) the blueprint-level list.

```python
from veloce import Blueprint, Depends, Request, Veloce


async def require_api_key(request: Request):
    if request.headers.get("x-api-key") != "secret":
        return {"error": "unauthorized"}
    return None


app = Veloce()
secured = Blueprint("secured", url_prefix="/secure", dependencies=[Depends(require_api_key)])


@secured.get("/data")
async def data(request: Request):
    return {"value": 1}


app.register_blueprint(secured)
```

See [Dependency injection](dependency-injection.md) for the full dependency
model.

## Next steps

- [Parameters](parameters.md) — declare query, path, body, form, file,
  header, and cookie parameters on your blueprint handlers.
- [Routing](routing.md) — path converters, query parameters, and `url_for`.
- [Dependency injection](dependency-injection.md) — `Depends` and `Security`.
- [API reference](../reference.md) — `Blueprint`, `APIRouter`, `Router`.
