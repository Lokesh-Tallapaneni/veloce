# Bench result — json-hello

Identical handler in each framework: `GET /` returning the dict
`{"hello": "world"}`. This stresses the core dispatch + JSON-response
path with no path parameters, no body parsing, no middleware.

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
| **veloce**  | **1,125** | 6,319  | **9,306**  | 0 |
| fastapi   | 1,105     | 6,371  | 9,709  | 0 |
| flask     | 714       | 5,102  | 10,380 | 0 |

**Veloce beats both rivals on rps and p99 (median).** Flask leads on p50
because sync WSGI has the shortest per-request critical path at low
concurrency, but trails ~37 % on throughput and loses on tail.

## How to reproduce

```bash
python -m bench.comparative.bench json-hello --runs 5 --duration 5 --concurrency 8
```
