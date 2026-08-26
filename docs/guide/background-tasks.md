---
description: Run work after the response is sent with BackgroundTask and BackgroundTasks — inject a task queue into a handler or attach a task directly to a response.
tags: [background, tasks, async, response]
---

# Background Tasks

A background task runs *after* the response has been sent to the client. Use
it for work that should not delay the reply — sending a confirmation email,
writing an audit log, invalidating a cache. Veloce ships two primitives:
[`BackgroundTask`](../reference/tasks.md#veloce.BackgroundTask) for a single callable
and [`BackgroundTasks`](../reference/tasks.md#veloce.BackgroundTasks) for an ordered
queue of them.

## A first example

The simplest path is to inject a `BackgroundTasks` queue into your handler.
Annotate a parameter with the `BackgroundTasks` type and Veloce hands you a
fresh queue for the request:

```python
from veloce import BackgroundTasks, Veloce

app = Veloce()


def write_log(message: str) -> None:
    with open("audit.log", "a", encoding="utf-8") as fh:
        fh.write(message + "\n")


@app.post("/users")
async def create_user(tasks: BackgroundTasks):
    # ... create the user ...
    tasks.add_task(write_log, "user created")
    return {"created": True}
```

The response goes out immediately with `{"created": True}`. Once it is on the
wire, Veloce runs `write_log("user created")`. The client never waits for the
log write.

## How injection works

The `BackgroundTasks` parameter is filled by its **type annotation**, not by
[`Depends`](dependency-injection.md). Any parameter annotated `BackgroundTasks`
receives the per-request queue:

```python
from veloce import BackgroundTasks, Veloce

app = Veloce()


async def notify(user_id: int) -> None:
    # async tasks are awaited directly on the event loop
    ...


@app.post("/items/{item_id}")
async def create_item(item_id: int, tasks: BackgroundTasks):
    tasks.add_task(notify, item_id)
    return {"item": item_id}, 201
```

`add_task` takes the callable first, then any positional and keyword arguments
to call it with. They are stored and applied later:

```python
from veloce import BackgroundTasks, Veloce

app = Veloce()


def send_email(to: str, *, subject: str) -> None:
    ...


@app.post("/signup")
async def signup(tasks: BackgroundTasks):
    tasks.add_task(send_email, "ada@example.com", subject="Welcome")
    return {"ok": True}
```

Tasks added to one queue run **sequentially, in the order added**, after the
response is sent.

!!! note
    The `BackgroundTasks` parameter is not injected into WebSocket handshake
    handlers — a WebSocket connection has no response cycle to attach
    after-response work to.

## Sync and async tasks

A task callable may be either `async def` or a plain `def`. Async callables
are awaited on the event loop. Sync callables are run in a thread-pool
executor so they do not block the loop, and they run inside a copy of the
request's context — so Flask-style helpers such as
[`g`](helpers.md) and [`current_app`](helpers.md) remain available inside a
sync task.

```python
from veloce import BackgroundTasks, Veloce

app = Veloce()


async def async_task() -> None:
    ...


def blocking_task() -> None:
    # CPU-bound or blocking I/O — runs off the event loop
    ...


@app.post("/mix")
async def mix(tasks: BackgroundTasks):
    tasks.add_task(async_task)
    tasks.add_task(blocking_task)
    return {"queued": 2}
```

## Attaching a task to a response

If you build a [`Response`](../reference/responses.md#veloce.Response) object yourself,
attach a single [`BackgroundTask`](../reference/tasks.md#veloce.BackgroundTask) — or
a whole `BackgroundTasks` queue — through the `background` argument:

```python
from veloce import BackgroundTask, Response, Veloce

app = Veloce()


def cleanup(token: str) -> None:
    ...


@app.get("/download")
async def download():
    return Response(
        body=b"file contents",
        content_type="application/octet-stream",
        background=BackgroundTask(cleanup, "abc123"),
    )
```

`BackgroundTask(func, *args, **kwargs)` stores the callable and the
arguments to call it with later. The dispatcher fires the attached task
fire-and-forget after the response is sent.

You can attach a `BackgroundTasks` collection the same way when you have
more than one task:

```python
from veloce import BackgroundTasks, Response, Veloce

app = Veloce()


def step_one() -> None:
    ...


def step_two() -> None:
    ...


@app.get("/report")
async def report():
    tasks = BackgroundTasks()
    tasks.add_task(step_one)
    tasks.add_task(step_two)
    return Response(body=b"queued", background=tasks)
```

!!! warning "`StreamingResponse` and `RedirectResponse` take no `background=`"
    [`Response`](../reference/responses.md#veloce.Response),
    [`JSONResponse`](../reference/responses.md#veloce.JSONResponse) and
    `PlainTextResponse` all accept `background=` in their constructor.
    `StreamingResponse` and `RedirectResponse` do **not**. To attach a task to
    one of those, set the attribute after constructing it:

    ```python
    from veloce import BackgroundTask, JSONResponse, Veloce

    app = Veloce()


    def cleanup() -> None:
        ...


    @app.get("/data")
    async def data():
        response = JSONResponse({"ok": True})
        response.background = BackgroundTask(cleanup)
        return response
    ```

## Mixing both forms

A handler may use the injected queue *and* a response-attached task at the
same time. Both fire after the response is sent:

```python
from veloce import BackgroundTask, BackgroundTasks, Response, Veloce

app = Veloce()


def from_queue() -> None:
    ...


def from_response() -> None:
    ...


@app.post("/both")
async def both(tasks: BackgroundTasks):
    tasks.add_task(from_queue)
    return Response(
        body=b"ok",
        background=BackgroundTask(from_response),
    )
```

## Error handling

Background tasks run *after* the response, so a failing task cannot change
what the client already received. When a task in a `BackgroundTasks` queue
raises, the exception is logged (to the `veloce.background` logger) and the
remaining tasks still run. The response itself is never affected.

```python
from veloce import BackgroundTasks, Veloce

app = Veloce()


def will_fail() -> None:
    raise RuntimeError("boom")


def still_runs() -> None:
    ...


@app.post("/resilient")
async def resilient(tasks: BackgroundTasks):
    tasks.add_task(will_fail)   # logged, does not abort the queue
    tasks.add_task(still_runs)  # runs regardless
    return {"ok": True}
```

!!! tip
    Background tasks run in the same process and event loop as your app. They
    are fire-and-forget and not persisted — if the process exits before a task
    finishes, the work is lost. For durable, retryable, or cross-process work,
    reach for a dedicated task queue instead.

## App-scoped long-lived tasks

`BackgroundTask`/`BackgroundTasks` are request-scoped and fire-and-forget. For
work that should live for the application's lifetime — a queue poller, a metrics
flusher, a heartbeat — use `app.spawn(coro, *, name=None)`. A spawned task is
held with a strong reference (so the event loop cannot garbage-collect it
mid-flight) and is automatically cancelled and awaited during shutdown, within
the per-task `GRACEFUL_TASK_TIMEOUT` budget (default 10 seconds).

```python
from veloce import Veloce

app = Veloce()


async def poll_queue():
    while True:
        await do_one_unit_of_work()


@app.on_startup
async def start_workers():
    app.spawn(poll_queue(), name="queue-poller")
```

Name a task to retrieve it with `app.get_spawned_task("queue-poller")` or stop
it early with `app.cancel_spawned_task("queue-poller")`. A duplicate name raises
`ValueError`. `spawn` must be called with a running event loop — typically from
an `on_startup` handler, the lifespan context, or a request handler — and raises
`RuntimeError` otherwise. Failures are logged through the same path as
request-scoped background tasks.

## Waiting for background work

A background task runs *after* the response is sent, so there is normally
nothing to await - that is the point. When you do need to know it finished (a
test asserting its effect, a script that must not exit early),
`wait_for_background_tasks()` waits for every task the application currently has
in flight:

```python
from veloce import BackgroundTasks, Request, Veloce

app = Veloce()
sent = []


@app.post("/subscribe")
async def subscribe(request: Request, tasks: BackgroundTasks):
    tasks.add_task(sent.append, "welcome@example.com")
    return {"queued": True}
```

```python
from veloce.testclient import TestClient

with TestClient(app) as client:
    client.post("/subscribe")
    client.wait_for_background_tasks()
    assert sent == ["welcome@example.com"]
```

It returns `True` when everything finished and `False` if the `timeout`
(5 seconds by default; pass `None` to wait indefinitely) elapsed first. It
**waits rather than cancels**, so a timeout leaves the work running - shutdown
is what cancels. A task that spawns another is waited for in full, and a task
that raises does not become the caller's exception: failures are logged the same
way they are without a wait.

`AsyncTestClient.wait_for_background_tasks()` is the awaitable form, and
`app.wait_for_background_tasks()` is available anywhere you already have a
running loop.

!!! note "Added in version 0.18"

## Next steps

- [Server-Sent Events](sse.md) — stream events to the client over a
  long-lived connection.
- [Requests & Responses](requests-responses.md) — the response classes you
  attach tasks to.
- [Dependency injection](dependency-injection.md) — for request-scoped
  values supplied via `Depends`.
- API reference: [`BackgroundTask`](../reference/tasks.md#veloce.BackgroundTask),
  [`BackgroundTasks`](../reference/tasks.md#veloce.BackgroundTasks).
