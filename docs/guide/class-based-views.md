---
description: Organise related handlers into classes with View and MethodView, dispatch HTTP verbs to async methods, and register class-based views with add_url_rule.
tags: [views, class-based, MethodView, dispatch]
---

# Class-based views

Class-based views group related logic into a class instead of a flat
function. Veloce offers two styles: [`View`](../reference/routers.md#veloce.View), where
one class handles a URL through a single `dispatch_request`, and
[`MethodView`](../reference/routers.md#veloce.MethodView), where one async method maps to
each HTTP verb.

```python
from veloce import Request, Veloce, View

app = Veloce()


class IndexView(View):
    async def dispatch_request(self, request: Request):
        return {"page": "index"}


app.add_url_rule("/", view_func=IndexView.as_view("index"))
```

`as_view("index")` builds an async view function from the class and gives
it the name `"index"` (used by `url_for` and route introspection).
[`add_url_rule`](../reference/application.md#veloce.Veloce.add_url_rule) registers it like
any other handler.

## How `as_view` works

`View.as_view(name, *class_args, **class_kwargs)` returns a coroutine
function that the router calls per request. Any extra positional or
keyword arguments are passed to the class constructor each time an
instance is built, which lets one class back several routes with
different configuration:

```python
from veloce import Request, Veloce, View

app = Veloce()


class GreetView(View):
    def __init__(self, greeting: str):
        self.greeting = greeting

    async def dispatch_request(self, request: Request):
        return {"message": self.greeting}


app.add_url_rule("/hi", view_func=GreetView.as_view("hi", "Hello"))
app.add_url_rule("/bye", view_func=GreetView.as_view("bye", "Goodbye"))
```

The generated view function carries `view_class`, `methods`, and
`__name__` for the router. The first argument `dispatch_request` receives
is the [`Request`](../reference/requests.md#veloce.Request); path parameters follow as
keyword arguments.

## Method-based dispatch with `MethodView`

[`MethodView`](../reference/routers.md#veloce.MethodView) dispatches to a method named
after the HTTP verb. Define `get`, `post`, `put`, `patch`, `delete`,
`head`, or `options` as `async def`; the allowed methods are inferred from
which verbs you implement.

```python
from veloce import MethodView, Request, Veloce

app = Veloce()


class UserView(MethodView):
    async def get(self, request: Request, id: int):
        return {"action": "read", "id": id}

    async def post(self, request: Request, id: int):
        return {"action": "create", "id": id}


app.add_url_rule("/users/{id:int}", view_func=UserView.as_view("user"))
```

A `GET /users/1` calls `get`, a `POST /users/1` calls `post`. A request
for a verb the class does not implement raises `MethodNotAllowed`, which
Veloce turns into a `405` response with an `Allow` header listing the
verbs the view does support.

!!! note
    Veloce handlers are `async def`. A `MethodView` whose verb method is a
    plain `def` raises `TypeError` at class-definition time, not at
    request time — so the mistake surfaces immediately on import.

## Path and query parameters

The verb methods receive path parameters as keyword arguments, the same
way function handlers do. Declarative parameter markers and dependency
injection work too — annotate the method parameters as you would on a
function handler.

```python
from veloce import MethodView, Query, Request, Veloce

app = Veloce()


class SearchView(MethodView):
    async def get(self, request: Request, q: str = Query(default="")):
        return {"query": q}


app.add_url_rule("/search", view_func=SearchView.as_view("search"))
```

See [Parameters](parameters.md) and
[Dependency injection](dependency-injection.md) for the full set of
options available on these method parameters.

## Instance lifetime

By default a fresh instance is constructed for every request
(`init_every_request = True`), so per-request attributes set in
`dispatch_request` never leak between requests. Set the class attribute to
`False` to reuse a single shared instance across all requests — only safe
when the view holds no per-request mutable state.

```python
from veloce import Request, Veloce, View

app = Veloce()


class CachedView(View):
    init_every_request = False

    def __init__(self):
        self.calls = 0

    async def dispatch_request(self, request: Request):
        self.calls += 1
        return {"calls": self.calls}


app.add_url_rule("/counter", view_func=CachedView.as_view("counter"))
```

!!! warning
    With `init_every_request = False` the single instance is shared across
    concurrent requests. Do not store request-scoped data on `self` in
    that mode — use [`g`](helpers.md) or `request.state` instead.

## Declaring methods on a plain `View`

A base `View` answers `GET` by default. To advertise other verbs (for
routing and OpenAPI introspection) set the `methods` class attribute and
read `request.method` inside `dispatch_request` yourself:

```python
from veloce import Request, Veloce, View

app = Veloce()


class FormView(View):
    methods = ["GET", "POST"]

    async def dispatch_request(self, request: Request):
        if request.method == "POST":
            return {"submitted": True}
        return {"form": "empty"}


app.add_url_rule("/form", view_func=FormView.as_view("form"))
```

## Applying decorators

The `decorators` class attribute holds a list applied to the generated
view function. Entries are applied innermost-first, so the last entry
wraps outermost. This is the place for cross-cutting wrappers that should
apply to the whole view regardless of which verb runs.

```python
import functools

from veloce import MethodView, Request, Veloce

app = Veloce()


def log_calls(view_func):
    @functools.wraps(view_func)
    async def wrapper(request: Request, **kwargs):
        print(f"{request.method} {request.path}")
        return await view_func(request, **kwargs)

    return wrapper


class ReportView(MethodView):
    decorators = [log_calls]

    async def get(self, request: Request):
        return {"report": "ok"}


app.add_url_rule("/report", view_func=ReportView.as_view("report"))
```

## Testing class-based views

Class-based views are ordinary registered routes, so the in-memory
[`TestClient`](testing.md) drives them like any handler:

```python
from veloce import MethodView, Request, TestClient, Veloce

app = Veloce()


class PingView(MethodView):
    async def get(self, request: Request):
        return {"pong": True}


app.add_url_rule("/ping", view_func=PingView.as_view("ping"))


def test_ping():
    client = TestClient(app)
    assert client.get("/ping").json() == {"pong": True}
    assert client.post("/ping").status_code == 405
```

## Next steps

- [Routing](routing.md) — path parameters, converters, and the verb
  shortcuts class-based views build on.
- [Parameters](parameters.md) — declarative markers usable on view methods.
- [Dependency injection](dependency-injection.md) — `Depends` and
  `Security` in view methods.
- API reference: [`View`](../reference/routers.md#veloce.View),
  [`MethodView`](../reference/routers.md#veloce.MethodView).
