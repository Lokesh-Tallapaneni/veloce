# Kubernetes

Kubernetes asks two different questions about a pod, and answering both with
one endpoint is the most common cause of a bad rollout.

| Probe | Question | Failure means |
| --- | --- | --- |
| `livenessProbe` | Is this process wedged? | The container is **killed and restarted** |
| `readinessProbe` | Should this pod receive traffic? | The pod is **removed from the Service** |

The distinction matters under load. If your liveness probe checks the
database, a thirty-second database blip restarts *every* replica at once —
turning a brief degradation into a full outage, and losing all in-flight
requests. Liveness must only answer "is this process still running my event
loop?".

## Serving the probes

```python
from veloce import Veloce
from veloce.health import HealthPlugin

app = Veloce()
health = app.install(HealthPlugin())


@health.readiness_check("database")
async def database_ready() -> bool:
    return await pool.fetchval("SELECT 1") == 1
```

That serves `/livez` and `/readyz`. Checks may be sync or async, run
concurrently under one shared timeout, and a check that raises or hangs counts
as not-ready rather than erroring the probe.

`/readyz` reports which check failed, so a probe failure is diagnosable from
the response instead of only from logs:

```json
{"status": "not_ready", "checks": {"database": "fail", "cache": "pass"}}
```

Register only dependencies this replica genuinely cannot serve without. A check
on a non-essential downstream will pull the pod out of rotation for something
it could have degraded gracefully around.

## Draining before shutdown

On `SIGTERM`, Kubernetes sends the signal *and* removes the pod from endpoints
at roughly the same moment — but propagating that removal to every kube-proxy
takes time, so traffic keeps arriving for a short window after the signal.

`HealthPlugin` fails `/readyz` as soon as shutdown begins while `/livez` keeps
passing, so the pod stops being routed to without being restarted mid-drain.
Pair it with a `preStop` sleep long enough for endpoint removal to propagate:

```yaml
apiVersion: apps/v1
kind: Deployment
spec:
  template:
    spec:
      terminationGracePeriodSeconds: 60
      containers:
        - name: api
          image: your-image
          lifecycle:
            preStop:
              exec:
                command: ["sh", "-c", "sleep 5"]
          livenessProbe:
            httpGet:
              path: /livez
              port: 8000
            periodSeconds: 10
            failureThreshold: 3
          readinessProbe:
            httpGet:
              path: /readyz
              port: 8000
            periodSeconds: 5
            failureThreshold: 2
          startupProbe:
            httpGet:
              path: /livez
              port: 8000
            periodSeconds: 5
            failureThreshold: 30
```

`terminationGracePeriodSeconds` must exceed the `preStop` sleep plus both
shutdown budgets, which run in sequence: `GRACEFUL_DRAIN_TIMEOUT` (default 30 s)
for in-flight requests, then `GRACEFUL_TASK_TIMEOUT` (default 10 s) for
`app.spawn(...)` background tasks. Below that sum, Kubernetes `SIGKILL`s the
container mid-shutdown.

## Startup probes

A `startupProbe` gives a slow-starting app time to warm up without a lax
liveness threshold. Until it succeeds, the liveness and readiness probes are
not consulted, so `failureThreshold × periodSeconds` is your startup budget —
30 × 5s = 150 seconds above.

`/readyz` returns `503` with `{"status": "not_ready", "reason": "starting"}`
until the application's startup hooks have completed, so a pod is never routed
to before its lifespan has run.

## Probe paths and the schema

Both paths are configurable, and the routes stay out of the OpenAPI schema by
default so probes do not appear in your published API:

```python
app.install(HealthPlugin(liveness_path="/healthz", readiness_path="/ready"))
```

If you also export Prometheus metrics, exclude the probe routes so
health-check traffic does not dominate your request counters — see
[Observability](../guide/observability.md).

## Next steps

- [Deployment concepts](concepts.md) — workers, restarts, and process models
- [Docker](docker.md) — building the image this Deployment runs
- [Observability](../guide/observability.md) — metrics and access logs
