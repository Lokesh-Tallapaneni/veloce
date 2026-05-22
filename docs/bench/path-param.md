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

## Result

| framework | rps (med) | p50 µs | p99 µs | errors |
|-----------|----------:|-------:|-------:|-------:|
| **veloce**  | **1,159** | 6,223  | **8,606**  | 0 |
| fastapi   | 1,091     | 6,400  | 9,148  | 0 |
| flask     | 731       | 4,965  | 8,717  | 0 |

**Veloce beats both rivals on rps and p99 (median).** Same shape as
json-hello: Veloce ~6 % ahead of FastAPI on rps and ~6 % ahead on p99;
Flask leads p50 by ~25 % but trails ~37 % on throughput. The radix-tree
router with parameter extraction at routing time (R12) keeps the
path-param case essentially as cheap as the static-path case.

## How to reproduce

```bash
python -m bench.comparative.bench path-param --runs 5 --duration 5 --concurrency 8
```
