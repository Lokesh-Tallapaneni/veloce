---
description: Subscribe to Veloce's built-in lifecycle signals and define your own named signals with a lightweight connect/send pub/sub API.
tags: [signals, pubsub, lifecycle, events]
---

# Signals

Signals are a small publish/subscribe layer for decoupling code. A
receiver connects to a named signal; whenever something sends that
signal, every connected receiver fires. Veloce uses signals internally
for request-lifecycle notifications, and you can define your own.

The built-in signals, plus `Signal` and `Namespace`, are exported from the
top-level `veloce` package; `veloce.signals` works too.

```python
from veloce.signals import request_finished


def log_response(sender, request, response):
    print(f"{request.method} {request.path} -> {response.status_code}")


request_finished.connect(log_response, weak=False)
```

Every receiver is called with the sender as its first positional
argument, followed by the signal's keyword arguments. For the built-in
request signals the sender is the application instance.

## Connecting receivers

`Signal.connect` registers a receiver and returns it unchanged, so it
doubles as a decorator:

```python
from veloce.signals import request_started


@request_started.connect
def on_request(sender, request):
    print("request started:", request.path)
```

The receiver signature must accept the sender positionally plus the
keyword arguments the signal sends. A receiver that ignores most of
them can collect them with `**kwargs`:

```python
from veloce.signals import request_started


@request_started.connect
def on_request(sender, **kwargs):
    request = kwargs["request"]
    print("request started:", request.path)
```

!!! warning "Receivers are held by weak reference by default"
    `connect(receiver)` stores the receiver as a weak reference
    (`weak=True` is the default). If nothing else keeps the receiver
    alive — for example a function defined inside another function, or a
    lambda — it is garbage-collected and silently stops firing. Module-
    level functions are safe because the module holds them. For anything
    else, pass `weak=False`:

    ```python
    from veloce.signals import request_started

    request_started.connect(lambda sender, **kw: None, weak=False)
    ```

## Disconnecting

`Signal.disconnect` removes a previously connected receiver:

```python
from veloce.signals import request_finished


def audit(sender, request, response):
    ...


request_finished.connect(audit, weak=False)
request_finished.disconnect(audit)
```

## The built-in signals

Veloce fires these signals over the request and application-context
lifecycle. Each is a module-level `Signal` singleton in `veloce.signals`.
The sender is always the application instance (for `message_flashed` and
the app-context signals it is the current app).

| Signal | Fires | Keyword arguments |
|--------|-------|-------------------|
| `request_started` | Before the handler runs | `request` |
| `request_finished` | After a response is produced | `request`, `response` |
| `got_request_exception` | When a handler raises | `exception` |
| `request_tearing_down` | After every request, during teardown | `exc` (may be `None`) |
| `message_flashed` | On every `flash()` call | `message`, `category` |
| `appcontext_pushed` | When an app context is entered | — |
| `appcontext_popped` | When an app context exits | — |
| `appcontext_tearing_down` | While an app context is torn down | `exc` (may be `None`) |

A complete request-logging example:

```python
from veloce import Veloce, request
from veloce.signals import got_request_exception, request_finished

app = Veloce()


@request_finished.connect
def log_ok(sender, request, response):
    print(f"{request.method} {request.path} -> {response.status_code}")


@got_request_exception.connect
def log_error(sender, exception):
    print("handler raised:", repr(exception))


@app.get("/")
async def index(request):
    return {"message": "Hello"}
```

!!! warning "Use synchronous receivers for the built-in signals"
    The framework fires the built-in signals with `Signal.send`, which
    calls each receiver and uses its return value but does not await
    coroutines. A receiver connected to `request_started`,
    `request_finished`, or the other built-ins should be a plain `def`,
    not `async def` — an async receiver's coroutine would be created and
    dropped without running. Do blocking-free, fast work in these
    receivers, or hand off heavy work to a background task.

### Reacting to flashed messages

