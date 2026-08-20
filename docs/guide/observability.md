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

[`app.add_instrumentation(hook)`](../reference.md#veloce.Veloce.add_instrumentation)
registers a callable that receives a
[`RequestMetrics`](../reference.md#veloce.RequestMetrics) record after each
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

[`instrument_access_log`](../reference.md#veloce.instrument_access_log) emits
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

## See also

- [Middleware](middleware.md) — `LoggingMiddleware` and `RequestIDMiddleware`
  for request-scoped logging without the instrumentation hook.
- [Deployment](deployment.md) — running Veloce in production.
