---
description: Instrument Veloce with per-request metrics — a structured access log, a Prometheus exporter, and custom instrumentation hooks driven by RequestMetrics.
tags: [observability, metrics, logging]
---

# Observability

Veloce exposes a single instrumentation hook that fires once per finished
HTTP request. Every observability integration — the access log and the
Prometheus exporter — is built on it, and you can register your own hook the
same way.

## The instrumentation hook

[`app.add_instrumentation(hook)`](../reference/application.md#veloce.Veloce.add_instrumentation)
registers a callable that receives a
[`RequestMetrics`](../reference/tasks.md#veloce.RequestMetrics) record after each
request finishes. The hook may be a plain function or a coroutine function,
and a hook that raises is logged and skipped so instrumentation never breaks
a response. With no hook registered the request path carries no
instrumentation cost — not even a clock read.

```python
@app.add_instrumentation
def export(metrics):
    statsd.timing(metrics.route or "unmatched", metrics.duration_ms)
```

`RequestMetrics` carries these fields:

| Field | Carries |
| --- | --- |
| `method` | the request `method` |
| `path` | the concrete `path` |
| `route` | the matched `route` template (or `None` for a 404/405) |
| `status_code` | the `status_code` |
| `duration_ms` | the wall-clock `duration_ms` |
| `streamed` | a `streamed` flag |

The route *template*
(`/items/{id}`) — not the concrete path — is the safe aggregation key:
an attacker-controlled URL can never explode label cardinality.

## Access logging

[`instrument_access_log`](../reference/tasks.md#veloce.instrument_access_log) emits
one access-log record per request, sourced from the same `RequestMetrics`
record so logs and traces stay joinable on `(route, status)`. It bootstraps a
default handler on the `veloce.access` logger and supports a text or JSON
format:

```python
from veloce import Veloce
from veloce.observability import instrument_access_log

app = Veloce()
instrument_access_log(app, json=True)
```

The hook gates on `logger.isEnabledFor`, so a muted access log does zero
serialization work. Pass your own `logger=...` to route records into an
existing logging setup. Register this **instead of** `LoggingMiddleware`, not
in addition — doing both double-logs each request.

### Keeping log writes off the event loop

The bootstrapped handler is a `StreamHandler`, and `logging` writes
synchronously: the write happens on the event loop, so a slow or blocked
destination stalls request handling. On a local terminal or a container's
stdout that is negligible; over a network handler, or with stdout piped to
something that stops reading, it is not.

The stdlib fix is a queue: the handler on the hot path only enqueues, and a
listener thread performs the actual write.

```python
import logging
import logging.handlers
import queue

from veloce import Veloce
from veloce.observability import instrument_access_log

log_queue: queue.Queue = queue.Queue(-1)

access_logger = logging.getLogger("veloce.access")
access_logger.handlers.clear()
access_logger.addHandler(logging.handlers.QueueHandler(log_queue))
access_logger.setLevel(logging.INFO)

listener = logging.handlers.QueueListener(log_queue, logging.StreamHandler())
listener.start()

app = Veloce()
instrument_access_log(app, json=True)
```

`QueueHandler.emit` is a `put_nowait`, so the request path never blocks on the
destination. Stop the listener during shutdown so buffered records are flushed:

```python
@app.on_shutdown
async def stop_log_listener() -> None:
    listener.stop()
```

Configure the handler *before* the first request: `instrument_access_log` and
`LoggingMiddleware` only install their own `StreamHandler` when the logger has
none, so a handler you added first is left alone.

An async logging library is not needed for this, and Veloce does not ship one.
Choosing the transport is the application's decision — the queue pattern works
with any handler, adds no dependency, and is what the framework's own loggers
inherit once you configure them.

## Prometheus metrics

`instrument_with_prometheus` (from `veloce.metrics`) exports a request counter
and a request-duration histogram from the same hook. It is an optional
integration — install the extra:

```
pip install veloceframework[metrics]
```

Then wire it up once at startup. The exporter only records series; serving
`/metrics` is the application's job:

```python
from veloce import Veloce
from veloce.metrics import instrument_with_prometheus

app = Veloce()
instrument_with_prometheus(app)
```

This registers two series:

| Series | Labels |
| --- | --- |
| `http_requests_total` counter | method, route template, and status |
| `http_request_duration_seconds` histogram | method and route template |

An unmatched request (404/405) uses the constant `"<unmatched>"` route label.

Override the metric name `prefix`, the histogram `buckets`, or pass a custom
`registry=...` to isolate apps.

The collectors bind to prometheus_client's process-global registry by default,
which is what the exposition helpers (`generate_latest`, `make_asgi_app`,
`start_http_server`) read. That is right for a single app per process, but it
means instrumenting twice in one process collides — a second app, or a test
suite that builds the app once per test:

```python
from prometheus_client import CollectorRegistry

registry = CollectorRegistry()
instrument_with_prometheus(app, registry=registry)
```

Pass a fresh registry per app, or a distinct `prefix=`. Serve that registry
from your `/metrics` route rather than the global one.

Calling `instrument_with_prometheus` without the extra installed raises an
`ImportError` with an install hint.

## Finding a blocked event loop

A synchronous call inside an `async def` handler — a blocking database driver, a
`requests` call, a CPU-heavy loop — freezes the whole event loop. Every other
request stalls behind it, so the symptom is unrelated endpoints getting slow
while the endpoint that actually contains the problem looks fine.

The event-loop watchdog finds the line responsible. It is opt-in through one
config key:

```python
from veloce import Veloce

app = Veloce()
app.config["EVENT_LOOP_WATCHDOG"] = True
```

Set it before startup — the watchdog is constructed once, during the lifespan
startup phase, so assigning to a running app has no effect.

A stall is then reported like this:

```text
WARNING veloce.watchdog: event loop blocked for 78 ms while serving GET /report
  - the loop thread is parked in one call - most likely blocking I/O or a
  sleep. Use an async client, or move the call off the loop with
  asyncio.to_thread().
Blocked loop stack:
  ...
  File "app.py", line 34, in report
    rows = connection.execute("select * from ledger").fetchall()
```

The report names the route and carries the stack of the loop thread *while it is
still stuck*, which is the part that makes it actionable: the last frame is the
call that blocked. A blocking call inside a dependency is attributed to that
dependency rather than to the handler body.

!!! note "The blocking-versus-CPU hint is unreliable"

    The second sentence classifies the stall by comparing two stack samples,
    and a CPU-bound loop that sits on one line looks the same in both. Tight
    loops are usually reported as "parked in one call". Read the stack rather
    than the hint — for CPU-bound work the advice to use `asyncio.to_thread()`
    does not help, because the GIL keeps that thread contending with the loop.
    A process pool does.

### Tuning it

Pass a mapping instead of `True` to change the thresholds:

```python
app.config["EVENT_LOOP_WATCHDOG"] = {"interval": 0.02, "stall_threshold": 0.05}
```

`interval` (default `0.05`) is how often the heartbeat re-arms on the loop.
`stall_threshold` (default `0.1`) is how long the loop may go without a
heartbeat before a stall is reported. Lower both to catch shorter stalls while
you are hunting one; the cost is a busier watch thread.

Each distinct stall is reported once, however long it lasts, so a handler that
blocks for ten seconds produces one warning rather than a stream of them.

### Why it is opt-in

The watchdog runs a real OS thread that samples the loop, so an app that never
sets the key constructs nothing and pays nothing. It is a development aid:
leaving it on in production spends a thread continuously, and the report
contains a stack trace, which is the same category of disclosure as a debug
traceback.

It is also not armed by `debug=True`. The two are independent.

A stall is only reported while the loop is actually running. An idle loop —
stopped between calls, or parked waiting for I/O with nothing to do — is not a
stall, so the in-memory [`TestClient`](testing.md), which drives one request at
a time, does not trigger it. Exercising the watchdog needs a running server.

## See also

- [Middleware](middleware.md) — `LoggingMiddleware` and `RequestIDMiddleware`
  for request-scoped logging without the instrumentation hook.
- [Deployment](deployment.md) — running Veloce in production.