`message_flashed` fires for every [`flash()`](helpers.md#flash) call, with the
message text and its category:

```python
from veloce import Veloce, flash
from veloce.signals import message_flashed

app = Veloce()


@message_flashed.connect
def record_flash(sender, message, category):
    print(f"flashed [{category}]: {message}")


@app.get("/save")
async def save(request):
    flash("Saved.", "success")
    return {"ok": True}
```

### Reaching signals through the app

The [`app.signal_namespace`](../reference/application.md#veloce.Veloce.signal_namespace)
property returns the `veloce.signals` module, so
`app.signal_namespace.request_started` is the same object you would import
directly:

```python
from veloce import Veloce
from veloce.signals import request_started

app = Veloce()

assert app.signal_namespace.request_started is request_started
```

## Defining your own signals

Create a `Signal` directly when you only need one, or use a `Namespace`
to mint named signals on demand. A namespace returns the same `Signal`
instance for a given name every time, so two parts of an application can
share a signal by agreeing on its name rather than passing the instance
around:

```python
from veloce.signals import Namespace

signals = Namespace()
user_registered = signals.signal("user-registered")


@user_registered.connect
def send_welcome_email(sender, user):
    print("welcome,", user)


# Anywhere with the same name gets the same Signal object.
assert signals.signal("user-registered") is user_registered

user_registered.send("auth", user="alice")
```

`Signal.send` returns a list of `(receiver, return_value)` pairs in
registration order, so a sender can inspect what fired and what each
receiver returned:

```python
from veloce.signals import Signal

ready = Signal("ready")


@ready.connect
def double(sender, value):
    return value * 2


results = ready.send("main", value=21)
assert results == [(double, 42)]
```

## Filtering by sender

By default a receiver fires for every send. Pass `sender=` to `connect`
to restrict it to one sender; `send(sender)` then fires only the
receivers registered for that exact sender (compared by identity, then
by equality) plus those registered for every sender:

```python
from veloce.signals import Signal

login = Signal("login")
fired = []

login.connect(lambda sender, **kw: fired.append(sender), sender="web", weak=False)

login.send("api")  # receiver does not fire — different sender
login.send("web")  # receiver fires
assert fired == ["web"]
```

The sentinel for "all senders" is `ANY_SENDER`, the default for both
`connect` and `disconnect`. To detach a sender-specific subscription,
pass the same `sender` to `disconnect`.

## Letting receivers fail independently

With plain `send`, an exception in one receiver aborts the send and
propagates to the caller. Use `Signal.send_robust` when one misbehaving
receiver should not stop the others. It logs each failure at `WARNING`
and substitutes the `Exception` instance into that receiver's slot in the
result list, so subsequent receivers still fire:

```python
from veloce.signals import Signal

ready = Signal("ready")


@ready.connect
def good(sender, **kw):
    return "ok"


@ready.connect
def bad(sender, **kw):
    raise RuntimeError("boom")


results = ready.send_robust("main")
# [(good, "ok"), (bad, RuntimeError("boom"))]
for receiver, value in results:
    if isinstance(value, Exception):
        print("receiver failed:", value)
```

!!! note "Async receivers need an async send"
    `send` and `send_robust` are synchronous — they do not await
    coroutines. If a receiver may be `async def`, use `asend` (the async
    counterpart of `send`) or `send_robust_async` (the async counterpart of
    `send_robust`, which applies the same per-receiver error handling).
    Both await coroutine-returning receivers **concurrently** — async
    receivers run together rather than one after another — while sync
    receivers still run inline. Under plain `send_robust`, an async
    receiver's coroutine is closed and a `TypeError` is recorded in its
    result slot.

    ```python
    import asyncio

    from veloce.signals import Signal

    ready = Signal("ready")


    @ready.connect
    async def notify(sender, **kw):
        await asyncio.sleep(0)
        return "done"


    async def main():
        return await ready.send_robust_async("main")


    print(asyncio.run(main()))  # [(notify, "done")]
    ```

## Checking for receivers

`has_receivers_for` reports whether any connected receiver would fire for
a given sender. Use it to skip building expensive payloads when nothing
is listening — the framework guards its own sends this way:

```python
from veloce.signals import Signal

metrics = Signal("metrics")

if metrics.has_receivers_for("web"):
    metrics.send("web", snapshot="...")
```

## Next steps

- [Flask-style helpers](helpers.md) — `flash`, `g`, `current_app`, and
  the request context the lifecycle signals fire within.
- [Middleware](middleware.md) — for cross-cutting request/response
  logic that needs to modify the response, not just observe it.
- The [API reference](../reference/index.md) documents the public `veloce`
  surface; `Signal`, `Namespace`, and the standard signals live in
  `veloce.signals`.
