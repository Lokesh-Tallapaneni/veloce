# Bench result — path-param

Identical handler in each framework: `GET /items/{item_id}` returning
`{"id": item_id, "name": f"item-{item_id}"}`. Stresses the path-param
extraction + type coercion path on top of the json-hello pipeline.

## Setup

- 5 runs per framework, 5 second timed window per run (after a 1 s
  warmup), 8 concurrent connections, run order shuffled.
- Server: `uvicorn --loop asyncio --no-access-log --log-level critical`,
  single worker.
- Client: `httpx.AsyncClient`, same machine, localhost.
- Versions: Veloce 0.1.0 · FastAPI 0.115.12 · Flask 3.1.1 · uvicorn 0.34.2
  · httpx 0.28.1 · CPython 3.13 on Windows 10.

## Result (seed 42, 5 measured runs + 1 discarded cold-cache round)

| framework | rps (med) | p50 µs | p99 µs | errors |
|-----------|----------:|-------:|-------:|-------:|
| **veloce**  | **1,263** | 6,160  | 8,407  | 0 |
| fastapi   | 1,196     | 6,178  | 8,964  | 0 |
| flask     | 795       | **4,920**  | **7,401**  | 0 |

**vs FastAPI:** Veloce wins on rps (+6 %), p50 (-0.3 %), and p99 (-6 %).

**vs Flask:** Veloce wins on rps (+59 %); Flask wins p50 (-20 %) and p99
(-12 %). Same shape as json-hello — the radix-tree router with parameter
extraction at routing time (R12) keeps path-param dispatch essentially
as cheap as the static-path case, but sync WSGI through
`asgiref.WsgiToAsgi` still keeps a shorter critical path at low
concurrency.

See the [README caveats](README.md#caveats--read-before-quoting-any-number-out-of-context):
Flask runs via `asgiref.WsgiToAsgi` under one uvicorn worker, which is
not its native deployment. These numbers are not "Veloce vs Flask in
production"; they are relative measurements within this harness.

## How to reproduce

```bash
python -m bench.comparative.bench path-param --runs 5 --duration 5 --concurrency 8 --seed 42
```
