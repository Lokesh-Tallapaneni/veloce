# Plugins

A plugin bundles a component's setup — routes, middleware, startup hooks, shared
state — behind a single `app.install(...)` call. Any object with an
`install(self, app)` method is a plugin; you do not need to import or subclass
anything.

!!! note "Added in version 0.11"

```python
from veloce import Veloce

class GreetingPlugin:
    name = "greeting"

    def __init__(self, message: str = "hello") -> None:
        self.message = message

    def install(self, app: Veloce) -> None:
        @app.get("/greet")
        async def greet() -> dict[str, str]:
            return {"message": self.message}

app = Veloce()
app.install(GreetingPlugin(message="hi there"))
```

`app.install()` runs the plugin's `install(app)` immediately and returns the
plugin, so you can keep a handle to it:

```python
from veloce import Veloce

class CounterPlugin:
    name = "counter"

    def __init__(self) -> None:
        self.hits = 0

    def install(self, app: Veloce) -> None:
        @app.before_request
        async def bump() -> None:
            self.hits += 1

app = Veloce()
counter = app.install(CounterPlugin())
# reachable anywhere the app is:
assert app.extensions["counter"] is counter
```

## The `name` convention

If a plugin sets a `name` attribute, `app.install()` records it under
`app.extensions[name]`, giving you a discoverable handle from anywhere that holds
the app. Installing two plugins with the same `name` raises `ValueError`. A plugin
with no `name` installs fire-and-forget — useful for one that only registers
middleware or hooks.

## Requiring another plugin

There is no dependency resolver: plugins install in call order, and a plugin that
needs another checks `app.extensions` itself.

```python
from veloce import Veloce

class AdminPlugin:
    name = "admin"

    def install(self, app: Veloce) -> None:
        if "auth" not in app.extensions:
            raise RuntimeError("AdminPlugin requires an auth plugin installed first")
        # ... register admin routes ...

app = Veloce()
# install the auth plugin before this one, or AdminPlugin raises.
```

## Startup work

`install()` runs synchronously at registration time, so do event-loop work (opening
a connection pool, etc.) in a startup hook the plugin registers itself:

```python
from veloce import Veloce

class PoolPlugin:
    name = "pool"

    def install(self, app: Veloce) -> None:
        @app.on_startup
        async def connect() -> None:
            self.pool = await open_pool()  # your async setup

app = Veloce()
app.install(PoolPlugin())
```

## Writing a distributable plugin

Because the contract is just "an object with `install(self, app)`", a plugin can
live in its own PyPI package. Publish a class, document the `name` it registers,
and users install it with one line.

## What's next

- [Middleware](middleware.md) — the request/response hooks a plugin often registers.
- [Lifespan & Events](lifespan.md) — `on_startup` / `on_shutdown` for async setup.
- [Dependency Injection](dependency-injection.md) — sharing plugin state with handlers.
